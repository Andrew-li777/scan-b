import asyncio
import json
import os
import queue
import re
import threading
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.cache import cache
from src.config import settings
from src.output.markdown import _OUTPUT_DIR
from src.rag.chat import ask as rag_ask, global_ask
from src.web.history import find_in_roots, visible_for_role, router as history_router
from src.web.guest_rules import guest_read_allowed
from src.web.auth import AuthMiddleware, load_accounts
from src.web.roles import ADMIN, GUEST, ROLES, USER, can_view_record, member_allowed as _member_allowed, viewer
from src.output.html_standalone import generate_html_from_history
from src.web.pipeline import (
    SSE_EVENTS,
    cancel_task,
    claim_task,
    make_task_id,
    push,
    release_task,
    run_pipeline,
    sanitize_task_id,
)
from src.web import quota

# Batch task tracking
batch_tracker: dict[str, dict] = {}

app = FastAPI(title="Video Analyzer")

_accounts = load_accounts()
if not _accounts:
    print("[warn] 凭据文件无可登录账号（/root/.scan-web-cred 缺 bcrypt 行）"
          "——管理功能不可用（fail closed），游客浏览不受影响")
else:
    _roles = ",".join(f"{n}={r}" for n, (_, r) in _accounts.items())
    print(f"[auth] 已加载 {len(_accounts)} 个账号（角色：{_roles}）")

# 三角色：游客只读白名单 / 常规用户（受限登录）/ 管理员（全放行）
app.add_middleware(
    AuthMiddleware,
    # 账号与 bcrypt 哈希从服务器本地凭据文件读，**绝不硬编码**（仓库公开，hash 泄漏只能靠轮换密码撤回）
    accounts=_accounts,
    is_read_allowed=guest_read_allowed,
    # 常规用户白名单：游客白名单 + 解析/问答/下载导出/删自己的（合集、批量、对比除外）
    member_allowed=lambda method, path: _member_allowed(method, path, guest_read_allowed),
)


@app.get("/api/whoami")
def whoami(request: Request):
    """当前身份（前端用它决定按角色显示哪些按钮，替代原先"有凭据=管理员"的二元判断）。

    附带返回配额概览（阶段 3）：剩余条数、今日已用、在飞任务数。
    游客拿到的 quota 是空壳（limit=0），不泄露任何账号信息。
    """
    user, role = viewer(request)
    return {
        "user": user, "role": role, "roles": list(ROLES), "admin": role == ADMIN,
        "quota": quota.status(user, role),
    }


_static = Path(__file__).parent / "static"


# mermaid 预压缩分发：回源拉取 0.9MB(gz) 而非 3.3MB —— 1Mbps 出网下 ~8s vs ~30s
@app.get("/static/vendor/mermaid.min.js")
def vendor_mermaid():
    gz = _static / "vendor" / "mermaid.min.js.gz"
    raw = _static / "vendor" / "mermaid.min.js"
    if gz.is_file():
        return FileResponse(gz, media_type="text/javascript",
                            headers={"Content-Encoding": "gzip",
                                     "Cache-Control": "public, max-age=86400"})
    return FileResponse(raw, media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})


# 自托管静态资源（vendor/ 下其余文件等；游客可读——见 guest_rules 白名单）
app.mount("/static", StaticFiles(directory=_static), name="static")


# ── SEO / 爬虫入口（2026-09-21 补）：此前 /robots.txt、/sitemap.xml 对游客 403
_SCAN_ORIGIN = "https://scan.andrew-li.top"


@app.get("/robots.txt")
def robots_txt():
    return Response(
        content=(
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /api/\n"
            "\n"
            f"Sitemap: {_SCAN_ORIGIN}/sitemap.xml\n"
        ),
        media_type="text/plain; charset=utf-8",
    )


@app.get("/sitemap.xml")
def sitemap_xml():
    return Response(
        content=(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "  <url>\n"
            f"    <loc>{_SCAN_ORIGIN}/</loc>\n"
            f"    <lastmod>{date.today().isoformat()}</lastmod>\n"
            "    <changefreq>weekly</changefreq>\n"
            "    <priority>1.0</priority>\n"
            "  </url>\n"
            "</urlset>\n"
        ),
        media_type="application/xml; charset=utf-8",
    )


