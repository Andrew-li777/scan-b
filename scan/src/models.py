from dataclasses import dataclass, field


@dataclass
class SubtitleEntry:
    start: float
    end: float
    text: str


@dataclass
class VideoMeta:
    platform: str
    video_id: str
    title: str
    url: str
    duration: int
    author: str
    subtitles: list[SubtitleEntry] = field(default_factory=list)
    thumbnail: str = ""
    subtitle_source: str = ""  # 字幕来源: "CC字幕(lan)" / "AI字幕(lan)" / "whisper" / ""


@dataclass
class Chapter:
    start: float
    title: str
    summary: str


@dataclass
class KeyPoint:
    timestamp: float
    content: str
    importance: int  # 1-5


@dataclass
class AnalysisResult:
    meta: VideoMeta
    summary: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    keypoints: list[KeyPoint] = field(default_factory=list)
