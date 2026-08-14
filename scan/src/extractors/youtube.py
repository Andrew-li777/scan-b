import re

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

from src.extractors.base import BaseExtractor, clean_url
from src.models import SubtitleEntry, VideoMeta

_YT_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([\w-]{11})"
)
_YT_PLAYLIST_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?youtube\.com/playlist\?list=([\w-]+)"
)


def extract_playlist_urls(url: str) -> list[str]:
    """Expand a YouTube playlist URL into individual video URLs."""
    if not _YT_PLAYLIST_PATTERN.search(url):
        return []
    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        entries = info.get("entries", [])
        return [
            f"https://www.youtube.com/watch?v={e['id']}"
            for e in entries if e.get("id")
        ]


class YouTubeExtractor(BaseExtractor):
    platform = "youtube"

    def match(self, url: str) -> bool:
        return bool(_YT_URL_PATTERN.search(url)) or bool(_YT_PLAYLIST_PATTERN.search(url))

    def extract(self, url: str) -> VideoMeta:
        video_id = self._parse_video_id(url)
        info = self._fetch_info(url)
        subs = self._fetch_subtitles(video_id)
        return VideoMeta(
            platform="youtube",
            video_id=video_id,
            title=info.get("title", ""),
            url=url,
            duration=int(info.get("duration", 0) or 0),
            author=info.get("uploader", ""),
            thumbnail=info.get("thumbnail", ""),
            subtitles=subs,
        )

    def _parse_video_id(self, url: str) -> str:
        m = _YT_URL_PATTERN.search(url)
        if not m:
            raise ValueError(f"无法解析 YouTube URL: {url}")
        return m.group(1)

    def _fetch_info(self, url: str) -> dict:
        opts = {"quiet": True, "no_warnings": True, "extract_flat": False}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _fetch_subtitles(self, video_id: str) -> list[SubtitleEntry]:
        last_error = None

        # Method 1: youtube-transcript-api (preferred)
        try:
            api = YouTubeTranscriptApi()
            tl = api.list(video_id)
            manual = [t for t in tl if not t.is_generated]
            generated = [t for t in tl if t.is_generated]
            candidates = manual + generated

            preferred = None
            # Two-pass: manual subtitles first (author-provided, more accurate),
            # then auto-generated. Within each, prefer original-language tracks
            # for English-source channels (en > zh > ja > ko).
            for source in (manual, generated):
                for lang_prefix in ("en", "zh", "ja", "ko"):
                    for t in source:
                        if t.language_code.startswith(lang_prefix):
                            preferred = t
                            break
                    if preferred:
                        break
                if preferred:
                    break
            if not preferred:
                preferred = candidates[0] if candidates else None
            if not preferred:
                raise NoTranscriptFound(video_id, ["any"], None)

            fetched = api.fetch(video_id, languages=[preferred.language_code])
            entries = list(fetched)
        except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as e:
            last_error = str(e)
        except Exception as e:
            last_error = str(e)

        # Method 2: yt-dlp subtitle extraction (fallback)
        if last_error and not last_error.startswith("NoTranscriptFound"):
            try:
                entries = self._fetch_subs_via_ytdlp(video_id)
                if entries:
                    last_error = None
            except Exception:
                pass

        if last_error:
            raise RuntimeError(f"字幕提取失败: {last_error}")

        out: list[SubtitleEntry] = []
        for e in entries:
            if hasattr(e, "start"):
                out.append(SubtitleEntry(
                    start=float(e.start),
                    end=float(e.start + e.duration),
                    text=e.text,
                ))
            else:
                out.append(SubtitleEntry(
                    start=float(e["start"]),
                    end=float(e["end"]),
                    text=e["text"],
                ))
        return out

    def _fetch_subs_via_ytdlp(self, video_id: str) -> list[dict]:
        url = f"https://www.youtube.com/watch?v={video_id}"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "zh-Hans", "zh", "ja", "ko"],
            "skip_download": True,
            "outtmpl": "-",
        }
        entries: list[dict] = []
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            subs = info.get("subtitles", {}) or info.get("automatic_captions", {})
            for lang in ("en", "zh-Hans", "zh", "ja", "ko"):
                if lang in subs:
                    for fmt in subs[lang]:
                        if fmt.get("ext") in ("srv1", "srv2", "srv3", "json3"):
                            continue
                        # Download the subtitle file
                        sub_data = ydl.urlopen(fmt["url"]).read().decode("utf-8", errors="replace")
                        entries = self._parse_srt(sub_data)
                        if entries:
                            return entries
        return entries

    def _parse_srt(self, srt_text: str) -> list[dict]:
        entries = []
        pattern = re.compile(
            r"(\d+)\n(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\n([\s\S]+?)(?=\n\n|\Z)"
        )
        for m in pattern.finditer(srt_text):
            start = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4)) + int(m.group(5)) / 1000
            end = int(m.group(6)) * 3600 + int(m.group(7)) * 60 + int(m.group(8)) + int(m.group(9)) / 1000
            text = m.group(10).replace("\n", " ").strip()
            # Remove HTML tags from SRT
            text = re.sub(r"<[^>]+>", "", text)
            entries.append({"start": start, "end": end, "text": text, "duration": end - start})
        return entries