@app.get("/favicon.ico")
def favicon_ico():
    # SPA 无独立 ico；用同一枚 SVG 顶掉 404 噪音（浏览器优先读 <link rel="icon">）
    return FileResponse(str(_static / "favicon.svg"), media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
def index():
    # 每次从磁盘读（改动免重启）；no-cache 确保浏览器不会拿启发式缓存里的旧页面
    # —— 否则前端改了按钮，用户刷新后仍看到旧 UI，误判「没生效」
    return HTMLResponse(
        (_static / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/api/progress/{task_id}")
async def progress(task_id: str):
    if task_id not in SSE_EVENTS:
        SSE_EVENTS[task_id] = queue.Queue()
    # 循环外捕获队列引用：不能每轮重新 SSE_EVENTS[task_id] ——
    # 若此时有第二条流/新任务把该键覆盖或 pop，正在读的这条会 KeyError 崩掉。
    q = SSE_EVENTS[task_id]

    async def gen():
        try:
            while True:
                try:
                    msg = await asyncio.to_thread(q.get, timeout=900)
                    yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"
                    if msg["event"] == "done" or msg["event"] == "error":
                        break
                except queue.Empty:
                    break
        finally:
            # 只清理自己那一条队列：被别人替换过就不能 pop（否则会清掉新任务的进度）
            if SSE_EVENTS.get(task_id) is q:
                SSE_EVENTS.pop(task_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/analyze")
async def analyze(request: Request, url: str = Query(...), parent_dir: str = Query("")):
    _user, _role = viewer(request)
    # 先去掉尾部斜杠：否则 rsplit("/",1)[-1] 为空串，所有以 / 结尾的 URL 会共用同一个 task_id
    task_id = sanitize_task_id(url.rstrip("/").rsplit("/", 1)[-1][:50])
    if not claim_task(task_id):
        # 同一 URL 已在跑：不再覆盖队列、不再起第二条流水线（会双份吃内存 + 两条流互踩）
        raise HTTPException(409, "该视频正在解析中，请稍候（进度条仍在本页显示）")

    # 配额检查放在 claim_task 之后：同一 URL 重复提交应该报 409 而不是吃配额报错。
    # 失败时要把刚登记的任务释放掉，否则这个 URL 会一直被当成"在跑"。
    ok, why = quota.reserve(_user, _role)
    if not ok:
        release_task(task_id)
        raise HTTPException(429, why)

    SSE_EVENTS[task_id] = queue.Queue()
    threading.Thread(target=asyncio.run, args=(run_pipeline(
        url, task_id, parent_dir=parent_dir, owner=_user, owner_role=_role),), daemon=True).start()
    return {"task_id": task_id}


@app.post("/api/analyze/playlist")
async def analyze_playlist(request: Request, url: str = Query(...)):
    from src.extractors import expand_playlist
    _pl_user, _pl_role = viewer(request)
    urls, author = await asyncio.to_thread(expand_playlist, url)
    if not urls:
        raise HTTPException(400, "播放列表为空或无法解析")

    batch_id = make_task_id()
    batch_tracker[batch_id] = {"total": len(urls), "done": 0, "failed": 0, "tasks": []}

    # Get author for parent folder naming. Bilibili provides it directly;
    # YouTube needs yt-dlp fallback.
    if not author:
        import yt_dlp as _yt_dlp
        try:
            with _yt_dlp.YoutubeDL({"quiet": True, "extract_flat": False}) as ydl:
                pl_info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            uploader = pl_info.get("uploader", "") or pl_info.get("channel", "") or ""
            author = re.sub(r"[\\/:*?\"<>|' ]", "_", uploader)[:30]
        except Exception:
            author = ""
    else:
        author = re.sub(r"[\\/:*?\"<>|' ]", "_", author)[:30]
    parent_dir = f"{author}_{date.today()}" if author else ""

    async def _run_serial():
        for i, video_url in enumerate(urls):
            tid = make_task_id()
            tr = batch_tracker.get(batch_id)
            if tr is None:  # 防御：记录意外丢失时仍继续跑任务（不阻塞批量本身）
                tr = {"total": len(urls), "done": 0, "failed": 0, "tasks": []}
                batch_tracker[batch_id] = tr
            tr["tasks"].append({"url": video_url, "task_id": tid, "status": "running"})
            SSE_EVENTS[tid] = queue.Queue()
            push(f"batch_{batch_id}", "batch_progress", json.dumps({
                "done": tr["done"],
                "total": len(urls),
                "failed": tr["failed"],
                "task_id": tid, "video": video_url.rsplit("/", 1)[-1][:40],
            }))
            try:
                ok = await run_pipeline(video_url, tid, parent_dir=parent_dir,
                                        owner=_pl_user, owner_role=_pl_role)
            except Exception:
                ok = False
            if batch_id in batch_tracker and not ok:
                batch_tracker[batch_id]["failed"] += 1
            if batch_id in batch_tracker:
                batch_tracker[batch_id]["done"] += 1
                done = batch_tracker[batch_id]["done"]
                failed = batch_tracker[batch_id]["failed"]
                push(f"batch_{batch_id}", "batch_progress", json.dumps({
                    "done": done, "total": len(urls), "failed": failed, "task_id": tid,
                }))
        if batch_id in batch_tracker:
            push(f"batch_{batch_id}", "batch_done", json.dumps({
                "done": batch_tracker[batch_id]["done"],
                "total": len(urls),
                "failed": batch_tracker[batch_id]["failed"],
            }))
        # 批量线程收尾：清理 tracker，避免内存泄漏（原由 SSE finally pop，已改为线程负责）
        batch_tracker.pop(batch_id, None)

    threading.Thread(target=asyncio.run, args=(_run_serial(),), daemon=True).start()
    return {"batch_id": batch_id, "total": len(urls)}


@app.get("/api/progress/batch/{batch_id}")
async def batch_progress(batch_id: str):
    if batch_id not in batch_tracker:
        raise HTTPException(404, "批次不存在")
    event_id = f"batch_{batch_id}"
    SSE_EVENTS[event_id] = queue.Queue()
    # 同 progress()：循环外捕获队列引用，避免中途被替换/pop 时 KeyError
    q = SSE_EVENTS[event_id]

    async def gen():
        try:
            while True:
                try:
                    msg = await asyncio.to_thread(q.get, timeout=900)
                    yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"
                    if msg["event"] == "batch_done":
                        break
                except queue.Empty:
                    break
        finally:
            # 只清理 SSE 事件队列；batch_tracker 归后台线程 _run_serial 管理，
            # 若在此 pop，前端断开进度流会清掉仍在运行批次的记录 →
            # _run_serial 下一轮 batch_tracker[batch_id] 直接 KeyError 崩溃（实测只剩 P1）。
            # 同样只清理自己那一条队列。
            if SSE_EVENTS.get(event_id) is q:
                SSE_EVENTS.pop(event_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/download/{filepath:path}")
def download(request: Request, filepath: str):
    _MIME = {".md": "text/markdown", ".srt": "text/plain", ".json": "application/json",
             ".pdf": "application/pdf", ".html": "text/html"}
    # 路径校验统一走 history.find_in_roots（原实现和 _resolve_folder 各抄了一份）
    fp = find_in_roots(filepath)
    if fp is not None:
        # 行级可见性：文件属于哪条记录，就按那条记录的可见性判（不可见一律 404）
        _user, role = viewer(request)
        if not visible_for_role(role, filepath):
            raise HTTPException(404, "文件不存在")
        mime = _MIME.get(fp.suffix, "application/octet-stream")
        return FileResponse(fp, filename=fp.name, media_type=mime)
    raise HTTPException(404, "文件不存在")


@app.get("/api/export/{base_name:path}")
def export(request: Request, base_name: str, format: str = "html"):
    from fastapi.responses import Response
    _user, role = viewer(request)
    if not visible_for_role(role, base_name):
        raise HTTPException(404, "记录不存在")
    if format == "html":
        html = generate_html_from_history(base_name)
        if html is None:
            raise HTTPException(404, "记录不存在")
        return Response(content=html, media_type="text/html",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_safe_filename(base_name)}.html"})
    if format == "pdf":
        from src.output.pdf import generate_pdf_from_history
        try:
            pdf_bytes = generate_pdf_from_history(base_name, backend=settings.pdf_backend)
        except RuntimeError as e:
            raise HTTPException(500, f"PDF 生成失败: {e}")
        if pdf_bytes is None:
            raise HTTPException(404, "记录不存在")
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_safe_filename(base_name)}.pdf"})
    raise HTTPException(400, f"不支持的导出格式: {format}")


@app.get("/api/mindmap/{base_name:path}")
def get_mindmap(request: Request, base_name: str):
    from src.analyzers.mindmap import generate_mindmap
    from src.web.history import _load_detail
    detail = _load_detail(base_name)
    if detail is None:
        raise HTTPException(404, "记录不存在")
    _user, role = viewer(request)
    if not can_view_record(role, detail):
        raise HTTPException(404, "记录不存在")
    if detail.get("mindmap"):
        return {"mermaid": detail["mindmap"], "title": detail["title"]}
    kp_dicts = [{"timestamp": kp["timestamp"], "content": kp["content"]} for kp in detail["keypoints"]]
    mermaid = generate_mindmap(detail["title"], detail["overview"], kp_dicts)
    return {"mermaid": mermaid, "title": detail["title"]}


class BatchExportRequest(BaseModel):
    base_names: list[str]
    # "html" = 零依赖（复用 generate_html_from_history），默认；
    # "pdf" = 需要本机有 weasyprint 或 chrome，否则每条都会进 _failed.json
    format: str = "html"


@app.post("/api/export/batch")
def export_batch(req: BatchExportRequest):
    from fastapi.responses import Response
    from io import BytesIO
    import zipfile
    if not req.base_names:
        raise HTTPException(400, "未选择记录")
    if len(req.base_names) > 50:
        raise HTTPException(400, "单次最多 50 条")
    if req.format not in ("html", "pdf"):
        raise HTTPException(400, f"不支持的导出格式: {req.format}（可选 html / pdf）")

    # 无条件导入：pdf.py 顶层不 import weasyprint/chrome，只有真正渲染时才去找引擎
    from src.output.pdf import generate_pdf_from_history

    used: set[str] = set()
    failed: list[dict] = []
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for bn in req.base_names:
            try:
                if req.format == "html":
                    html = generate_html_from_history(bn)
                    if html is None:
                        failed.append({"base_name": bn, "error": "记录不存在"})
                        continue
                    payload = html.encode("utf-8") if isinstance(html, str) else html
                else:
                    payload = generate_pdf_from_history(bn, backend=settings.pdf_backend)
                    if payload is None:
                        failed.append({"base_name": bn, "error": "记录不存在"})
                        continue
                # Unique filename: last segment of base_name + ext, with _1/_2 suffix on collision
                stem = bn.replace("\\", "/").rsplit("/", 1)[-1]
                stem = re.sub(r"[\\/:*?\"<>|]", "_", stem)[:80] or "video"
                name = f"{stem}.{req.format}"
                i = 1
                while name in used:
                    name = f"{stem}_{i}.{req.format}"
                    i += 1
                used.add(name)
                zf.writestr(name, payload)
            except Exception as e:
                failed.append({"base_name": bn, "error": str(e)})
        if failed:
            zf.writestr("_failed.json", json.dumps(failed, ensure_ascii=False, indent=2))

    buf.seek(0)
    fname = f"videos_{len(req.base_names)}_{req.format}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=\"{fname}\""},
    )


def _safe_filename(name: str) -> str:
    import re
    from urllib.parse import quote
    cleaned = re.sub(r"[\\/:*?\"<>| ]", "_", name.replace("\\", "/").rsplit("/", 1)[-1])[:60]
    return quote(cleaned)


class CompareRequest(BaseModel):
    base_names: list[str]


@app.post("/api/compare")
def compare_videos_endpoint(req: CompareRequest):
    from src.analyzers.comparator import compare_videos
    if len(req.base_names) < 2:
        raise HTTPException(400, "至少需要 2 个视频")
    if len(req.base_names) > 5:
        raise HTTPException(400, "单次最多对比 5 个视频")
    try:
        return {"comparison": compare_videos(req.base_names)}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"对比分析失败: {e}")


class ChatRequest(BaseModel):
    task_id: str
    q: str
    history: list[dict] = []

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    try:
        answer = await asyncio.to_thread(rag_ask, req.task_id, req.q, req.history)
        return {"answer": answer}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/chat/global")
async def chat_global(q: str = Query(...)):
    try:
        answer = await asyncio.to_thread(global_ask, q)
        return {"answer": answer}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/cancel/{task_id}")
def cancel(task_id: str):
    if cancel_task(task_id):
        return {"ok": True}
    raise HTTPException(404, "任务不存在")


# Include history routes (GET/DELETE /api/history, GET /api/history/{base_name})
app.include_router(history_router)


def main():
    import uvicorn
    uvicorn.run("src.web.server:app", host=settings.host, port=settings.port, reload=True)


if __name__ == "__main__":
    main()
