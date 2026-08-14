from src.preprocess.segmenter import merge_short, segment, build_text
from src.models import SubtitleEntry


def _e(start: float, end: float, text: str) -> SubtitleEntry:
    return SubtitleEntry(start=start, end=end, text=text)


class TestMergeShort:
    def test_empty(self):
        assert merge_short([]) == []

    def test_all_long_unchanged(self):
        entries = [_e(0, 1, "hello"), _e(1, 2, "world")]
        result = merge_short(entries, min_chars=3)
        assert len(result) == 2

    def test_short_merged(self):
        entries = [_e(0, 1, "hi"), _e(1, 2, "world")]
        result = merge_short(entries, min_chars=5)
        assert len(result) == 1
        assert "hi" in result[0].text
        assert "world" in result[0].text

    def test_single_short_preserved(self):
        entries = [_e(0, 1, "hi")]
        result = merge_short(entries, min_chars=5)
        assert len(result) == 1

    def test_multiple_shorts_merged(self):
        entries = [_e(0, 1, "a"), _e(1, 2, "b"), _e(2, 3, "c")]
        result = merge_short(entries, min_chars=2)
        assert len(result) == 1
        assert result[0].text == "a b c"


class TestSegment:
    def test_empty(self):
        assert segment([]) == []

    def test_single_entry(self):
        e = _e(0, 10, "hello")
        result = segment([e])
        assert result == [[e]]

    def test_gap_exceeds_max_splits(self):
        result = segment([_e(0, 1, "a"), _e(5, 6, "b")], max_gap=2.0)
        assert len(result) == 2

    def test_gap_within_max_stays_together(self):
        result = segment([_e(0, 1, "a"), _e(2, 3, "b")], max_gap=2.0)
        assert len(result) == 1

    def test_gap_exactly_max_does_not_split(self):
        # Uses > not >=
        result = segment([_e(0, 1, "a"), _e(3, 4, "b")], max_gap=2.0)
        assert len(result) == 1

    def test_long_segment_forced_split(self):
        # Entries spanning > 300s should split regardless of gap
        entries = [_e(0, 250, "a"), _e(250, 550, "b")]
        result = segment(entries, max_gap=999)
        assert len(result) >= 2


class TestBuildText:
    def test_empty(self):
        assert build_text([]) == ""

    def test_single(self):
        assert build_text([_e(0, 1, "hello")]) == "hello"

    def test_multiple(self):
        entries = [_e(0, 1, "hello"), _e(1, 2, "world")]
        assert build_text(entries) == "hello world"
