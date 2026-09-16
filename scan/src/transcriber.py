import logging
import math
import os
import site
import threading
import uuid
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)


def _inject_cuda_path():
    """Inject NVIDIA CUDA DLL dirs into PATH before CTranslate2 loads."""
    for sp in site.getsitepackages():
        nv_dir = os.path.join(sp, "nvidia")
        if not os.path.isdir(nv_dir):
            continue
        for pkg in os.listdir(nv_dir):
            bin_dir = os.path.join(nv_dir, pkg, "bin")
            if os.path.isdir(bin_dir) and bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + ";" + os.environ.get("PATH", "")
    # Also check system CUDA Toolkit
    for ver in ("v13.3", "v13.0", "v12.8", "v12.6"):
        cuda_bin = rf"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\{ver}\bin"
        if os.path.isdir(cuda_bin) and cuda_bin not in os.environ.get("PATH", ""):
            os.environ["PATH"] = cuda_bin + ";" + os.environ.get("PATH", "")


_inject_cuda_path()

import yt_dlp

from src.config import settings
from src.models import SubtitleEntry

_PROJECT_ROOT = Path(__file__).parent.parent
_TEMP_VIDEO_DIR = _PROJECT_ROOT / "tempvideo"
_MODEL_ROOT = _PROJECT_ROOT / (settings.model_dir or "models")


def _ensure_model_dir(model_size: str) -> Path:
    """Return the local model dir for `model_size`, raising a clear offline error if missing."""
    d = _MODEL_ROOT / f"whisper-{model_size}"
    if not d.is_dir():
        raise RuntimeError(
            f"Whisper 模型未就绪：{d} 不存在。"
            f"请先运行: python scripts/download_models.py --whisper"
        )
    return d

_whisper_model = None
_device = None
_compute_type = None
_model_size = None
_model_lock = threading.Lock()


def _detect_device():
    global _device, _compute_type, _model_size
    if _device is not None:
        return _device, _compute_type, _model_size

    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            _device = "cuda"
            _compute_type = "float16"
            _model_size = settings.whisper_model or "small"
            logger.info("GPU detected → model=%s, device=%s, compute=%s", _model_size, _device, _compute_type)
            return _device, _compute_type, _model_size
    except Exception:
        pass  # 无 GPU / ctranslate2 不可用 / DLL 缺失 → CPU 兜底

    import multiprocessing
    _device = "cpu"
    _compute_type = "int8_float16"
    _model_size = settings.whisper_model or "small"
    n_cores = multiprocessing.cpu_count()
    logger.info("No GPU → cpu/%s, model=%s, cores=%s", _compute_type, _model_size, n_cores)
    return _device, _compute_type, _model_size


def _get_model():
    global _whisper_model, _model_size, _device, _compute_type
    if _whisper_model is not None:
        return _whisper_model

    with _model_lock:
        if _whisper_model is not None:
            return _whisper_model
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
        from faster_whisper import WhisperModel

        device, compute, model_size = _detect_device()
        # 离线优先：模型必须已存在于本地 models/ 目录，绝不触发 HuggingFace 下载
        _ensure_model_dir(model_size)

        if device == "cuda":
            candidates = []
            if settings.whisper_model:
                candidates.append((settings.whisper_model, "int8_float16"))
            candidates += [("medium", "int8_float16"), ("small", "float16")]
            for model_size, compute in candidates:
                try:
                    _whisper_model = WhisperModel(str(_ensure_model_dir(model_size)), device="cuda",
                        compute_type=compute, num_workers=2)
                    import numpy as np
                    _whisper_model.encode(np.zeros((1, 80, 3000), dtype=np.float32))
                    _device = "cuda"
                    _compute_type = compute
                    _model_size = model_size
                    logger.info("Model loaded: %s on cuda/%s", model_size, compute)
                    return _whisper_model
                except Exception as e:
                    logger.warning("%s on cuda/%s failed: %s", model_size, compute, e)

        device = "cpu"
        model_size = settings.whisper_model or "small"
        for compute in ("int8", "int8_float16"):
            try:
                _whisper_model = WhisperModel(str(_ensure_model_dir(model_size)), device="cpu",
                    compute_type=compute, num_workers=2)
                _device = "cpu"
                _compute_type = compute
                _model_size = model_size
                logger.info("Model loaded: %s on cpu/%s", model_size, compute)
                return _whisper_model
            except Exception:
                continue
        raise RuntimeError("无法加载 Whisper 模型（CPU/GPU 均不可用）")


