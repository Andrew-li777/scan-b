import hashlib
import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from src.cache import cache
from src.output.markdown import _OUTPUT_DIR
from src.rag.ids import legacy_ids, task_id_for_url
from src.urlutil import https_url
from src.web.roles import ADMIN, GUEST, USER, can_view_record, viewer

logger = logging.getLogger(__name__)

router = APIRouter()


def _viewer_of(request) -> tuple[str, str]:
    """请求的 (账号, 角色)；无 request（内部调用/单测）时按游客处理。"""
    if request is None:
        return "", GUEST
    return viewer(request)


def meta_for_path(rel: str) -> dict:
    """给定记录路径（目录**或目录里的文件**），向上找最近的 meta.json 并返回其内容。

    download 接口拿到的是 `标题/overview.md` 这种文件路径，而可见性记在记录目录的
    meta.json 上，所以要往上找（最多 4 层，避免误爬到输出根目录）。
    """
    target = find_in_roots(rel, want_dir=True) or find_in_roots(rel)
    if target is None:
        return {}
    folder = target if target.is_dir() else target.parent
    for _ in range(4):
        mj = folder / "meta.json"
        if mj.exists():
            try:
                return json.loads(mj.read_text(encoding="utf-8"))
            except Exception:
                return {}
        parent = folder.parent
        if parent == folder:
            break
        folder = parent
    return {}


def visible_for_role(role: str, rel: str) -> bool:
    """这条记录（或它下面的文件）对 role 可见吗？

    找不到记录 / 读不到 meta → 按 public 放行（与"存量记录无归属即 public"同口径）；
    真正的路径安全由 resolve_within 负责，这里只管可见性。
    """
    if role == ADMIN:
        return True
    return can_view_record(role, meta_for_path(rel))


def _rag_ids(url: str) -> dict:
    """记录级 RAG 检索标识：集合名从 meta.json 的 url 派生（见 src/rag/ids.py）。

    task_id  = 前端问答要带的集合名
    legacy   = 升级前的历史集合名候选，仅作排查提示
    """
    return {
        "task_id": task_id_for_url(url),
        "legacy": legacy_ids(url),
    }


def scan_dirs():
    """Return list of output root dirs to scan for history records."""
    old_root = Path(__file__).parent.parent.parent / "output"
    dirs = {}
    for root in (_OUTPUT_DIR, old_root):
        if root.exists():
            dirs[str(root)] = root
    return list(dirs.values())


def resolve_within(root: Path, rel: str) -> Path | None:
    """把相对路径安全解析到 root 之内；越界（../）或解析失败返回 None。

    download() / _resolve_folder() / _delete_one() 原来各抄了一份 resolve + 前缀校验，
    安全校验这种东西抄两份就会"改一处漏一处"，统一收拢到这里。
    """
    try:
        resolved_root = root.resolve()
        target = (resolved_root / rel).resolve()
    except Exception:
        return None
    if target == resolved_root:
        return None
    if not str(target).startswith(str(resolved_root) + os.sep):
        return None
    return target


def find_in_roots(rel: str, *, want_dir: bool = False) -> Path | None:
    """在全部历史根目录里安全定位一个条目（默认要文件，want_dir=True 要目录）。"""
    for root in scan_dirs():
        target = resolve_within(root, rel)
        if target is None:
            continue
        if (target.is_dir() if want_dir else target.exists()):
            return target
    return None


def _resolve_folder(base_name: str) -> Path | None:
    """Safely resolve a base_name to an absolute folder path, or None if not found / unsafe."""
    return find_in_roots(base_name, want_dir=True)


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


