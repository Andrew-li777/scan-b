"""Tests for Bilibili multi-P (分P) expansion and extract ?p=N support (2026-09-22)."""
from unittest.mock import patch

from src.extractors.bilibili import BilibiliExtractor, expand_pages


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _pagelist(pages):
    return _FakeResp({"code": 0, "data": pages})


def _view(payload):
    return _FakeResp({"code": 0, "data": payload})


_NO_SUBS = _FakeResp({"data": {"subtitle": {"subtitles": []}}})


class TestExpandPages:
    def test_multi_p_expands(self):
        pages = [
            {"cid": 1, "part": "第一节", "duration": 100},
            {"cid": 2, "part": "第二节", "duration": 200},
            {"cid": 3, "part": "第三节", "duration": 300},
        ]
        view = _view({"owner": {"name": "UP主"}, "title": "T"})
        with patch("src.extractors.bilibili._retry_get",
                   side_effect=[_pagelist(pages), view]):
            urls, author = expand_pages("https://www.bilibili.com/video/BV1xx/?p=1")
        assert urls == [
            "https://www.bilibili.com/video/BV1xx?p=1",
            "https://www.bilibili.com/video/BV1xx?p=2",
            "https://www.bilibili.com/video/BV1xx?p=3",
        ]
        assert author == "UP主"

    def test_single_p_returns_empty(self):
        with patch("src.extractors.bilibili._retry_get",
                   return_value=_pagelist([{"cid": 1}])):
            assert expand_pages("https://www.bilibili.com/video/BV1xx/") == ([], "")

    def test_non_bili_url(self):
        assert expand_pages("https://www.youtube.com/watch?v=abc") == ([], "")


class TestParsePage:
    def test_default(self):
        assert BilibiliExtractor._parse_page("https://www.bilibili.com/video/BV1xx/") == 1

    def test_p3(self):
        assert BilibiliExtractor._parse_page(
            "https://www.bilibili.com/video/BV1xx/?p=3") == 3

    def test_p0_clamped(self):
        assert BilibiliExtractor._parse_page(
            "https://www.bilibili.com/video/BV1xx/?p=0") == 1


class TestExtractPage:
    def test_extract_p3_duration_and_title(self):
        pages = [
            {"cid": 10, "duration": 100},
            {"cid": 20, "duration": 200},
            {"cid": 30, "duration": 300},
        ]
        info = {"cid": 10, "aid": 1, "title": "视频标题", "duration": 600,
                "pages": pages, "owner": {"name": "UP"}, "pic": ""}

        def _fake_get(url, params=None, max_retries=3):
            if "web-interface/view" in url:
                return _view(info)
            return _NO_SUBS

        with patch("src.extractors.bilibili._retry_get", side_effect=_fake_get):
            meta = BilibiliExtractor().extract(
                "https://www.bilibili.com/video/BV1xx/?p=3")
        assert meta.duration == 300          # P3 时长，而非 600 总时长
        assert "（P3）" in meta.title          # 标题后缀，输出目录唯一

    def test_extract_default_is_p1(self):
        pages = [{"cid": 10, "duration": 100}, {"cid": 20, "duration": 200}]
        info = {"cid": 10, "aid": 1, "title": "视频标题", "duration": 300,
                "pages": pages, "owner": {"name": "UP"}, "pic": ""}

        def _fake_get(url, params=None, max_retries=3):
            if "web-interface/view" in url:
                return _view(info)
            return _NO_SUBS

        with patch("src.extractors.bilibili._retry_get", side_effect=_fake_get):
            meta = BilibiliExtractor().extract("https://www.bilibili.com/video/BV1xx/")
        assert meta.duration == 100
        assert "（P" not in meta.title


class TestExpandPlaylistWiring:
    def test_collection_wins_before_pages(self):
        with patch("src.extractors.expand_collection",
                   return_value=(["https://www.bilibili.com/video/BV001"], "UP")), \
             patch("src.extractors.expand_pages",
                   return_value=(["https://www.bilibili.com/video/BV002?p=1"], "UP")):
            from src.extractors import expand_playlist
            urls, author = expand_playlist("https://space.bilibili.com/1/lists/2")
        assert urls == ["https://www.bilibili.com/video/BV001"]

    def test_pages_fallback(self):
        with patch("src.extractors.expand_collection", return_value=([], "")), \
             patch("src.extractors.expand_pages",
                   return_value=(["https://www.bilibili.com/video/BV002?p=1",
                                  "https://www.bilibili.com/video/BV002?p=2"], "UP")):
            from src.extractors import expand_playlist
            urls, author = expand_playlist("https://www.bilibili.com/video/BV002/")
        assert len(urls) == 2
        assert urls[0].endswith("?p=1")
