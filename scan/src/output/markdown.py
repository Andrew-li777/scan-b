import os
import re
from datetime import date
from pathlib import Path

from src.config import settings
from src.models import AnalysisResult, KeyPoint, SubtitleEntry, VideoMeta
from src.output.srt import generate_srt

_OUTPUT_DIR = Path(settings.output_dir) if settings.output_dir else Path(__file__).parent.parent.parent / "output"


def _fmt_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, list):
            inner = ", ".join(f'"{x}"' for x in v)
            lines.append(f"{k}: [{inner}]")
        elif isinstance(v, str) and ('"' in v or ":" in v):
            lines.append(f'{k}: "{v}"')
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def generate_all(result: AnalysisResult, base_name: str, concepts: list[str] = None) -> dict[str, str]:
    concepts = concepts or []
    folder = _OUTPUT_DIR / base_name
    folder.mkdir(parents=True, exist_ok=True)
    outputs = {}
    outputs["subtitles"] = _gen_subtitles(result.meta, base_name, folder)
    outputs["overview"] = _gen_overview(result, base_name, folder, concepts)
    outputs["keypoints"] = _gen_keypoints(result, base_name, folder, concepts)
    srt_path = folder / "subtitles.srt"
    srt_path.write_text(generate_srt(result.meta.subtitles), encoding="utf-8")
    outputs["srt"] = str(srt_path.relative_to(_OUTPUT_DIR))
    return outputs


def _safe_name(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|' ]", "_", name)[:80]


def _gen_subtitles(meta: VideoMeta, base_name: str, folder: Path) -> str:
    filepath = folder / "subtitles.md"
    fm = _fmt_frontmatter({
        "title": meta.title,
        "type": "subtitles",
        "platform": meta.platform,
        "author": meta.author,
        "duration": meta.duration,
        "url": meta.url,
        "date": str(date.today()),
    })
    lines = [
        fm,
        f"# {meta.title} — 完整字幕",
        "",
        f"**来源**: {meta.platform} | **作者**: {meta.author} | **时长**: {_fmt_duration(meta.duration)}",
        f"**原链**: {meta.url}",
        "",
        "---",
        "",
    ]
    for e in meta.subtitles:
        lines.append(f"[{_fmt_time(e.start)}] {e.text}")
        lines.append("")
    filepath.write_text("\n".join(lines), encoding="utf-8")
    return str(filepath.relative_to(_OUTPUT_DIR))


def _gen_overview(result: AnalysisResult, base_name: str, folder: Path, concepts: list[str] = None) -> str:
    concepts = concepts or []
    filepath = folder / "overview.md"
    meta = result.meta
    fm = _fmt_frontmatter({
        "title": meta.title,
        "type": "overview",
        "platform": meta.platform,
        "author": meta.author,
        "duration": meta.duration,
        "url": meta.url,
        "date": str(date.today()),
        "concepts": concepts,
    })
    lines = [
        fm,
        f"# {meta.title} — 内容概览",
        "",
        f"**来源**: {meta.platform} | **作者**: {meta.author} | **时长**: {_fmt_duration(meta.duration)}",
        f"**原链**: {meta.url}",
        "",
        "---",
        "",
        result.summary,
        "",
    ]
    if result.chapters:
        lines.append("## 章节导航")
        lines.append("")
        lines.append("| # | 章节 | 概要 |")
        lines.append("|---|------|------|")
        for i, ch in enumerate(result.chapters, 1):
            lines.append(f"| {i} | {ch.title} | {ch.summary} |")
        lines.append("")
    filepath.write_text("\n".join(lines), encoding="utf-8")
    return str(filepath.relative_to(_OUTPUT_DIR))


def _gen_keypoints(result: AnalysisResult, base_name: str, folder: Path, concepts: list[str] = None) -> str:
    concepts = concepts or []
    filepath = folder / "keypoints.md"
    meta = result.meta
    fm = _fmt_frontmatter({
        "title": meta.title,
        "type": "keypoints",
        "platform": meta.platform,
        "author": meta.author,
        "duration": meta.duration,
        "url": meta.url,
        "date": str(date.today()),
        "concepts": concepts,
    })
    lines = [
        fm,
        f"# {meta.title} — 重点标示",
        "",
        f"**来源**: {meta.platform} | **作者**: {meta.author} | **时长**: {_fmt_duration(meta.duration)}",
        f"**原链**: {meta.url}",
        "",
        "---",
        "",
        "| 时间 | 内容 | 重要度 |",
        "|------|------|--------|",
    ]
    for kp in result.keypoints:
        stars = "★" * kp.importance + "☆" * (5 - kp.importance)
        content = kp.content.replace("\n", " ").replace("|", "｜")
        lines.append(f"| {_fmt_time(kp.timestamp)} | {content} | {stars} |")
    lines.append("")
    filepath.write_text("\n".join(lines), encoding="utf-8")
    return str(filepath.relative_to(_OUTPUT_DIR))


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_duration(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