def _load_detail(base_name: str, *, with_subtitles: bool = True,
                 offset: int = 0, limit: int = 0) -> dict | None:
    """Load full detail data for a history record. Returns None if the folder doesn't exist.

    字幕是详情里唯一的「大块头」（一条长视频的 subtitles.md 可达几十 KB，占整个
    详情 JSON 的 3/4）。所以这里把「解析」和「回传」拆开：
      * **总是解析** —— 必须准确回答 `subtitle_count` 和 `translated`，服务端读 33KB
        文本是毫秒级，瓶颈在 1Mbps 出网，省字节比省 CPU 有意义；
      * **按需回传** —— `with_subtitles=False` 时只给条数不给正文；给了 `limit`
        就只回 `[offset, offset+limit)` 这一段。
    """
    folder = _resolve_folder(base_name)
    if folder is None:
        return None

    result = {
        "base_name": base_name, "title": base_name,
        "subtitles": [], "overview": "", "keypoints": [],
        "thumbnail": "", "category": "", "tags": [],
        "platform": "", "author": "", "duration": "", "url": "",
        "mindmap": "",
        # 行级可见性三件套（旧记录没有这三个键 → 空值 → 判为 public）
        "owner": "", "owner_role": "", "visibility": "",
    }

    # Load meta.json
    mj = folder / "meta.json"
    if mj.exists():
        try:
            meta = json.loads(mj.read_text(encoding="utf-8"))
            result["title"] = meta.get("title", base_name)
            # 归一为 https：仓库里的旧记录存的是 http://iX.hdslb.com/...，
            # 而线上 Caddy 下发的 CSP 只放行 https://*.hdslb.com → 直接显示会被拒载。
            result["thumbnail"] = https_url(meta.get("thumbnail", ""))
            result["platform"] = meta.get("platform", "")
            result["author"] = meta.get("author", "")
            result["duration"] = meta.get("duration", "")
            result["url"] = meta.get("url", "")
            result["category"] = meta.get("category", "")
            result["tags"] = meta.get("tags", [])
            result["owner"] = meta.get("owner", "")
            result["owner_role"] = meta.get("owner_role", "")
            result["visibility"] = meta.get("visibility", "")
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
    sub_all: list[str] = []
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
                sub_all.append(line + ("\n" + zh if zh else ""))
            i += 1

    total = len(sub_all)
    if with_subtitles:
        lo = max(0, min(offset, total))
        hi = total if limit <= 0 else min(total, lo + max(0, limit))
        result["subtitles"] = sub_all[lo:hi]
    else:
        lo, hi = 0, 0
        result["subtitles"] = []
    result["subtitle_count"] = total          # 总条数（不受分页影响）
    result["subtitle_offset"] = lo            # 本段起始下标
    result["has_more_subtitles"] = hi < total  # 还有没拉的

    mm = folder / "mindmap.md"
    if mm.exists():
        text = mm.read_text(encoding="utf-8", errors="replace")
        if "```mermaid" in text:
            result["mindmap"] = text.split("```mermaid", 1)[1].split("```", 1)[0].strip()
        else:
            result["mindmap"] = text.strip()

    result["translated"] = any("\n" in s for s in sub_all)
    result["rag"] = _rag_ids(result.get("url", ""))

    return result