def _release_model():
    """释放已加载的 Whisper 模型，回收物理内存（含已换出到 swap 的页）。

    转录任务结束后调用，避免模型常驻（~1.3GB）。代价：下次转录需重新加载（慢 10-20s）。
    保留 _device/_compute_type 缓存，重新加载时无需再次探测设备。
    """
    global _whisper_model
    with _model_lock:
        if _whisper_model is not None:
            _whisper_model = None
            import gc
            gc.collect()
            logger.info("Whisper model released, ~1.3GB freed (next transcription will reload)")


def download_audio(url: str, progress_cb=None) -> Path:
    _TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    tmpdir = _TEMP_VIDEO_DIR / str(uuid.uuid4())[:8]
    tmpdir.mkdir(exist_ok=True)

    # Bilibili: use playurl API to bypass yt-dlp 412
    if "bilibili.com/video/BV" in url:
        return _download_bilibili_audio(url, tmpdir, progress_cb)

    out = tmpdir / "audio"

    def hook(d):
        if d["status"] == "downloading" and progress_cb:
            pct = d.get("_percent_str", "0%").strip().rstrip("%")
            try:
                progress_cb(float(pct))
            except ValueError:
                pass

    # Bilibili cookie support for yt-dlp audio download
    bili_opts = {}
    if settings.bilibili_cookies:
        cookie_path = Path(settings.bilibili_cookies)
        if cookie_path.exists():
            bili_opts["cookiefile"] = str(cookie_path.resolve())
        bili_opts.setdefault("extractor_args", {"bilibili": {"skip_login": ["true"]}})

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": str(out),
        "progress_hooks": [hook],
        **bili_opts,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    candidates = sorted(tmpdir.glob("audio*"), key=lambda p: p.stat().st_size, reverse=True)
    for c in candidates:
        if c.suffix in (".m4a", ".webm", ".opus", ".mp4", ".mkv", ".wav", ".mp3"):
            return c
    if candidates:
        return candidates[0]
    raise RuntimeError("音频下载失败")


def _download_bilibili_audio(url: str, tmpdir: Path, progress_cb=None) -> Path:
    """Download Bilibili audio via playurl API, bypassing yt-dlp."""
    import re

    import httpx

    from src.extractors.bilibili import BilibiliExtractor, _COOKIE_STR, fetch_bilibili_audio_url

    ex = BilibiliExtractor()
    bvid = ex._parse_bvid(url)
    info = ex._fetch_info(bvid)
    cid = info.get("cid", 0)

    result = fetch_bilibili_audio_url(bvid, cid)
    if not result:
        raise RuntimeError("无法获取 Bilibili 音频流地址")
    audio_url, mime_type = result

    # Determine extension from mime type
    ext = ".m4a"
    if "webm" in mime_type:
        ext = ".webm"
    elif "mp4" in mime_type:
        ext = ".mp4"
    outpath = tmpdir / ("audio" + ext)

    dl_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com",
    }
    if _COOKIE_STR:
        dl_headers["Cookie"] = _COOKIE_STR

    # 本地修复：B 站部分 CDN 节点（*.bilivideo.com）证书过期导致 SSL 验证失败。
    # 音频为公开内容，verify=False 可绕过坏节点；仍保留重试以期望 DNS 轮询到正常节点。
    import time as _time

    _last_err = None
    for _attempt in range(3):
        try:
            with httpx.stream("GET", audio_url, headers=dl_headers, timeout=120,
                              follow_redirects=True, verify=False) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0)) or None
                downloaded = 0
                with open(outpath, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb and total:
                            progress_cb(min(downloaded / total * 100, 99))
            break
        except Exception as e:
            _last_err = e
            _time.sleep(1)
    else:
        raise RuntimeError(f"音频下载失败（已重试 3 次）: {_last_err}")
    if progress_cb:
        progress_cb(100)
    return outpath


def _transcribe_single(model, audio_path: Path, progress_cb=None, duration: float = 0) -> list[SubtitleEntry]:
    """Transcribe one audio file with the already-loaded model. Returns entries (0-100 progress)."""
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=1,
        language=None,
        vad_filter=True,
        vad_parameters={
            "threshold": 0.5,
            "min_speech_duration_ms": 250,
            "min_silence_duration_ms": 400,
            "speech_pad_ms": 400,
        },
        word_timestamps=False,
    )

    entries: list[SubtitleEntry] = []
    if progress_cb:
        progress_cb(0)

    for seg in segments:
        entries.append(SubtitleEntry(
            start=round(seg.start, 2),
            end=round(seg.end, 2),
            text=seg.text.strip(),
        ))
        if progress_cb and info.duration > 0:
            progress_cb(min(seg.end / info.duration * 100, 99))

    if progress_cb:
        progress_cb(100)
    return entries


