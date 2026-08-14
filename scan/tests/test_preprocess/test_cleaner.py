from src.preprocess.cleaner import clean
from src.models import SubtitleEntry


def _e(text: str) -> SubtitleEntry:
    return SubtitleEntry(start=0.0, end=1.0, text=text)


class TestClean:
    def test_empty_list(self):
        assert clean([]) == []

    def test_html_tags_stripped(self):
        result = clean([_e("<b>hello</b>")])
        assert result[0].text == "hello"

    def test_music_markers_removed(self):
        cases = ["[音乐]hello", "hello[掌声]", "[Music]intro", "(音乐)bgm", "[Applause]"]
        for c in cases:
            r = clean([_e(c)])
            if r:
                assert "音乐" not in r[0].text and "掌声" not in r[0].text

    def test_whitespace_collapsed(self):
        result = clean([_e("hello   world")])
        assert result[0].text == "hello world"

    def test_traditional_to_simplified(self):
        result = clean([_e("繁體字")])
        assert result[0].text == "繁体字"

    def test_pure_tag_filtered_out(self):
        result = clean([_e("<b></b>")])
        assert len(result) == 0

    def test_pure_music_marker_filtered_out(self):
        result = clean([_e("[音乐]")])
        assert len(result) == 0

    def test_empty_text_filtered_out(self):
        result = clean([_e("")])
        assert len(result) == 0

    def test_normal_text_unchanged(self):
        text = "Hello, world!"
        result = clean([_e(text)])
        assert result[0].text == text
