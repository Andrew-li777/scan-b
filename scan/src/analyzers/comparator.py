"""Compare multiple analyzed videos using their stored summaries and keypoints."""

from src.llm import chat
from src.web.history import _load_detail

_COMPARE_SYSTEM = """你是一个视频内容对比分析专家。请基于用户提供的多个视频摘要和重点，进行横向对比。

要求:
1. 只基于提供的信息分析，不要编造视频中没有的内容
2. 输出中文 Markdown
3. 必须包含以下四部分:
   - ## 共同主题
   - ## 各视频独特贡献
   - ## 横向对比表
   - ## 观看建议
4. 横向对比表必须使用标准 Markdown 表格，维度至少包含: 核心方法、关键概念、适用场景、难度级别
5. 如果视频主题差异较大，请明确指出差异，不要强行总结共同点
"""


def compare_videos(base_names: list[str]) -> str:
    if len(base_names) < 2:
        raise ValueError("至少需要 2 个视频进行对比")
    if len(base_names) > 5:
        raise ValueError("单次最多对比 5 个视频")

    videos = []
    for bn in base_names:
        detail = _load_detail(bn)
        if detail is None:
            raise ValueError(f"记录不存在: {bn}")
        videos.append({
            "base_name": bn,
            "title": detail["title"],
            "summary": _extract_summary(detail["overview"]),
            "keypoints": detail["keypoints"][:8],
        })

    return chat(_COMPARE_SYSTEM, _build_compare_prompt(videos), max_tokens=4096)


def _extract_summary(overview_md: str, max_len: int = 800) -> str:
    text = _strip_frontmatter(overview_md)
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("**来源**") or line.startswith("**原链**") or line == "---":
            continue
        lines.append(line)
        if len(" ".join(lines)) >= max_len:
            break
    return " ".join(lines)[:max_len]


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text


def _build_compare_prompt(videos: list[dict]) -> str:
    parts = []
    for i, v in enumerate(videos, 1):
        kp_text = "\n".join(
            f"  - [{_fmt_ts(kp['timestamp'])}] {kp['content']}"
            for kp in v["keypoints"]
        ) or "  - 无重点数据"
        parts.append(
            f"视频 {i}: {v['title']}\n"
            f"base_name: {v['base_name']}\n"
            f"总结: {v['summary']}\n"
            f"重点:\n{kp_text}"
        )
    return "\n\n".join(parts)


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