@router.get("/api/history")
def history(request: Request):
    _user, role = _viewer_of(request)
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
        owner = owner_role = visibility = ""
        rag = _rag_ids("")
        mj = folder / "meta.json"
        if mj.exists():
            try:
                md = json.loads(mj.read_text(encoding="utf-8"))
                created_at = md.get("created_at", mtime)
                tags = md.get("tags", [])
                category = md.get("category", "")
                owner = md.get("owner", "")
                owner_role = md.get("owner_role", "")
                visibility = md.get("visibility", "")
                rag = _rag_ids(md.get("url", ""))
            except Exception:
                pass

        # 行级可见性：看不见的记录直接从列表里消失（不是灰掉/报错）——
        # 与"详情/导图/下载返回 404"是同一规则（can_view_record）。
        if not can_view_record(role, {"owner": owner, "owner_role": owner_role,
                                      "visibility": visibility}):
            continue

        items.append({
            "base_name": rel,
            "title": title or folder.name,
            "time": created_at,
            "path": str(root),
            "tags": tags,
            "category": category,
            "owner": owner,
            "owner_role": owner_role,
            "visibility": visibility,
            "rag": rag,
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


def _purge_index(folder: Path, base_name: str) -> dict:
    """删记录时同步清掉它的向量：per-video 集合 + 全局集合里该记录的条目。

    旧代码是 `from src.rag.vectorstore import _client; _client.delete_collection(base_name)`：
      ① `_client` 在导入那一刻把值绑死，未初始化时是 None → AttributeError；
      ② base_name（如 `生信小兮_2026-09-28/[P9]_【…】`）含 `/` 与中文，Chroma 集合名规则
         `^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$`（3-63 字符）必然拒绝 → ValueError；
      ③ 即使合法也对不上：写入用 URL 派生的 task_id，删除却用目录名。
    三条叠加，全被 `except Exception: pass` 吞掉 → 索引从来没被清过，
    全局问答会一直召回已删记录的内容。
    """
    meta: dict = {}
    try:
        mj = folder / "meta.json"
        if mj.exists():
            meta = json.loads(mj.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    tid = task_id_for_url(meta.get("url") or "")
    info = {"task_id": tid, "collection_removed": False, "global_removed": 0}
    # 缓存也要按两种键各删一次：写缓存用的是 task_id，历史里的老代码删的是 base_name
    for key in {base_name, tid}:
        if key:
            try:
                cache.delete(key)
            except Exception:
                pass
    try:
        from src.rag.vectorstore import delete_index, forget_source

        info["collection_removed"] = delete_index(tid)
        info["global_removed"] = forget_source(base_name)
        logger.info("delete_history: 索引已清理 %s", info)
    except Exception as e:  # 索引清理失败不应阻断删记录，但必须留痕
        logger.warning("delete_history: 索引清理失败 task_id=%s: %s", tid, e)
    return info


def _delete_one(base_name: str) -> None:
    """Delete a single history record (folder + cache + chromadb collection).
    Raises FileNotFoundError if the record doesn't exist.
    """
    import shutil
    folder = find_in_roots(base_name, want_dir=True)
    if folder is not None:
        _purge_index(folder, base_name)
        shutil.rmtree(folder)
        return
    raise FileNotFoundError(base_name)


def _can_delete(user: str, role: str, base_name: str) -> bool:
    """这条记录能不能被当前身份删掉？

    管理员全可删；常规用户**只能删自己建的**（meta.json 的 owner 等于本人）。
    存量无归属记录（owner 为空）常规用户删不掉 —— 它们不属于任何人。
    """
    if role == ADMIN:
        return True
    if role != USER or not user:
        return False
    md = meta_for_path(base_name)
    return (md.get("owner") or "") == user


@router.delete("/api/history/{base_name:path}")
def delete_history(request: Request, base_name: str):
    user, role = _viewer_of(request)
    if not _can_delete(user, role, base_name):
        # 不区分"不存在"与"不是你的"：一律 404，避免暴露别人的记录名是否存在
        raise HTTPException(404, "记录不存在")
    try:
        _delete_one(base_name)
    except FileNotFoundError:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


class BatchDeleteRequest(BaseModel):
    base_names: list[str]


@router.post("/api/history/batch_delete")
def batch_delete(request: Request, req: BatchDeleteRequest):
    if not req.base_names:
        raise HTTPException(400, "未选择记录")
    if len(req.base_names) > 200:
        raise HTTPException(400, "单次最多 200 条")
    user, role = _viewer_of(request)
    deleted: list[str] = []
    failed: list[dict] = []
    for bn in req.base_names:
        # 别人的记录：整批不报 403，逐条进 failed（前端一次勾选里混着别人的是常态）
        if not _can_delete(user, role, bn):
            failed.append({"base_name": bn, "error": "只能删除自己解析的记录" if role == USER else "权限不足"})
            continue
        try:
            _delete_one(bn)
            deleted.append(bn)
        except FileNotFoundError:
            failed.append({"base_name": bn, "error": "记录不存在"})
        except Exception as e:
            failed.append({"base_name": bn, "error": str(e)})
    return {"deleted": deleted, "failed": failed}


def _detail_etag(folder: Path, extra: str = "") -> str:
    """详情响应指纹 = 记录目录里各 md 的 (mtime, size) + 角色/分页参数。

    用 mtime+size 而不是内容哈希：读者要的是「这份文件没变」，而 stat 是 O(1)，
    对一个 33KB 的 subtitles.md 也省掉一次全量读取。角色进指纹是防止
    「管理员看过的 304 被游客复用」这类跨身份缓存串味。
    """
    parts = []
    for name in ("meta.json", "overview.md", "keypoints.md", "mindmap.md", "subtitles.md"):
        p = folder / name
        try:
            st = p.stat()
            parts.append(f"{name}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            parts.append(f"{name}:-")
    parts.append(extra)
    return '"' + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:24] + '"'


@router.get("/api/history/{base_name:path}")
def history_detail(request: Request, base_name: str, response: Response,
                   subtitles: int = 1, offset: int = 0, limit: int = 0):
    """记录详情。

    字幕参数（默认全量，保持老前端/脚本兼容）：
      ?subtitles=0                    只回条数，不回正文（新前端首屏用）
      ?subtitles=1&offset=&limit=     分页取字幕
    ⚠️ 不能用 `/api/history/{base}/subtitles` 这种子路径 —— `{base_name:path}`
    是贪婪匹配，会把 `subtitles` 一起吞进 base_name，直接 404。
    """
    folder = _resolve_folder(base_name)
    if folder is None:
        raise HTTPException(404, "记录不存在")
    detail = _load_detail(base_name, with_subtitles=bool(subtitles),
                          offset=offset, limit=limit)
    if detail is None:
        raise HTTPException(404, "记录不存在")
    _user, role = _viewer_of(request)
    if not can_view_record(role, detail):
        # 不可见 = 不存在（404，不是 403）——403 等于承认"这条记录存在但不给你看"
        raise HTTPException(404, "记录不存在")

    etag = _detail_etag(folder, f"{role}|{int(bool(subtitles))}|{offset}|{limit}")
    # If-None-Match 可能是逗号列表、可能带 W/ 前缀（反向代理会降级为弱校验），都要认
    inm = request.headers.get("if-none-match", "")
    if inm and (inm.strip() == "*" or etag in {t.strip().removeprefix("W/").strip() for t in inm.split(",")}):
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    # private：内容跟身份有关，绝不能让 CF/中间代理共享缓存；no-cache 仍允许本地
    # 保存副本，但每次用前必须回源校验（命中就是 304，省掉正文那几个 KB）。
    response.headers["Cache-Control"] = "private, no-cache"
    return detail