def _probe_duration(audio_path: Path) -> float:
    """Return audio duration in seconds using PyAV (no system ffmpeg needed)."""
    import av
    container = av.open(str(audio_path))
    try:
        stream = container.streams.audio[0]
        dur = float(stream.duration * stream.time_base) if stream.duration else 0.0
        if dur <= 0:
            # Fallback: read frames and track pts
            max_pts = 0.0
            for frame in container.decode(stream):
                if frame.pts is not None:
                    max_pts = max(max_pts, float(frame.pts * frame.time_base))
            dur = max_pts
        return dur
    finally:
        container.close()


def _cut_audio_chunk(src: Path, out: Path, start: float, end: float):
    """Cut [start, end] seconds from src into out using PyAV streaming decode.

    Decodes from `start` (backward seek for safety), muxes until `end`.
    Writes to `out` as m4a/aac to keep the file small.
    """
    import av
    in_c = av.open(str(src))
    in_s = in_c.streams.audio[0]
    # try to copy the same codec if available, else fallback to AAC
    out_c = av.open(str(out), "w")
    out_s = None
    try:
        out_s = out_c.add_stream("aac")
    except Exception:
        out_s = out_c.add_stream("mp3")
    out_s.sample_rate = in_s.codec_context.sample_rate or 16000
    # Map channel count to a legal layout name (ctx.layout.name can be junk like '1 channels')
    n_ch = in_s.codec_context.layout.channels if in_s.codec_context.layout else None
    if n_ch == 1:
        out_s.layout = "mono"
    elif n_ch == 2:
        out_s.layout = "stereo"
    out_s.bit_rate = in_s.bit_rate or 64000

    # Seek to start (backward) then decode, keeping frames within [start, end]
    try:
        in_c.seek(int(start / float(in_s.time_base)), stream=in_s, backward=True)
    except Exception:
        # Fallback: no seek, decode from beginning (slower but correct)
        pass

    t0, t1 = start, end
    for frame in in_c.decode(in_s):
        if frame.pts is None:
            continue
        ts = float(frame.pts * frame.time_base)
        if ts + float(frame.duration * frame.time_base) < t0:
            continue
        if ts > t1:
            break
        for packet in out_s.encode(frame):
            out_c.mux(packet)
    for packet in out_s.encode(None):
        out_c.mux(packet)
    out_c.close()
    in_c.close()
    return out


def _dedup_overlaps(entries: list[SubtitleEntry], overlap: float) -> list[SubtitleEntry]:
    """Merge entries from chunked transcription, dropping duplicated text in overlap zones.

    After chunk offsetting, entries are sorted by start. Only entries whose start lands
    inside the previous KEPT entry's window (i.e. from the overlap region) are checked:
    - if its text is near-identical to the previous entry's text -> drop (duplicate)
    - otherwise keep but clamp its start to the previous entry's end (avoid time overlap)
    """
    if not entries:
        return []
    entries = sorted(entries, key=lambda e: (e.start, e.end))
    merged: list[SubtitleEntry] = []
    last_kept = None
    for e in entries:
        if last_kept is not None and e.start < last_kept.end - 0.01:
            # Only consider entries that actually overlap the previous kept one
            if _text_similar(e.text, last_kept.text):
                continue  # duplicate from overlap zone -> drop
            if e.start < last_kept.end:
                new_start = last_kept.end
                if new_start < e.end:
                    e = SubtitleEntry(start=round(new_start, 2), end=e.end, text=e.text)
                else:
                    continue  # fully swallowed
        merged.append(e)
        last_kept = e
    return merged


