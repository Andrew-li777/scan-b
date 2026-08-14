from src.models import SubtitleEntry


def deduplicate(entries: list[SubtitleEntry], threshold: float = 0.85) -> list[SubtitleEntry]:
    if not entries:
        return []
    result = [entries[0]]
    for cur in entries[1:]:
        prev = result[-1]
        if _similarity(prev.text, cur.text) < threshold:
            result.append(cur)
        else:
            result[-1] = SubtitleEntry(start=prev.start, end=cur.end, text=prev.text)
    return result


def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    longer = a if len(a) > len(b) else b
    shorter = b if len(a) > len(b) else a
    if not longer:
        return 1.0
    match = sum(1 for i, ch in enumerate(shorter) if i < len(longer) and ch == longer[i])
    return match / len(longer)
