"""Tests for batch history/export endpoints.

These tests use FastAPI TestClient against the real app, but redirect
scan_dirs() to a tmp directory so we never touch the user's actual history.
"""
import json
import shutil
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fake_vault(monkeypatch):
    """Point history.scan_dirs() at a fresh tmp folder. Auto-cleans on teardown.

    We use tempfile.mkdtemp() rather than pytest's tmp_path because the user's
    pytest tmp dir has perm issues on this Windows machine.
    """
    from src.web import history as hist_mod

    d = Path(tempfile.mkdtemp(prefix="va_test_"))
    monkeypatch.setattr(hist_mod, "scan_dirs", lambda: [d])
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def client():
    from src.web.server import app
    return TestClient(app)


def _seed_record(vault: Path, name: str) -> str:
    """Create a minimal valid history folder. Returns the base_name."""
    folder = vault / name
    folder.mkdir(parents=True)
    (folder / "overview.md").write_text(f"# {name}\n\ntest content\n", encoding="utf-8")
    (folder / "keypoints.md").write_text("| 时间 | 内容 | 重要度 |\n|---|---|---|\n| 0:01 | x | ★ |\n", encoding="utf-8")
    (folder / "subtitles.md").write_text("[0:00] hello\n", encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"title": name, "url": "", "platform": "youtube"}), encoding="utf-8")
    return name


class TestBatchDelete:
    def test_empty_list_returns_400(self, client, fake_vault):
        r = client.post("/api/history/batch_delete", json={"base_names": []})
        assert r.status_code == 400

    def test_oversize_returns_400(self, client, fake_vault):
        r = client.post("/api/history/batch_delete", json={"base_names": ["x"] * 201})
        assert r.status_code == 400

    def test_missing_records_all_failed(self, client, fake_vault):
        r = client.post("/api/history/batch_delete",
                        json={"base_names": ["nope1", "nope2"]})
        assert r.status_code == 200
        d = r.json()
        assert d["deleted"] == []
        assert len(d["failed"]) == 2
        assert all(f["error"] == "记录不存在" for f in d["failed"])

    def test_mixed_existing_and_missing(self, client, fake_vault):
        a = _seed_record(fake_vault, "vid_a")
        b = _seed_record(fake_vault, "vid_b")
        r = client.post("/api/history/batch_delete",
                        json={"base_names": [a, "nope", b]})
        assert r.status_code == 200
        d = r.json()
        assert sorted(d["deleted"]) == sorted([a, b])
        assert len(d["failed"]) == 1
        assert d["failed"][0]["base_name"] == "nope"
        # Folders gone
        assert not (fake_vault / a).exists()
        assert not (fake_vault / b).exists()

    def test_idempotent_double_delete(self, client, fake_vault):
        a = _seed_record(fake_vault, "vid_dup")
        client.post("/api/history/batch_delete", json={"base_names": [a]})
        r = client.post("/api/history/batch_delete", json={"base_names": [a]})
        assert r.status_code == 200
        assert r.json()["deleted"] == []
        assert len(r.json()["failed"]) == 1


class TestBatchExport:
    def test_empty_list_returns_400(self, client, fake_vault):
        r = client.post("/api/export/batch", json={"base_names": []})
        assert r.status_code == 400

    def test_oversize_returns_400(self, client, fake_vault):
        r = client.post("/api/export/batch",
                        json={"base_names": ["x"] * 51, "format": "pdf"})
        assert r.status_code == 400

    def test_unsupported_format_returns_400(self, client, fake_vault):
        r = client.post("/api/export/batch",
                        json={"base_names": ["anything"], "format": "docx"})
        assert r.status_code == 400

    def test_all_missing_produces_zip_with_failed_json(self, client, fake_vault, monkeypatch):
        # Stub out PDF generator to skip Chrome dependency.
        from src.web import server as srv
        monkeypatch.setattr("src.output.pdf.generate_pdf_from_history",
                            lambda bn, backend="auto": None)
        r = client.post("/api/export/batch",
                        json={"base_names": ["nope1", "nope2"], "format": "pdf"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(BytesIO(r.content))
        names = zf.namelist()
        assert "_failed.json" in names
        failed = json.loads(zf.read("_failed.json"))
        assert len(failed) == 2
        assert all(f["error"] == "记录不存在" for f in failed)

    def test_successful_pdf_packed_in_zip(self, client, fake_vault, monkeypatch):
        # Stub PDF generator to return a fake "PDF" blob deterministically.
        monkeypatch.setattr("src.output.pdf.generate_pdf_from_history",
                            lambda bn, backend="auto": b"%PDF-1.4 fake " + bn.encode())
        r = client.post("/api/export/batch",
                        json={"base_names": ["a/video_x", "b/video_y"], "format": "pdf"})
        assert r.status_code == 200
        zf = zipfile.ZipFile(BytesIO(r.content))
        names = sorted(zf.namelist())
        assert "video_x.pdf" in names
        assert "video_y.pdf" in names
        assert "_failed.json" not in names
        # Content sanity
        assert zf.read("video_x.pdf").startswith(b"%PDF-1.4")

    def test_duplicate_basenames_get_suffix(self, client, fake_vault, monkeypatch):
        monkeypatch.setattr("src.output.pdf.generate_pdf_from_history",
                            lambda bn, backend="auto": b"%PDF-1.4")
        # Two records whose last segment collides
        r = client.post("/api/export/batch",
                        json={"base_names": ["playlist_a/clip", "playlist_b/clip"], "format": "pdf"})
        assert r.status_code == 200
        zf = zipfile.ZipFile(BytesIO(r.content))
        names = sorted(zf.namelist())
        assert "clip.pdf" in names
        assert "clip_1.pdf" in names