def _text_similar(a: str, b: str) -> bool:
    """True if a and b are near-duplicates (same utterance transcribed twice).

    Real Whisper repeats of the same audio are near-identical strings; distinct
    adjacent sentences rarely share 85%+ of character bigrams. Threshold is high
    on purpose so we only drop true duplicates, never distinct content.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    # Short-cut: one contains the other
    if len(a) >= 6 and len(b) >= 6 and (a in b or b in a):
        return True
    def bigrams(s):
        return {s[i:i + 2] for i in range(len(s) - 1)}
    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return False
    inter = len(ba & bb)
    return inter / min(len(ba), len(bb)) >= 0.85


def transcribe(audio_path: Path, progress_cb=None) -> list[SubtitleEntry]:
    model = _get_model()
    try:
        duration = _probe_duration(audio_path)

        chunk_len = max(int(settings.whisper_chunk_seconds), 60)
        overlap = max(float(settings.whisper_chunk_overlap), 0.0)

        # Short audio: single-pass (existing behaviour)
        if duration <= chunk_len:
            return _transcribe_single(model, audio_path, progress_cb)

        # Long audio: chunked transcription to bound peak memory
        logger.info("Long audio (%.1fs > %ds): chunking with %ds overlap", duration, chunk_len, overlap)
        tmpdir = audio_path.parent
        all_entries: list[SubtitleEntry] = []
        n_chunks = int(math.ceil(duration / chunk_len))
        chunk_i = 0
        start = 0.0
        while start < duration:
            chunk_i += 1
            end = min(start + chunk_len + overlap, duration)
            chunk_path = tmpdir / f"chunk_{chunk_i:03d}.m4a"
            _cut_audio_chunk(audio_path, chunk_path, start, end)
            chunk_dur = _probe_duration(chunk_path)

            # Progress for this chunk (within-chunk progress scaled to global)
            def make_cb(c_start, c_dur):
                def cb(pct):
                    if progress_cb and c_dur > 0:
                        global_pct = (c_start + pct / 100 * c_dur) / duration * 100
                        progress_cb(min(global_pct, 99))
                return cb

            try:
                entries = _transcribe_single(model, chunk_path, make_cb(start, chunk_dur))
            except Exception as e:
                logger.warning("Chunk %d failed: %s — skipping", chunk_i, e)
                entries = []
            finally:
                try:
                    chunk_path.unlink(missing_ok=True)
                except OSError:
                    pass

            # Offset timestamps to global timeline. Overlap-zone duplicates are
            # handled later by _dedup_overlaps; do NOT skip entries here (their
            # local timestamps are meaningless vs the global `offset`).
            offset = start
            for e in entries:
                e.start = round(e.start + offset, 2)
                e.end = round(e.end + offset, 2)
            all_entries.extend(entries)
            logger.info("Chunk %d/%d done: %d entries (offset %.1fs)", chunk_i, n_chunks, len(entries), offset)

            start += chunk_len

        merged = _dedup_overlaps(all_entries, overlap)
        if progress_cb:
            progress_cb(100)
        logger.info("Chunked transcription complete: %d entries after dedup (raw %d)", len(merged), len(all_entries))
        return merged
    finally:
        # 转录结束（无论成败）释放模型，回收物理内存 + swap 页，避免模型常驻
        _release_model()


def cleanup_audio(audio_path: Path):
    tmpdir = audio_path.parent
    for f in tmpdir.iterdir():
        try:
            f.unlink()
        except OSError:
            pass
    try:
        tmpdir.rmdir()
    except OSError:
        pass
