import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.cache import cache
from src.output.markdown import _OUTPUT_DIR

router = APIRouter()


def scan_dirs():
    """Return list of output root dirs to scan for history records."""
    old_root = Path(__file__).parent.parent.parent / "output"
    dirs = {}
    for root in (_OUTPUT_DIR, old_root):
        if root.exists():
            dirs[str(root)] = root
    return list(dirs.values())


def _resolve_folder(base_name: str) -> Path | None:
    """Safely resolve a base_name to an absolute folder path, or None if not found / unsafe."""
    for root in scan_dirs():
        resolved_root = root.resolve()
        f = (resolved_root / base_name).resolve()
        if not str(f).startswith(str(resolved_root) + os.sep):
            continue
        if f.is_dir():
            return f
    return None


def _parse_keypoints_md(text: str) -> list[dict]:
    """Parse a keypoints.md markdown table into structured keypoint dicts."""
    keypoints = []
    in_table = False
    pending = None
    for line in text.split("\n"):
        if line.startswith("| 时间 |"):
            in_table = True
            pending = None
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|") and not line.startswith("| 时间"):
            parts_raw = line.split("|")
            cells = [c.strip() for c in parts_raw[1:]]
            if len(cells) >= 3:
                ts, content, stars = cells[0], cells[1], cells[2]
                importance = stars.count("★")
                timestamp = _parse_timestamp(ts)
                keypoints.append({
                    "timestamp": timestamp, "content": content, "importance": importance,
                })
                pending = None
            else:
                pending = cells
        elif in_table and not line.startswith("|"):
            if pending and len(pending) >= 1:
                tail_parts = line.strip().split("|")
                if len(tail_parts) >= 2:
                    content = (pending[1].strip() if len(pending) >= 2 else "") + tail_parts[0].strip()
                    stars = tail_parts[1].strip()
                    ts = pending[0]
                    importance = stars.count("★")
                    timestamp = _parse_timestamp(ts)
                    keypoints.append({
                        "timestamp": timestamp, "content": content, "importance": importance,
                    })
                pending = None
            else:
                break
    return keypoints


def _parse_timestamp(ts: str) -> int:
    """Parse 'M:SS' or 'H:MM:SS' into seconds."""
    parts = ts.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    return 0


def _load_detail(base_name: str) -> dict | None:
    """Load full detail data for a history record. Returns None if the folder doesn't exist."""
    folder = _resolve_folder(base_name)
    if folder is None:
        return None

    result = {
        "base_name": base_name, "title": base_name,
        "subtitles": [], "overview": "", "keypoints": [],
        "thumbnail": "", "category": "", "tags": [],
        "platform": "", "author": "", "duration": "", "url": "",
        "mindmap": "",
    }

    # Load meta.json
    mj = folder / "meta.json"
    if mj.exists():
        try:
            meta = json.loads(mj.read_text(encoding="utf-8"))
            result["title"] = meta.get("title", base_name)
            result["thumbnail"] = meta.get("thumbnail", "")
            result["platform"] = meta.get("platform", "")
            result["author"] = meta.get("author", "")
            result["duration"] = meta.get("duration", "")
            result["url"] = meta.get("url", "")
            result["category"] = meta.get("category", "")
            result["tags"] = meta.get("tags", [])
        except Exception:
            pass

    # Parse overview
    ov = folder / "overview.md"
    if ov.exists():
        text = ov.read_text(encoding="utf-8", errors="replace")
        for line in text.split("\n"):
            if line.startswith("# "):
                result["title"] = line[2:].replace(" — 内容概览", "").strip()
                break
        for line in text.split("\n"):
            if line.startswith("**来源**:"):
                parts = line.replace("**来源**: ", "").split("|")
                if len(parts) >= 2:
                    result["platform"] = parts[0].strip()
                    result["author"] = parts[1].replace("**作者**:", "").strip()
                    dur_part = parts[2] if len(parts) > 2 else ""
                    result["duration"] = dur_part.replace("**时长**:", "").strip()
            if line.startswith("**原链**:"):
                result["url"] = line.replace("**原链**: ", "").strip()
        result["overview"] = text

    # Parse keypoints
    kp = folder / "keypoints.md"
    if kp.exists():
        text = kp.read_text(encoding="utf-8", errors="replace")
        result["keypoints"] = _parse_keypoints_md(text)

    # Parse subtitles (merge timestamped line with following translation line if present)
    sub = folder / "subtitles.md"
    if sub.exists():
        text = sub.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        ts_re = re.compile(r"^\[(\d+):(\d+)(?::(\d+))?\]\s+(.+)")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if ts_re.match(line):
                # peek next non-empty line; if it's not a timestamped line, treat as translation
                zh = ""
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if nxt and not ts_re.match(nxt):
                        zh = nxt
                        i += 1
                result["subtitles"].append(line + ("\n" + zh if zh else ""))
            i += 1

    mm = folder / "mindmap.md"
    if mm.exists():
        text = mm.read_text(encoding="utf-8", errors="replace")
        if "```mermaid" in text:
            result["mindmap"] = text.split("```mermaid", 1)[1].split("```", 1)[0].strip()
        else:
            result["mindmap"] = text.strip()

    result["translated"] = any("\n" in s for s in result["subtitles"])

    return result


