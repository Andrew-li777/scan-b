from src.preprocess.dedup import deduplicate, _similarity
from src.models import SubtitleEntry


def _e(start: float, end: float, text: str) -> SubtitleEntry:
    return SubtitleEntry(start=start, end=end, text=text)


class TestSimilarity:
    def test_identical(self):
        assert _similarity("hello", "hello") == 1.0

    def test_completely_different(self):
        assert _similarity("abc", "xyz") == 0.0

    def test_partial_match(self):
        sim = _similarity("abc", "abd")
        assert sim == 2.0 / 3.0

    def test_empty_strings(self):
        assert _similarity("", "") == 1.0

    def test_empty_vs_nonempty(self):
        # An empty string has 0 matching chars out of 1
        assert _similarity("", "a") == 0.0

    def test_different_lengths(self):
        sim = _similarity("ab", "abc")
        assert sim == 2.0 / 3.0


class TestDeduplicate:
    def test_empty_list(self):
        assert deduplicate([]) == []

    def test_single_entry(self):
        e = _e(0, 10, "hello")
        assert deduplicate([e]) == [e]

    def test_adjacent_identical_merged(self):
        result = deduplicate([_e(0, 5, "hello"), _e(5, 10, "hello")])
        assert len(result) == 1
        assert result[0].start == 0
        assert result[0].end == 10
        assert result[0].text == "hello"

    def test_adjacent_different_kept(self):
        result = deduplicate([_e(0, 5, "hello"), _e(5, 10, "world")])
        assert len(result) == 2

    def test_threshold_zero_keeps_all(self):
        result = deduplicate(
            [_e(0, 5, "hello"), _e(5, 10, "hello")],
            threshold=0.0,
        )
        assert len(result) == 1  # 0.0 means ALL merge into one

    def test_threshold_one_exact_only(self):
        result = deduplicate(
            [_e(0, 5, "hello"), _e(5, 10, "hello")],
            threshold=1.0,
        )
        assert len(result) == 1  # "hello" == "hello" => similarity 1.0

    def test_threshold_one_near_duplicate_kept(self):
        result = deduplicate(
            [_e(0, 5, "hello"), _e(5, 10, "hellp")],
            threshold=1.0,
        )
        assert len(result) == 2  # not exact match
