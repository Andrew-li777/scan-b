"""Tests for multi-video comparator helpers."""
from unittest.mock import patch

import pytest

from src.analyzers.comparator import (
    _build_compare_prompt,
    _extract_summary,
    _fmt_ts,
    compare_videos,
)


class TestExtractSummary:
    def test_strips_frontmatter_and_noise(self):
        md = """---
title: Test
---
# Title

**来源**: youtube | **作者**: a
**原链**: https://x

---

This is the summary.

More details.
"""
        assert _extract_summary(md).startswith("This is the summary.")

    def test_limits_length(self):
        text = "a" * 1000
        assert len(_extract_summary(text, max_len=100)) == 100


class TestBuildPrompt:
    def test_includes_title_summary_and_keypoints(self):
        prompt = _build_compare_prompt([
            {
                "base_name": "v1",
                "title": "Video 1",
                "summary": "Summary 1",
                "keypoints": [{"timestamp": 65, "content": "Point 1"}],
            }
        ])
        assert "Video 1" in prompt
        assert "Summary 1" in prompt
        assert "[1:05] Point 1" in prompt

    def test_fmt_ts_supports_hours(self):
        assert _fmt_ts(3661) == "1:01:01"


class TestCompareVideos:
    def test_requires_at_least_two(self):
        with pytest.raises(ValueError, match="至少"):
            compare_videos(["one"])

    def test_rejects_too_many(self):
        with pytest.raises(ValueError, match="最多"):
            compare_videos(["a", "b", "c", "d", "e", "f"])

    def test_missing_record_raises(self):
        with patch("src.analyzers.comparator._load_detail", return_value=None):
            with pytest.raises(ValueError, match="记录不存在"):
                compare_videos(["a", "b"])

    def test_calls_llm_with_loaded_details(self):
        detail = {
            "title": "Test Video",
            "overview": "---\n---\nA summary here.",
            "keypoints": [{"timestamp": 60, "content": "Key point 1"}],
        }
        with patch("src.analyzers.comparator._load_detail", return_value=detail):
            with patch("src.analyzers.comparator.chat", return_value="Comparison result") as mock_chat:
                result = compare_videos(["vid1", "vid2"])
                assert result == "Comparison result"
                args, kwargs = mock_chat.call_args
                assert "Test Video" in args[1]
                assert kwargs["max_tokens"] == 4096
