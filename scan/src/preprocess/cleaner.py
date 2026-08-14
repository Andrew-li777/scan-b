import re

from opencc import OpenCC

from src.models import SubtitleEntry

_HTML_RE = re.compile(r"<[^>]+>")
_NOTE_RE = re.compile(r"♪|♫|\[音乐\]|\[Music\]|\(音乐\)|\[掌声\]|\[Applause\]", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_t2s = OpenCC("t2s")


def clean(entries: list[SubtitleEntry]) -> list[SubtitleEntry]:
    result: list[SubtitleEntry] = []
    for e in entries:
        text = _HTML_RE.sub("", e.text)
        text = _NOTE_RE.sub("", text)
        text = _WS_RE.sub(" ", text).strip()
        if text:
            result.append(SubtitleEntry(start=e.start, end=e.end, text=_t2s.convert(text)))
    return result
