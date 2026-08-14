from src.models import SubtitleEntry


def make_entry(start: float, end: float, text: str) -> SubtitleEntry:
    return SubtitleEntry(start=start, end=end, text=text)
