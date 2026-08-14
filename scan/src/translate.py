import asyncio
import re

from src.llm import chat
from src.models import KeyPoint, SubtitleEntry

_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
_TRANSLATE_SYSTEM = """你是一个专业翻译。将以下英文内容翻译成简体中文。要求:
1. 只输出中文译文，不要解释、不要原文
2. 保持技术术语的准确性
3. 保持原文的格式和标点风格"""


def needs_translation(entries: list[SubtitleEntry], threshold: float = 0.3) -> bool:
    """Check if less than `threshold` of chars are CJK (Chinese)."""
    sample = " ".join(e.text for e in entries[:20])
    if not sample:
        return False
    cjk_count = len(_CJK_RE.findall(sample))
    total = len(sample.replace(" ", ""))
    if total == 0:
        return False
    return (cjk_count / total) < threshold


def translate_text(text: str) -> str:
    if not text.strip():
        return ""
    return chat(_TRANSLATE_SYSTEM, text, max_tokens=2048).strip()


def translate_keypoints(kps: list[KeyPoint]) -> list[KeyPoint]:
    if not kps:
        return kps
    lines = [f"[{kp.timestamp:.0f}s] {kp.content}" for kp in kps]
    combined = "\n".join(lines)
    result = chat(
        "将以下英文内容翻译成简体中文，保持编号和时间戳格式不变，每行对应翻译。",
        combined,
        max_tokens=4096,
    )
    translated = result.strip().split("\n")
    for i, line in enumerate(translated):
        if i < len(kps):
            m = re.match(r"^\[\d+s\]\s*(.+)", line)
            if m:
                kps[i].content += "\n" + m.group(1)
    return kps


async def translate_subtitles(subs: list[SubtitleEntry], batch_size: int = 15) -> list[str]:
    """Return translated lines aligned with subs indices."""
    translations: list[str] = [""] * len(subs)

    async def _translate_batch(start: int) -> list[tuple[int, str]]:
        batch = subs[start:start + batch_size]
        lines = [f"[{e.start:.0f}s] {e.text}" for e in batch]
        combined = "\n".join(lines)
        try:
            result = await asyncio.to_thread(
                chat,
                "将以下英文内容翻译成简体中文，保持时间戳格式，每行对应翻译。只输出译文。",
                combined,
                max_tokens=4096,
            )
        except Exception:
            return []
        parsed = []
        for j, line in enumerate(result.strip().split("\n")):
            idx = start + j
            if idx < len(subs):
                m = re.match(r"^\[\d+s\]\s*(.+)", line)
                parsed.append((idx, m.group(1) if m else line))
        return parsed

    sem = asyncio.Semaphore(3)  # translation is heavy, limit concurrency

    async def bounded(start):
        async with sem:
            return await _translate_batch(start)

    tasks = [bounded(i) for i in range(0, len(subs), batch_size)]
    results = await asyncio.gather(*tasks)
    for batch_result in results:
        for idx, text in batch_result:
            translations[idx] = text
    return translations
