"""Tests for Phase 4 web endpoints."""
import json
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fake_vault(monkeypatch):
    from src.web import history as hist_mod

    d = Path(tempfile.mkdtemp(prefix="va_phase4_"))
    monkeypatch.setattr(hist_mod, "scan_dirs", lambda: [d])
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def client():
    from src.web.server import app
    return TestClient(app)


def _seed_record(vault: Path, name: str, mindmap: str = "") -> str:
    folder = vault / name
    folder.mkdir(parents=True)
    (folder / "overview.md").write_text("# Test\n\nOverview text\n", encoding="utf-8")
    (folder / "keypoints.md").write_text("| 时间 | 内容 | 重要度 |\n|---|---|---|\n| 0:01 | point | ★ |\n", encoding="utf-8")
    (folder / "subtitles.md").write_text("[0:00] hello\n", encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"title": name}), encoding="utf-8")
    if mindmap:
        (folder / "mindmap.md").write_text(f"# {name}\n\n```mermaid\n{mindmap}\n```\n", encoding="utf-8")
    return name


class TestMindmapEndpoint:
    def test_returns_stored_mindmap(self, client, fake_vault):
        base = _seed_record(fake_vault, "video_a", "mindmap\n  root((A))")
        r = client.get(f"/api/mindmap/{base}")
        assert r.status_code == 200
        assert r.json()["mermaid"] == "mindmap\n  root((A))"

    def test_missing_record_returns_404(self, client, fake_vault):
        r = client.get("/api/mindmap/nope")
        assert r.status_code == 404


class TestCompareEndpoint:
    def test_requires_two_videos(self, client):
        r = client.post("/api/compare", json={"base_names": ["one"]})
        assert r.status_code == 400

    def test_rejects_more_than_five(self, client):
        r = client.post("/api/compare", json={"base_names": ["a", "b", "c", "d", "e", "f"]})
        assert r.status_code == 400

    def test_returns_comparison(self, client, monkeypatch):
        monkeypatch.setattr("src.analyzers.comparator.compare_videos", lambda base_names: "## 对比结果")
        r = client.post("/api/compare", json={"base_names": ["a", "b"]})
        assert r.status_code == 200
        assert r.json()["comparison"] == "## 对比结果"
