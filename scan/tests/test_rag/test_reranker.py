"""Tests for RAG reranker module."""
from unittest.mock import patch

import pytest

from src.rag.reranker import rerank


class TestRerank:
    def test_returns_all_when_candidates_leq_top_n(self):
        candidates = [
            {"id": "1", "text": "a"},
            {"id": "2", "text": "b"},
        ]
        result = rerank("query", candidates, top_n=3)
        assert result == candidates

    def test_truncates_to_top_n(self):
        with patch("src.rag.reranker._get_reranker") as mock_get:
            mock_model = mock_get.return_value
            mock_model.predict.return_value = [0.1, 0.9, 0.5, 0.3]
            candidates = [
                {"id": "1", "text": "irrelevant"},
                {"id": "2", "text": "very relevant"},
                {"id": "3", "text": "somewhat"},
                {"id": "4", "text": "meh"},
            ]
            result = rerank("query", candidates, top_n=2)
            assert len(result) == 2
            assert result[0]["id"] == "2"  # highest score
            assert result[1]["id"] == "3"  # second highest

    def test_empty_candidates(self):
        assert rerank("query", [], top_n=5) == []

    def test_preserves_extra_fields(self):
        with patch("src.rag.reranker._get_reranker") as mock_get:
            mock_model = mock_get.return_value
            mock_model.predict.return_value = [0.3, 0.5, 0.8]
            candidates = [
                {"id": "a", "text": "x", "timestamp": 10, "source": "v1"},
                {"id": "b", "text": "y", "timestamp": 20, "source": "v2"},
                {"id": "c", "text": "z", "timestamp": 30, "source": "v3"},
            ]
            result = rerank("q", candidates, top_n=2)
            assert len(result) == 2
            assert result[0]["source"] == "v3"  # highest score (0.8)
            assert result[1]["source"] == "v2"  # second (0.5)
