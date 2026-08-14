"""Tests for mindmap generation helpers."""
from unittest.mock import patch

from src.analyzers.mindmap import _extract_mermaid, _fmt_ts, generate_mindmap


class TestExtractMermaid:
    def test_extracts_mermaid_code_block(self):
        text = "before\n```mermaid\nmindmap\n  root((A))\n```\nafter"
        assert _extract_mermaid(text) == "mindmap\n  root((A))"

    def test_extracts_plain_code_block(self):
        text = "```\nmindmap\n  root((A))\n```"
        assert _extract_mermaid(text) == "mindmap\n  root((A))"

    def test_returns_stripped_text_without_fence(self):
        assert _extract_mermaid("  mindmap\n  root((A))  ") == "mindmap\n  root((A))"


class TestFormatTimestamp:
    def test_formats_minutes_seconds(self):
        assert _fmt_ts(65) == "1:05"


class TestGenerateMindmap:
    def test_generate_mindmap_calls_llm_and_extracts_mermaid(self):
        with patch("src.analyzers.mindmap.chat", return_value="```mermaid\nmindmap\n  root((Test))\n```") as mock_chat:
            result = generate_mindmap(
                "Test Video",
                "A summary",
                [{"timestamp": 60, "content": "Key point"}],
            )
            assert result == "mindmap\n  root((Test))"
            args, kwargs = mock_chat.call_args
            assert "Test Video" in args[1]
            assert "[1:00] Key point" in args[1]
            assert kwargs["max_tokens"] == 2048
