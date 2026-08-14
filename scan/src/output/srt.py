from src.models import SubtitleEntry


def generate_srt(entries: list[SubtitleEntry]) -> str:
    """Convert SubtitleEntry[] to SRT subtitle format string."""
    blocks: list[str] = []
    for i, e in enumerate(entries, 1):
        start = _fmt_srt_time(e.start)
        end = _fmt_srt_time(e.end)
        text = e.text.replace("\n", " ") if "\n" in e.text else e.text
        blocks.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(blocks)


def _fmt_srt_time(seconds: float) -> str:
    """Format seconds to SRT time: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
