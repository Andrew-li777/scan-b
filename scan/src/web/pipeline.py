import asyncio
import json
import queue
import re
import threading
import time
import uuid
from pathlib import Path

import yt_dlp

from src.analyzers import extract_keypoints, summarize
from src.analyzers.summarizer import extract_concepts
from src.cache import cache
from src.classify import classify
from src.config import settings
from src.extractors import get_extractor
from src.extractors.base import clean_url
from src.models import AnalysisResult, SubtitleEntry, VideoMeta
from src.output.markdown import _OUTPUT_DIR, generate_all
from src.preprocess.cleaner import clean
from src.preprocess.dedup import deduplicate
from src.preprocess.segmenter import build_text, merge_short, segment
from src.transcriber import cleanup_audio, download_audio, transcribe
from src.translate import (
    needs_translation,
    translate_keypoints,
    translate_subtitles,
    translate_text,
)

# Shared yt-dlp opts for Bilibili (cookie support)
_BILI_YTDLP_OPTS = {}
if settings.bilibili_cookies:
    cookie_path = Path(settings.bilibili_cookies)
    if cookie_path.exists():
        _BILI_YTDLP_OPTS["cookiefile"] = str(cookie_path.resolve())
    _BILI_YTDLP_OPTS.setdefault("extractor_args", {"bilibili": {"skip_login": ["true"]}})

SSE_EVENTS: dict[str, queue.Queue] = {}
_running_tasks: dict[str, threading.Event] = {}


def cancel_task(task_id: str) -> bool:
    """Signal a running pipeline to cancel. Returns True if task was found."""
    event = _running_tasks.get(task_id)
    if event:
        event.set()
        return True
    return False


def push(task_id: str, event: str, data: str = ""):
    """Thread-safe push to SSE queue."""
    if task_id not in SSE_EVENTS:
        return
    SSE_EVENTS[task_id].put({"event": event, "data": data})


def make_task_id() -> str:
    """Generate a unique task_id using UUID (for batches)."""
    return "v_" + uuid.uuid4().hex[:32]


def sanitize_task_id(raw: str) -> str:
    """Generate a ChromaDB-safe collection name from a raw URL fragment."""
    import hashlib
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", raw)
    cleaned = cleaned.strip("_.-")
    if not cleaned or len(cleaned) < 3:
        cleaned = "task_" + hashlib.md5(raw.encode()).hexdigest()[:16]
    elif not cleaned[0].isalpha():
        cleaned = "v_" + cleaned
    if not cleaned[-1].isalnum():
        cleaned = cleaned.rstrip("_.-")
    return cleaned[:63]


