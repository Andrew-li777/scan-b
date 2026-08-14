from src.models import SubtitleEntry

_MIN_SEGMENT_CHARS = 5
_MAX_SEGMENT_GAP = 2.0  # seconds
_MAX_SEGMENT_DURATION = 300.0  # 5 minutes


def merge_short(entries: list[SubtitleEntry], min_chars: int = _MIN_SEGMENT_CHARS) -> list[SubtitleEntry]:
    if not entries:
        return []
    result: list[SubtitleEntry] = []
    buf = entries[0]
    for cur in entries[1:]:
        if len(buf.text) < min_chars or len(cur.text) < min_chars:
            buf = SubtitleEntry(
                start=buf.start,
                end=cur.end,
                text=(buf.text + " " + cur.text).strip(),
            )
        else:
            result.append(buf)
            buf = cur
    result.append(buf)
    return result


def segment(entries: list[SubtitleEntry], max_gap: float = _MAX_SEGMENT_GAP) -> list[list[SubtitleEntry]]:
    if not entries:
        return []
    segments: list[list[SubtitleEntry]] = []
    current = [entries[0]]
    for cur in entries[1:]:
        gap = cur.start - current[-1].end
        seg_dur = cur.end - current[0].start
        if gap > max_gap or seg_dur > _MAX_SEGMENT_DURATION:
            segments.append(current)
            current = [cur]
        else:
            current.append(cur)
    segments.append(current)
    return segments


def build_text(entries: list[SubtitleEntry]) -> str:
    return " ".join(e.text for e in entries)
