import asyncio
import json
import os
import queue
import re
import threading
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from src.cache import cache
from src.config import settings
from src.output.markdown import _OUTPUT_DIR
from src.rag.chat import ask as rag_ask, global_ask
from src.web.history import router as history_router, scan_dirs
from src.output.html_standalone import generate_html_from_history
from src.web.pipeline import SSE_EVENTS, cancel_task, make_task_id, push, run_pipeline, sanitize_task_id

# Batch task tracking
batch_tracker: dict[str, dict] = {}

app = FastAPI(title="Video Analyzer")

_static = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
def index():
    return (_static / "index.html").read_text(encoding="utf-8")


@app.get("/api/progress/{task_id}")
async def progress(task_id: str):
    if task_id not in SSE_EVENTS:
        SSE_EVENTS[task_id] = queue.Queue()

    async def gen():
        try:
            while True:
                try:
                    msg = await asyncio.to_thread(SSE_EVENTS[task_id].get, timeout=900)
                    yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"
                    if msg["event"] == "done" or msg["event"] == "error":
                        break
                except queue.Empty:
                    break
        finally:
            SSE_EVENTS.pop(task_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/analyze")
async def analyze(url: str = Query(...), parent_dir: str = Query("")):
    task_id = sanitize_task_id(url.rsplit("/", 1)[-1][:50])
    SSE_EVENTS[task_id] = queue.Queue()
    threading.Thread(target=asyncio.run, args=(run_pipeline(url, task_id, parent_dir=parent_dir),), daemon=True).start()
    return {"task_id": task_id}


@app.post("/api/analyze/playlist")
async def analyze_playlist(url: str = Query(...)):
    from src.extractors import expand_playlist
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
            batch_tracker[batch_id]["tasks"].append({"url": video_url, "task_id": tid, "status": "running"})
            SSE_EVENTS[tid] = queue.Queue()
            push(f"batch_{batch_id}", "batch_progress", json.dumps({
                "done": batch_tracker[batch_id]["done"],
                "total": len(urls),
                "failed": batch_tracker[batch_id]["failed"],
                "task_id": tid, "video": video_url.rsplit("/", 1)[-1][:40],
            }))
            try:
                ok = await run_pipeline(video_url, tid, parent_dir=parent_dir)
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

    threading.Thread(target=asyncio.run, args=(_run_serial(),), daemon=True).start()
    return {"batch_id": batch_id, "total": len(urls)}


@app.get("/api/progress/batch/{batch_id}")
async def batch_progress(batch_id: str):
    if batch_id not in batch_tracker:
        raise HTTPException(404, "批次不存在")
    event_id = f"batch_{batch_id}"
    SSE_EVENTS[event_id] = queue.Queue()

    async def gen():
        try:
            while True:
                try:
                    msg = await asyncio.to_thread(SSE_EVENTS[event_id].get, timeout=900)
                    yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"
                    if msg["event"] == "batch_done":
                        break
                except queue.Empty:
                    break
        finally:
            SSE_EVENTS.pop(event_id, None)
            batch_tracker.pop(batch_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/download/{filepath:path}")
def download(filepath: str):
    _MIME = {".md": "text/markdown", ".srt": "text/plain", ".json": "application/json",
             ".pdf": "application/pdf", ".html": "text/html"}
    for root in scan_dirs():
        resolved_root = root.resolve()
        fp = (resolved_root / filepath).resolve()
        if not (str(fp).startswith(str(resolved_root) + os.sep) or fp == resolved_root):
            continue
        if fp.exists():
            mime = _MIME.get(fp.suffix, "application/octet-stream")
            return FileResponse(fp, filename=fp.name, media_type=mime)
    raise HTTPException(404, "文件不存在")


@app.get("/api/export/{base_name:path}")
def export(base_name: str, format: str = "html"):
    from fastapi.responses import Response
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
def get_mindmap(base_name: str):
    from src.analyzers.mindmap import generate_mindmap
    from src.web.history import _load_detail
    detail = _load_detail(base_name)
    if detail is None:
        raise HTTPException(404, "记录不存在")
    if detail.get("mindmap"):
        return {"mermaid": detail["mindmap"], "title": detail["title"]}
    kp_dicts = [{"timestamp": kp["timestamp"], "content": kp["content"]} for kp in detail["keypoints"]]
    mermaid = generate_mindmap(detail["title"], detail["overview"], kp_dicts)
    return {"mermaid": mermaid, "title": detail["title"]}


class BatchExportRequest(BaseModel):
    base_names: list[str]
    format: str = "pdf"


@app.post("/api/export/batch")
def export_batch(req: BatchExportRequest):
    from fastapi.responses import Response
    from io import BytesIO
    import zipfile
    if not req.base_names:
        raise HTTPException(400, "未选择记录")
    if len(req.base_names) > 50:
        raise HTTPException(400, "单次最多 50 条")
    if req.format != "pdf":
        raise HTTPException(400, f"暂只支持 pdf 格式，收到: {req.format}")

    from src.output.pdf import generate_pdf_from_history

    used: set[str] = set()
    failed: list[dict] = []
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for bn in req.base_names:
            try:
                pdf_bytes = generate_pdf_from_history(bn, backend=settings.pdf_backend)
                if pdf_bytes is None:
                    failed.append({"base_name": bn, "error": "记录不存在"})
                    continue
                # Unique filename: last segment of base_name + .pdf, with _1/_2 suffix on collision
                stem = bn.replace("\\", "/").rsplit("/", 1)[-1]
                stem = re.sub(r"[\\/:*?\"<>|]", "_", stem)[:80] or "video"
                name = f"{stem}.pdf"
                i = 1
                while name in used:
                    name = f"{stem}_{i}.pdf"
                    i += 1
                used.add(name)
                zf.writestr(name, pdf_bytes)
            except Exception as e:
                failed.append({"base_name": bn, "error": str(e)})
        if failed:
            zf.writestr("_failed.json", json.dumps(failed, ensure_ascii=False, indent=2))

    buf.seek(0)
    fname = f"videos_{len(req.base_names)}.zip"
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
