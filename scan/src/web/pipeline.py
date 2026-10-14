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
from src.transcriber import cleanup_audio, cleanup_empty_tempdirs, download_audio, transcribe
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

# 解析结果缓存 TTL：7 天。同 URL 再次提交可直接回放，省掉下载/转录/LLM/嵌入全流程。
_CACHE_TTL_SECONDS = 7 * 24 * 3600

# 转录并发闸：whisper 是全流程的内存峰值（dmesg 实测本机 OOM 杀过 python，RSS 3.0–3.4GB），
# 而 bge-m3 常驻 2.1GB 后余量更小 —— 同时跑两条无字幕视频就可能再撞 OOM。
# 只串行化「无字幕 → whisper 转录」这一段；有字幕的视频走平台字幕，不受影响。
_TRANSCRIBE_GATE = threading.BoundedSemaphore(max(1, int(settings.max_concurrent_transcribe or 1)))


def _acquire_transcribe_gate(task_id: str, cancel_event: threading.Event) -> bool:
    """取得转录闸（轮询等待以便响应取消）。返回 False = 等待期间被取消。"""
    if _TRANSCRIBE_GATE.acquire(blocking=False):
        return True
    push(task_id, "status", "前方有转录任务在运行，排队等待（避免内存溢出）...")
    while not _TRANSCRIBE_GATE.acquire(timeout=3):
        if cancel_event.is_set():
            return False
    return True


# 任务登记表：同一 URL 派生同一个 task_id，重复提交必须被挡住
_active_tasks: set[str] = set()
_active_lock = threading.Lock()


def claim_task(task_id: str) -> bool:
    """登记 task_id；已在跑则返回 False。

    旧代码直接 `SSE_EVENTS[task_id] = queue.Queue()` 覆盖，会同时踩三个坑：
      ① 两条进度流抢同一个队列（queue 是单消费者，事件被对半分，两边进度都残缺）；
      ② 任一条先结束时 pop 掉队列 → 另一条下一次取 SSE_EVENTS[task_id] 直接 KeyError 崩掉；
      ③ 白跑两条完整流水线（双份下载 + 双份转录内存，正好撞 OOM）。
    """
    with _active_lock:
        if task_id in _active_tasks:
            return False
        _active_tasks.add(task_id)
        return True


def release_task(task_id: str) -> None:
    with _active_lock:
        _active_tasks.discard(task_id)


def is_task_active(task_id: str) -> bool:
    with _active_lock:
        return task_id in _active_tasks


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
    from src.rag.ids import sanitize as _sanitize

    return _sanitize(raw)


async def run_pipeline(url: str, task_id: str, parent_dir: str = "") -> bool:
    """Full analysis pipeline. Pushes progress events via SSE_EVENTS."""
    cancel_event = threading.Event()
    _running_tasks[task_id] = cancel_event
    audio_path = None
    try:
        if cancel_event.is_set():
            push(task_id, "error", "任务已取消")
            return False

        # 缓存命中：同一 URL（task_id 由 URL 派生）直接回放上次结果，
        # 省掉下载/转录/LLM/嵌入全流程。旧代码只有 cache.set 没有 cache.get，
        # 缓存写进去从来没人读。
        try:
            hit = cache.get(task_id)
        except Exception:
            hit = None
        if isinstance(hit, dict) and hit.get("title"):
            push(task_id, "status", "命中本地缓存，直接返回上次解析结果")
            push(task_id, "done", json.dumps(hit))
            return True

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

            # 转录段过闸（并发=1 时排队），防多任务同时转录把内存打爆
            if not await asyncio.to_thread(_acquire_transcribe_gate, task_id, cancel_event):
                push(task_id, "error", "任务已取消")
                return False
            try:
                push(task_id, "status", "正在语音识别（可能需要几分钟）...")
                subs = await asyncio.to_thread(transcribe, audio_path,
                    lambda pct: push(task_id, "transcribe", f"{pct:.0f}"))
            finally:
                _TRANSCRIBE_GATE.release()

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

        # RAG 检索标识：一律由 URL 派生（见 src/rag/ids.py），不用运行时 task_id ——
        # 批量/分P 的 task_id 是随机 UUID，页面刷新后无从复现，历史记录会永远找不到自己的索引。
        from src.rag.ids import task_id_for_url

        index_id = task_id_for_url(meta.url or "")

        # Build RAG index (skipped when rag_enabled=False, saves ~1.6GB resident memory)
        if settings.rag_enabled:
            push(task_id, "status", "构建检索索引...")
            from src.rag.chat import build_index
            from src.rag.vectorstore import index_global

            segs = segment(subs)
            seg_texts = [build_text(s) for s in segs]
            seg_starts = [s[0].start for s in segs]
            await asyncio.to_thread(build_index, index_id, seg_texts, seg_starts)

            # Also index into the global cross-video collection
            global_segs = [
                (f"{index_id}_{i}", t, seg_starts[i], base_name)
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

        done_payload = {
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
            "rag": {"task_id": index_id},
        }
        # 缓存下来供下次同 URL 直接回放（带 TTL，避免过期结果被永久回放）
        try:
            cache.set(task_id, done_payload, expire=_CACHE_TTL_SECONDS)
        except Exception:
            pass
        push(task_id, "done", json.dumps(done_payload))
        return True
    except Exception as e:
        push(task_id, "error", str(e))
        return False
    finally:
        _running_tasks.pop(task_id, None)
        release_task(task_id)
        if audio_path:
            try:
                await asyncio.to_thread(cleanup_audio, audio_path)
            except Exception:
                pass
        # 清理临时目录：下载失败/中断时 audio_path 为空，cleanup_audio 不会被调用，
        # 于是每跑一次就留一个空目录 —— 交给 transcriber 里的专用函数处理
        # （原来这里写的是 `_TEMP_VIDEO_DIR`，该名字在本模块不存在 → NameError 被吞，
        #   清理从未生效。现已改为调用 transcriber.cleanup_empty_tempdirs()）
        try:
            await asyncio.to_thread(cleanup_empty_tempdirs)
        except Exception:
            pass