@router.get("/api/history")
def history():
    items = []
    folders = []  # (mtime, overview_path, folder_path, root_dir)
    for root in scan_dirs():
        for folder in root.rglob("*"):
            if not folder.is_dir():
                continue
            overview = folder / "overview.md"
            if not overview.exists():
                continue
            folders.append((overview.stat().st_mtime, overview, folder, root))

    # Deduplicate by relative path from root (keep newest by mtime)
    seen = set()
    folders.sort(key=lambda x: x[0], reverse=True)
    for mtime, overview, folder, root in folders:
        rel = str(folder.relative_to(root))
        if rel in seen:
            continue
        seen.add(rel)
        text = overview.read_text(encoding="utf-8", errors="replace")
        title = ""
        for line in text.split("\n"):
            if line.startswith("# "):
                title = line[2:].replace(" — 内容概览", "").strip()
                break

        # Read meta.json for created_at (stable timestamp), tags, category
        created_at = mtime  # fallback for records before this fix
        tags = []
        category = ""
        mj = folder / "meta.json"
        if mj.exists():
            try:
                md = json.loads(mj.read_text(encoding="utf-8"))
                created_at = md.get("created_at", mtime)
                tags = md.get("tags", [])
                category = md.get("category", "")
            except Exception:
                pass

        items.append({
            "base_name": rel,
            "title": title or folder.name,
            "time": created_at,
            "path": str(root),
            "tags": tags,
            "category": category,
            "files": {
                "subtitles": str((folder / "subtitles.md").relative_to(root)),
                "overview": str((folder / "overview.md").relative_to(root)),
                "keypoints": str((folder / "keypoints.md").relative_to(root)),
                **({"mindmap": str((folder / "mindmap.md").relative_to(root))} if (folder / "mindmap.md").exists() else {}),
            },
        })

    # Sort by created_at so new records are immune to st_mtime drift
    items.sort(key=lambda x: x["time"], reverse=True)
    return items


def _delete_one(base_name: str) -> None:
    """Delete a single history record (folder + cache + chromadb collection).
    Raises FileNotFoundError if the record doesn't exist.
    """
    import shutil
    for root in scan_dirs():
        resolved_root = root.resolve()
        folder = (resolved_root / base_name).resolve()
        if not str(folder).startswith(str(resolved_root) + os.sep):
            continue
        if folder.is_dir():
            cache.delete(base_name)
            try:
                from src.rag.vectorstore import _client
                _client.delete_collection(base_name)
            except Exception:
                pass
            shutil.rmtree(folder)
            return
    raise FileNotFoundError(base_name)


@router.delete("/api/history/{base_name:path}")
def delete_history(base_name: str):
    try:
        _delete_one(base_name)
    except FileNotFoundError:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


class BatchDeleteRequest(BaseModel):
    base_names: list[str]


@router.post("/api/history/batch_delete")
def batch_delete(req: BatchDeleteRequest):
    if not req.base_names:
        raise HTTPException(400, "未选择记录")
    if len(req.base_names) > 200:
        raise HTTPException(400, "单次最多 200 条")
    deleted: list[str] = []
    failed: list[dict] = []
    for bn in req.base_names:
        try:
            _delete_one(bn)
            deleted.append(bn)
        except FileNotFoundError:
            failed.append({"base_name": bn, "error": "记录不存在"})
        except Exception as e:
            failed.append({"base_name": bn, "error": str(e)})
    return {"deleted": deleted, "failed": failed}


@router.get("/api/history/{base_name:path}")
def history_detail(base_name: str):
    detail = _load_detail(base_name)
    if detail is None:
        raise HTTPException(404, "记录不存在")
    return detail
