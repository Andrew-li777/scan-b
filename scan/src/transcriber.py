import logging
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

    with httpx.stream("GET", audio_url, headers=dl_headers, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) or None
        downloaded = 0
        with open(outpath, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total:
                    progress_cb(min(downloaded / total * 100, 99))
    if progress_cb:
        progress_cb(100)
    return outpath


def transcribe(audio_path: Path, progress_cb=None) -> list[SubtitleEntry]:
    model = _get_model()
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
