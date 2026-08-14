from src.extractors import get_extractor
from src.extractors.bilibili import BilibiliExtractor
from src.extractors.youtube import YouTubeExtractor


class TestBilibiliMatch:
    def setup_method(self):
        self.extractor = BilibiliExtractor()

    def test_standard_https(self):
        assert self.extractor.match("https://www.bilibili.com/video/BV1xx411c7mD") is True

    def test_http(self):
        assert self.extractor.match("http://www.bilibili.com/video/BV1xx411c7mD") is True

    def test_without_www(self):
        assert self.extractor.match("https://bilibili.com/video/BV1xx411c7mD") is True

    def test_without_protocol(self):
        assert self.extractor.match("www.bilibili.com/video/BV1xx411c7mD") is True

    def test_with_query_params(self):
        assert self.extractor.match("https://www.bilibili.com/video/BV1xx411c7mD?p=2&spm_id_from=333.337") is True

    def test_av_id_not_supported(self):
        assert self.extractor.match("https://www.bilibili.com/video/av170001") is False

    def test_b23tv_shortlink(self):
        assert self.extractor.match("https://b23.tv/BV1xx411c7mD") is False

    def test_non_bilibili(self):
        assert self.extractor.match("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False

    def test_empty_string(self):
        assert self.extractor.match("") is False


class TestYouTubeMatch:
    def setup_method(self):
        self.extractor = YouTubeExtractor()

    def test_standard_watch(self):
        assert self.extractor.match("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is True

    def test_youtu_dot_be(self):
        assert self.extractor.match("https://youtu.be/dQw4w9WgXcQ") is True

    def test_embed_url(self):
        assert self.extractor.match("https://www.youtube.com/embed/dQw4w9WgXcQ") is True

    def test_with_query_params(self):
        assert self.extractor.match("https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=abc123&feature=shared") is True

    def test_non_youtube(self):
        assert self.extractor.match("https://www.bilibili.com/video/BV1xx411c7mD") is False


class TestGetExtractor:
    def test_youtube_url(self):
        ex = get_extractor("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert isinstance(ex, YouTubeExtractor)

    def test_youtu_dot_be(self):
        ex = get_extractor("https://youtu.be/dQw4w9WgXcQ")
        assert isinstance(ex, YouTubeExtractor)

    def test_bilibili_url(self):
        ex = get_extractor("https://www.bilibili.com/video/BV1xx411c7mD")
        assert isinstance(ex, BilibiliExtractor)

    def test_unsupported_platform(self):
        import pytest
        with pytest.raises(ValueError, match="不支持该视频平台"):
            get_extractor("https://vimeo.com/12345")
