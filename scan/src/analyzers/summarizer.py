import asyncio
import re

from src.llm import chat, chat_json
from src.models import Chapter, KeyPoint, SubtitleEntry

_SUMMARIZE_SYSTEM = """你是一个专业的视频内容分析师。你的任务是对视频字幕内容进行总结。

请按以下结构输出：

## 一句话总结
用一句话概括整个视频的核心内容。

## 章节划分
根据内容语义转折，将视频划分为若干章节，每个章节包含：
- 起始时间 (格式 mm:ss)：必须取自材料中出现的真实时间点，**禁止整篇都用 00:00 占位**
- 章节标题
- 2-3 句话概括该章节内容

如果没有明确章节转折，至少划分 3-5 个逻辑段落。

## 详细摘要
3-5 段话，覆盖视频的主要观点和论证过程。

注意：
- 保留时间戳引用，方便回看
- 对技术类内容保留关键术语
"""

_CHUNK_SIZE = 15000  # chars per chunk for Map-Reduce
_MAX_CHUNKS = 12      # safety cap


async def summarize(subtitles: list[SubtitleEntry], title: str, progress_cb=None) -> tuple[str, list[Chapter]]:
    text = _build_timed_text(subtitles)
    # 注意：用「纯文本」长度判长短，不能用 _build_timed_text 的长度 ——
    # 后者每行多出 "[mm:ss] " 前缀（本例 14277 字符被算成 23720），会把
    # 本可一次消化的视频误判成长视频、压成「2-3 段话」，章节信息大量丢失。
    plain = " ".join(e.text for e in subtitles)
    if len(plain) <= _CHUNK_SIZE:
        return await asyncio.to_thread(_summarize_short, text, title)
    return await _summarize_long(subtitles, title, progress_cb)


def _summarize_short(text: str, title: str) -> tuple[str, list[Chapter]]:
    prompt = f"视频标题: {title}\n\n字幕内容:\n{text}"
    result = chat(_SUMMARIZE_SYSTEM, prompt)
    chapters = _parse_chapters(result)
    return result, chapters


