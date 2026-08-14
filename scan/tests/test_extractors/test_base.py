from src.extractors.base import clean_url


class TestCleanUrl:
    def test_bilibili_tracking_stripped(self):
        url = "https://www.bilibili.com/video/BV1xx411c7mD?vd_source=abc123&spm_id_from=333.337"
        result = clean_url(url)
        assert "vd_source" not in result
        assert "spm_id_from" not in result

    def test_youtube_tracking_stripped(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=abc123&feature=shared"
        result = clean_url(url)
        assert "si=" not in result
        assert "feature=" not in result

    def test_utm_params_stripped(self):
        url = "https://example.com/page?utm_source=twitter&utm_medium=social&q=hello"
        result = clean_url(url)
        assert "utm_source" not in result
        assert "utm_medium" not in result
        assert "q=hello" in result

    def test_no_tracking_unchanged(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert clean_url(url) == url

    def test_empty_string(self):
        assert clean_url("") == ""

    def test_no_query_params(self):
        url = "https://example.com/video"
        assert clean_url(url) == url