async def run_pipeline(url: str, task_id: str, parent_dir: str = "") -> bool:
    """Full analysis pipeline. Pushes progress events via SSE_EVENTS."""
    cancel_event = threading.Event()
    _running_tasks[task_id] = cancel_event
    audio_path = None
    try:
        if cancel_event.is_set():
            push(task_id, "error", "任务已取消")
            return False
        push(task_id, "status", "提取字幕中...")
        extractor = get_extractor(url)
        title = author = thumb = ""
        duration = 0

        try:
            meta = await asyncio.to_thread(extractor.extract, url)
            title = meta.title
            author = meta.author
            duration = meta.duration
            thumb = meta.thumbnail
        except Exception as e:
            push(task_id, "status", f"API 提取失败 ({e})，改用语音识别...")
            opts = {"quiet": True, "no_warnings": True, "extract_flat": False, **_BILI_YTDLP_OPTS}
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                title = info.get("title", "") or title
                author = info.get("uploader", "") or author
                duration = int(info.get("duration", 0) or 0) or duration
                thumb = info.get("thumbnail", "") or thumb
            except Exception:
                pass
            meta = None

        subs = meta.subtitles if meta else []

        # Whisper fallback if no subtitles from platform
        if not subs:
            push(task_id, "status", "无可用字幕，正在下载音频...")
            audio_path = await asyncio.to_thread(download_audio, url,
                lambda pct: push(task_id, "download", f"{pct:.0f}"))

            push(task_id, "status", "正在语音识别（可能需要几分钟）...")
            subs = await asyncio.to_thread(transcribe, audio_path,
                lambda pct: push(task_id, "transcribe", f"{pct:.0f}"))

            push(task_id, "status", f"语音识别完成 ({len(subs)} 条)")
            existing_video_id = meta.video_id if meta else ""
            meta = VideoMeta(
                platform=extractor.platform,
                video_id=existing_video_id,
                title=title,
                url=clean_url(url),
                duration=duration,
                author=author,
                subtitles=subs,
                thumbnail=thumb,
                subtitle_source="whisper",
            )

        if not subs:
            push(task_id, "error", "该视频没有可用字幕，语音识别也未获取到内容")
            return False

        if cancel_event.is_set():
            push(task_id, "error", "任务已取消")
            return False

        push(task_id, "status",
             f"字幕获取成功 ({len(meta.subtitles)} 条，来源: {meta.subtitle_source or '平台字幕'})，预处理中...")

        subs = clean(meta.subtitles)
        subs = deduplicate(subs)
        subs = merge_short(subs)

        push(task_id, "status", f"预处理完成 ({len(subs)} 条)，AI 分析中...")

        if cancel_event.is_set():
            push(task_id, "error", "任务已取消")
            return False

        summary_text, chapters = await summarize(
            subs, meta.title,
            lambda msg: push(task_id, "status", msg))
        keypoints = await extract_keypoints(
            subs, meta.title,
            lambda msg: push(task_id, "status", msg))

        # Concept extraction for Obsidian wikilinks
        concepts = []
        try:
            push(task_id, "status", "提取核心概念...")
            concepts = await asyncio.to_thread(extract_concepts, meta.title, summary_text, keypoints)
        except Exception:
            pass

        # Translation for non-Chinese videos
        translated = False
        if needs_translation(subs):
            translated = True
            push(task_id, "status", "检测到外文内容，正在翻译...")
            keypoints = await asyncio.to_thread(translate_keypoints, keypoints)
            summary_text = summary_text + "\n\n## 中文对照\n" + await asyncio.to_thread(translate_text, summary_text)
            sub_zh = await translate_subtitles(subs)
            for i, zh in enumerate(sub_zh):
                if i < len(subs) and zh:
                    subs[i].text = subs[i].text + "\n" + zh

        result = AnalysisResult(
            meta=VideoMeta(
                platform=meta.platform,
                video_id=meta.video_id,
                title=meta.title,
                url=meta.url,
                duration=meta.duration,
                author=meta.author,
                subtitles=subs,
                thumbnail=meta.thumbnail,
                subtitle_source=meta.subtitle_source,
            ),
            summary=summary_text,
                chapters=chapters,
            keypoints=keypoints,
        )

        base_name = re.sub(r"[\\/:*?\"<>|' ]", "_", meta.title)[:60]
        if parent_dir:
            base_name = f"{parent_dir}/{base_name}"
        files = await asyncio.to_thread(generate_all, result, base_name, concepts=concepts)
        mermaid = ""

        # Generate mindmap (LLM-dependent, separate from sync generate_all)
        try:
            push(task_id, "status", "生成思维导图...")
            from src.analyzers.mindmap import generate_mindmap as gen_mm
            kp_dicts = [{"timestamp": kp.timestamp, "content": kp.content} for kp in keypoints]
            mermaid = await asyncio.to_thread(gen_mm, meta.title, summary_text, kp_dicts)
            mm_path = _OUTPUT_DIR / base_name / "mindmap.md"
            mm_path.write_text(
                f"# {meta.title} — 思维导图\n\n```mermaid\n{mermaid}\n```\n",
                encoding="utf-8")
            files["mindmap"] = str(mm_path.relative_to(_OUTPUT_DIR))
        except Exception:
            pass

        # Persist metadata for history
        _meta = {
            "title": meta.title, "platform": meta.platform,
            "author": meta.author, "duration": meta.duration,
            "url": meta.url, "thumbnail": meta.thumbnail,
            "concepts": concepts, "created_at": time.time(),
        }
        if meta.subtitles:
            push(task_id, "status", "分类中...")
            try:
                tags = await asyncio.to_thread(classify, meta.title, summary_text)
                _meta.update(tags)
            except Exception:
                pass
        (_OUTPUT_DIR / base_name / "meta.json").write_text(
            json.dumps(_meta, ensure_ascii=False, indent=2), encoding="utf-8")

        cache.set(task_id, result)

        # Build RAG index (skipped when rag_enabled=False, saves ~1.6GB resident memory)
        if settings.rag_enabled:
            push(task_id, "status", "构建检索索引...")
            from src.rag.chat import build_index
            from src.rag.vectorstore import index_global

            segs = segment(subs)
            seg_texts = [build_text(s) for s in segs]
            seg_starts = [s[0].start for s in segs]
            await asyncio.to_thread(build_index, task_id, seg_texts, seg_starts)

            # Also index into the global cross-video collection
            global_segs = [
                (f"{task_id}_{i}", t, seg_starts[i], base_name)
                for i, t in enumerate(seg_texts)
            ]
            await asyncio.to_thread(index_global, global_segs)

        # Build subtitle text preview
        sub_lines = []
        for e in subs[:500]:
            m, s = divmod(int(e.start), 60)
            h, m2 = divmod(m, 60)
            if h:
                ts = f"{h}:{m2:02d}:{s:02d}"
            else:
                ts = f"{m}:{s:02d}"
            sub_lines.append(f"[{ts}] {e.text}")

        push(task_id, "done", json.dumps({
            "title": meta.title,
            "author": meta.author,
            "duration": meta.duration,
            "thumbnail": meta.thumbnail,
            "url": meta.url,
            "platform": meta.platform,
            "subtitle_source": meta.subtitle_source or "unknown",
            "summary": summary_text[:500],
            "keypoints": [{"timestamp": kp.timestamp, "content": kp.content, "importance": kp.importance} for kp in keypoints],
            "files": {k: str(v) for k, v in files.items()},
            "subtitles": sub_lines,
            "overview": summary_text,
            "mindmap": mermaid,
            "translated": translated,
            "base_name": base_name,
        }))
        return True
    except Exception as e:
        push(task_id, "error", str(e))
        return False
    finally:
        _running_tasks.pop(task_id, None)
        if audio_path:
            try:
                await asyncio.to_thread(cleanup_audio, audio_path)
            except Exception:
                pass
        # Clean up empty temp directories
        try:
            for d in _TEMP_VIDEO_DIR.iterdir():
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        except Exception:
            pass
