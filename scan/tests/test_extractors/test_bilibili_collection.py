"""Tests for Bilibili collection/playlist expansion via yt-dlp."""
from unittest.mock import MagicMock, patch

from src.extractors.bilibili import expand_collection


def _mock_ydl(bvids: list[str], uploader: str = "TestUP"):
    """Create a mock YoutubeDL context manager that returns the given info."""
    info = type("InfoDict", (), {
        "get": lambda self, key, default=None: {
            "entries": [{"id": b} for b in bvids],
            "uploader": uploader,
        }.get(key, default),
    })()
    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock.extract_info.return_value = info
    return mock


class TestExpandCollection:
    def test_season_collection(self):
        with patch("yt_dlp.YoutubeDL", return_value=_mock_ydl(["BV001", "BV002", "BV003"])):
            urls, author = expand_collection(
                "https://space.bilibili.com/123456/lists/7890?type=season")
            assert urls == [
                "https://www.bilibili.com/video/BV001",
                "https://www.bilibili.com/video/BV002",
                "https://www.bilibili.com/video/BV003",
            ]
            assert author == "TestUP"

    def test_series_collection(self):
        with patch("yt_dlp.YoutubeDL", return_value=_mock_ydl(["BV100"])):
            urls, author = expand_collection(
                "https://space.bilibili.com/353711/lists/5833069")
            assert urls == ["https://www.bilibili.com/video/BV100"]
            assert author == "TestUP"

    def test_empty_collection(self):
        with patch("yt_dlp.YoutubeDL", return_value=_mock_ydl([])):
            urls, author = expand_collection(
                "https://space.bilibili.com/123/lists/456?type=season")
            assert urls == []
            assert author == "TestUP"

    def test_non_collection_url_returns_empty(self):
        assert expand_collection("https://www.bilibili.com/video/BV123") == ([], "")
        assert expand_collection("https://www.youtube.com/watch?v=abc") == ([], "")

    def test_ytdlp_error_returns_empty(self):
        mock = MagicMock()
        mock.__enter__.return_value = mock
        mock.extract_info.side_effect = Exception("yt-dlp failed")
        with patch("yt_dlp.YoutubeDL", return_value=mock):
            urls, author = expand_collection(
                "https://space.bilibili.com/1/lists/1?type=season")
            assert urls == []
            assert author == ""

    def test_filters_out_none_ids(self):
        with patch("yt_dlp.YoutubeDL", return_value=_mock_ydl(["BV001", None, "BV002"])):
            urls, _ = expand_collection(
                "https://space.bilibili.com/1/lists/1?type=season")
            assert urls == [
                "https://www.bilibili.com/video/BV001",
                "https://www.bilibili.com/video/BV002",
            ]

    def test_missing_uploader(self):
        with patch("yt_dlp.YoutubeDL", return_value=_mock_ydl(["BV001"], uploader="")):
            _, author = expand_collection(
                "https://space.bilibili.com/1/lists/1?type=season")
            assert author == ""
