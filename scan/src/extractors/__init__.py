from src.extractors.base import BaseExtractor
from src.extractors.bilibili import BilibiliExtractor, expand_collection
from src.extractors.youtube import YouTubeExtractor, extract_playlist_urls

_extractors: list[BaseExtractor] = [YouTubeExtractor(), BilibiliExtractor()]


def get_extractor(url: str) -> BaseExtractor:
    for ex in _extractors:
        if ex.match(url):
            return ex
    raise ValueError(f"不支持该视频平台: {url}")


def expand_playlist(url: str) -> tuple[list[str], str]:
    """Expand a playlist/collection URL into (video_urls, author_name).
    Supports YouTube playlists and Bilibili collections.
    """
    # Try YouTube playlist
    urls = extract_playlist_urls(url)
    if urls:
        return urls, ""

    # Try Bilibili collection (yt-dlp BilibiliCollectionList)
    return expand_collection(url)