async def _summarize_long(subtitles: list[SubtitleEntry], title: str, progress_cb=None) -> tuple[str, list[Chapter]]:
    from src.preprocess.segmenter import build_text, segment

    segs = segment(subtitles)
    n = len(segs)

    # Build chunks dynamically, targeting ~_CHUNK_SIZE chars each
    chunks: list[str] = []
    buf: list[SubtitleEntry] = []
    buf_len = 0
    for s in segs:
        text = build_text(s)
        if buf_len + len(text) > _CHUNK_SIZE and buf:
            chunks.append(_timed_chunk_text(buf))
            if len(chunks) >= _MAX_CHUNKS:
                # Merge remaining into last chunk
                buf = s
                buf_len = len(text)
            else:
                buf = s
                buf_len = len(text)
        else:
            buf.extend(s)
            buf_len += len(text)
    if buf:
        chunks.append(_timed_chunk_text(buf))

    total_chunks = len(chunks)

    # 只有 1 块就别走 Map-Reduce：先压成「2-3 段话」再还原摘要会丢时间轴与细节
    if total_chunks == 1:
        return await asyncio.to_thread(_summarize_short, chunks[0], title)

    async def _summarize_one(chunk: str, i: int) -> str:
        if progress_cb:
            progress_cb(f"摘要: {i + 1}/{total_chunks} 段")
        return await asyncio.to_thread(
            chat,
            f"请用2-3段话总结以下视频片段的内容（共{total_chunks}段中的第{i + 1}段）。\n"
            "片段中每句前都有 [mm:ss] 时间戳，首行标出本段范围。"
            "**必须保留 2-4 个关键时间点**，格式如「[12:34] 观点摘要」；不得省略或改写时间戳。",
            chunk,
            max_tokens=2048,
        )

    sem = asyncio.Semaphore(5)

    async def bounded(chunk, i):
        async with sem:
            return await _summarize_one(chunk, i)

    chunk_summaries = await asyncio.gather(*[bounded(c, i) for i, c in enumerate(chunks)])

    # Merge chunk summaries (2-level for very long videos)
    if total_chunks > 6:
        if progress_cb:
            progress_cb(f"摘要: 合并 {total_chunks} 段")

        async def _merge_group(group_parts: list[str], group_idx: int) -> str:
            group = "\n\n".join(
                f"=== 第{j + 1}部分 ===\n{group_parts[j]}"
                for j in range(len(group_parts))
            )
            return await asyncio.to_thread(
                chat,
                "请将以下视频片段的摘要合并为一段连贯的总结。尽量保留各摘要里的 [mm:ss] 时间点引用，不要改写时间。",
                group,
                max_tokens=1536,
            )

        merge_tasks = []
        for i in range(0, total_chunks, 3):
            merge_tasks.append(_merge_group(chunk_summaries[i:i + 3], i // 3))
        mid_summaries = await asyncio.gather(*merge_tasks)
        merged = "\n\n".join(f"=== 第{i + 1}部分 ===\n{s}" for i, s in enumerate(mid_summaries))
    else:
        merged = "\n\n".join(f"=== 第{i + 1}段摘要 ===\n{s}" for i, s in enumerate(chunk_summaries))

    if progress_cb:
        progress_cb("摘要: 生成最终总结")
    prompt = (
        f"视频标题: {title}\n\n以下是各部分的摘要（每部分都标了时间范围，摘要中带 [mm:ss] 时间点）:\n\n{merged}\n\n"
        "输出要求：章节起始时间必须取自材料中的真实 mm:ss 时间点，按时间顺序排列，"
        "**全篇不得用 00:00 占位**（除非视频确实从 0 秒开始）；每个章节附 2-3 句概括。"
    )
    result = chat(_SUMMARIZE_SYSTEM, prompt, max_tokens=4096)
    chapters = _parse_chapters(result)
    return result, chapters


async def extract_keypoints(subtitles: list[SubtitleEntry], title: str, progress_cb=None) -> list[KeyPoint]:
    text = _build_timed_text(subtitles)

    if len(text) <= _CHUNK_SIZE * 2:
        return await asyncio.to_thread(_extract_kp_direct, text, title)

    # For long videos: chunk → extract per chunk → deduplicate
    from src.preprocess.segmenter import build_text, segment
    segs = segment(subtitles)
    chunks: list[str] = []
    buf: list[SubtitleEntry] = []
    buf_len = 0
    for s in segs:
        t = build_text(s)
        if buf_len + len(t) > _CHUNK_SIZE * 2 and buf:
            chunks.append(build_text(buf))
            buf = s
            buf_len = len(t)
        else:
            buf.extend(s)
            buf_len += len(t)
    if buf:
        chunks.append(build_text(buf))

    async def _extract_one(chunk: str, i: int) -> list[KeyPoint]:
        if progress_cb:
            progress_cb(f"重点提取: {i + 1}/{len(chunks)} 段")
        return await asyncio.to_thread(_extract_kp_direct, chunk, f"{title} (第{i + 1}段)")

    sem = asyncio.Semaphore(5)
    async def bounded(chunk, i):
        async with sem:
            return await _extract_one(chunk, i)

    results = await asyncio.gather(*[bounded(c, i) for i, c in enumerate(chunks)])
    all_kps: list[KeyPoint] = []
    for r in results:
        all_kps.extend(r)

    # Deduplicate by content similarity, keep highest importance
    all_kps.sort(key=lambda k: k.importance, reverse=True)
    seen: set[str] = set()
    unique: list[KeyPoint] = []
    for kp in all_kps:
        key = kp.content[:50]
        if key not in seen:
            seen.add(key)
            unique.append(kp)
    unique.sort(key=lambda k: k.timestamp)
    return unique[:20]


def _extract_kp_direct(text: str, title: str) -> list[KeyPoint]:
    prompt = f"""视频标题: {title}

字幕内容:
{text}

请提取视频中 5-15 个最重要的观点/知识点。
每个重点包含: 时间戳(秒数)、内容描述、重要程度(1-5)。

输出 JSON 数组格式:
[{{"timestamp": 120.0, "content": "...", "importance": 5}}]"""

    try:
        raw = chat_json(_KEYPOINT_SYSTEM, prompt)
    except Exception:
        response = chat(_KEYPOINT_SYSTEM, prompt)
        raw = _extract_json_from_text(response)

    return [
        KeyPoint(
            timestamp=float(item.get("timestamp", 0)),
            content=str(item.get("content", "")),
            importance=int(item.get("importance", 3)),
        )
        for item in raw
    ]


_KEYPOINT_SYSTEM = """你是一个专业的视频内容分析助手。你的任务是从视频字幕中提取关键信息点。

要求:
1. 提取最重要、最有价值的观点/知识点/转折点
2. 每个关键点必须有精确的时间戳
3. 重要度评分: 5=核心观点必须知道, 3=重要支撑内容, 1=补充信息
4. 只提取真正有信息量的内容，跳过寒暄和废话
5. 对于教程/技术类视频，优先提取方法论和关键步骤"""


def _build_timed_text(subtitles: list[SubtitleEntry]) -> str:
    lines = []
    for e in subtitles:
        ts = _fmt_time(e.start)
        lines.append(f"[{ts}] {e.text}")
    return "\n".join(lines)


def _timed_chunk_text(entries: list[SubtitleEntry]) -> str:
    """Map 阶段的 chunk 文本：每句带 [mm:ss]，并在首行标出整段范围。

    `build_text()` 只拼纯文本、不保留时间戳 —— 长视频走 Map-Reduce 时，
    各段摘要再合并后时间轴已彻底丢失，最终「章节划分」只能写 00:00 占位。
    这里把时间信息带进 Map 阶段，Reduce 才能拿到真实时间点。
    """
    if not entries:
        return ""
    head = f"【本段范围 {_fmt_time(entries[0].start)}–{_fmt_time(entries[-1].end)}】"
    lines = [head]
    lines.extend(f"[{_fmt_time(e.start)}] {e.text}" for e in entries)
    return "\n".join(lines)


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


_CHAPTER_ITEM_RE = re.compile(
    r"^\s*(?:[-*+]|\d+\.)\s*\*{0,2}\s*[\[【（(]?(\d{1,2}:\d{2}(?::\d{2})?)[\]】）)]?\s*\*{0,2}\s*[:：、\-—]?\s*(.*)$"
)
_FIXED_SECTION_TITLES = {"一句话总结", "章节划分", "详细摘要", "章节导航"}


def _parse_ts(ts: str) -> float:
    parts = [int(x) for x in ts.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def _parse_chapters(text: str) -> list[Chapter]:
    """解析 LLM 输出里的章节。

    优先解析「## 章节划分」小节下的列表项（`- **12:34 标题**` / `1. 12:34 标题`），
    取真实起点时间。旧实现按 `##` 切分，会把「一句话总结 / 章节划分 / 详细摘要」
    这三个固定小节当成章节、且 start 恒为 0 —— 那是「章节导航」表一直只有三行
    假章节的原因。
    """
    import re

    chapters: list[Chapter] = []
    m = re.search(r"#{2,3}\s*章节划分\s*\n(.*?)(?=\n#{2,3}\s|\Z)", text, re.S)
    section = m.group(1) if m else ""

    cur: Chapter | None = None
    for raw in section.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        mm = _CHAPTER_ITEM_RE.match(line)
        if mm:
            if cur is not None:
                chapters.append(cur)
            title = mm.group(2).replace("**", "").strip().lstrip("：:、-— ")
            title = re.sub(r"^[（(][^）)]*[）)]\s*", "", title)  # 去掉「（逻辑段1）」前缀
            title = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", title).strip()
            cur = Chapter(start=_parse_ts(mm.group(1)), title=title[:80], summary="")
        elif cur is not None:
            body = line.strip().lstrip("-*+ ").strip()
            if body:
                cur.summary = (cur.summary + " " + body).strip()[:200]
    if cur is not None:
        chapters.append(cur)

    if len(chapters) >= 2 and any(c.start > 0 for c in chapters):
        return chapters

    # 回退：按小标题切分（排除固定小节名，避免再造假章节）
    chapters = []
    sections = re.split(r"\n#{2,3}\s+", text)
    for section_text in sections[1:]:
        lines = section_text.strip().split("\n", 1)
        title = lines[0].strip()
        if title in _FIXED_SECTION_TITLES:
            continue
        body = lines[1].strip() if len(lines) > 1 else ""
        chapters.append(Chapter(start=0, title=title, summary=body[:200]))
    return chapters


def _extract_json_from_text(text: str) -> list:
    """Robustly extract the first valid JSON array from free-form LLM text.

    The old non-greedy regex ``\\[[\\s\\S]*?\\]`` broke when the *content*
    itself contained brackets (e.g. "低保[最低生活保障]") — it would match
    up to the first closing bracket and then fail json.loads with
    "Expecting value" / "Unterminated string". Instead, scan for a JSON
    value start (``[`` or ``{``) and use ``raw_decode`` so brackets nested
    *inside* strings are handled correctly.
    """
    import json

    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            obj, _end = decoder.raw_decode(text, i)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, list):
            return obj
    return []



_CONCEPT_SYSTEM = """从视频内容中提取 3-5 个核心概念/术语，返回 JSON 字符串数组。
每个概念应该是可独立成笔记的专业术语（如 "self-attention"、"patch embedding"、"Vision Transformer"）。
不要重复，优先选择视频中最核心的技术术语。
只输出 JSON 数组，不要其他内容。"""


def extract_concepts(title: str, summary: str, keypoints: list[KeyPoint]) -> list[str]:
    kp_text = "\n".join(f"- {kp.content}" for kp in keypoints[:10])
    prompt = f"视频标题: {title}\n\n摘要: {summary[:3000]}\n\n重点:\n{kp_text}"
    try:
        result = chat_json(_CONCEPT_SYSTEM, prompt, max_tokens=512)
        return result if isinstance(result, list) else []
    except Exception:
        return []
