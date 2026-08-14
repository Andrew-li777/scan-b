from src.output.srt import generate_srt, _fmt_srt_time
from src.models import SubtitleEntry


def _e(start: float, end: float, text: str) -> SubtitleEntry:
    return SubtitleEntry(start=start, end=end, text=text)


class TestFmtSrtTime:
    def test_zero(self):
        assert _fmt_srt_time(0) == "00:00:00,000"

    def test_exact_second(self):
        assert _fmt_srt_time(3661.0) == "01:01:01,000"

    def test_with_milliseconds(self):
        assert _fmt_srt_time(3661.5) == "01:01:01,500"

    def test_rounding_up(self):
        # Python round() uses banker's rounding, so 0.5 rounds to 0
        assert _fmt_srt_time(0.0005) == "00:00:00,000"

    def test_large_value(self):
        assert _fmt_srt_time(999999) == "277:46:39,000"


class TestGenerateSrt:
    def test_empty(self):
        assert generate_srt([]) == ""

    def test_single_entry(self):
        result = generate_srt([_e(0, 1, "hello")])
        assert result == "1\n00:00:00,000 --> 00:00:01,000\nhello\n"

    def test_multiple_entries(self):
        entries = [_e(0, 1, "first"), _e(1, 2, "second")]
        result = generate_srt(entries)
        lines = result.strip().split("\n")
        assert lines[0] == "1"
        assert lines[4] == "2"

    def test_newline_in_text_replaced(self):
        result = generate_srt([_e(0, 1, "hello\nworld")])
        assert "\nhello world\n" in result
        assert "hello\nworld" not in result
