import asyncio
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path

import httpx

from src.extractors.base import BaseExtractor, clean_url
from src.models import SubtitleEntry, VideoMeta

_BILI_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?bilibili\.com/video/(BV[\w]+)"
)
# 合集/收藏夹列表页：旧格式 space.bilibili.com/<uid>/lists/<sid>，
# 新格式 bilibili.com/list/<uid>/?sid=<sid>（B站迁移后的链接形态）
_BILI_COLLECTION_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?bilibili\.com/list/\d+/?(?:\?[^#]*)?(?:[?&]sid=\d+)"
    r"|(?:https?://)?space\.bilibili\.com/\d+/lists/\d+"
)

_BILI_API_INFO = "https://api.bilibili.com/x/web-interface/view"
_BILI_API_PLAYER = "https://api.bilibili.com/x/player/v2"
_BILI_API_PLAYER_WBI = "https://api.bilibili.com/x/player/wbi/v2"
_BILI_API_NAV = "https://api.bilibili.com/x/web-interface/nav"
_BILI_API_PLAYURL = "https://api.bilibili.com/x/player/playurl"
_BILI_API_PAGELIST = "https://api.bilibili.com/x/player/pagelist"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

_BASE_HEADERS = {
    "Referer": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.bilibili.com",
}


def _load_cookie_str() -> str:
    """Load Bilibili cookies from Netscape-format file (BILIBILI_COOKIES env var).

    优先取 os.environ；systemd 服务未注入 .env 时，兜底直接从项目 .env 读路径。
    """
    cookie_path = os.environ.get("BILIBILI_COOKIES", "")
    if not cookie_path:
        try:
            _envp = Path(__file__).resolve().parent.parent.parent / ".env"
            for _line in _envp.read_text(encoding="utf-8").splitlines():
                if _line.startswith("BILIBILI_COOKIES="):
                    cookie_path = _line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            return ""
    if not cookie_path:
        return ""
    try:
        p = Path(cookie_path)
        if not p.exists():
            p = Path.cwd() / cookie_path
        if not p.exists():
            return ""
        cookies = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("HttpOnly "):
                line = line[9:]
            parts = line.split("\t")
            if len(parts) >= 7:
                domain = parts[0]
                name, value = parts[5], parts[6]
                if "bilibili.com" in domain:
                    cookies.append(f"{name}={value}")
        return "; ".join(cookies)
    except Exception:
        return ""


_COOKIE_STR = _load_cookie_str()


def _get_headers(ua_index: int = 0) -> dict:
    h = {
        "User-Agent": _USER_AGENTS[ua_index % len(_USER_AGENTS)],
        **_BASE_HEADERS,
    }
    if _COOKIE_STR:
        h["Cookie"] = _COOKIE_STR
    return h


def _retry_get(url: str, params: dict = None, max_retries: int = 3) -> httpx.Response:
    """GET with UA rotation and exponential backoff on 412."""
    for attempt in range(max_retries):
        headers = _get_headers(attempt)
        r = httpx.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 412 and attempt < max_retries - 1:
            time.sleep(1.5 ** attempt + random.uniform(0, 0.5))
            continue
        r.raise_for_status()
        return r
    # unreachable
    raise httpx.HTTPStatusError("max retries exceeded", request=None, response=r)


_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_wbi_mixin_cache: dict = {"key": "", "ts": 0.0}


def _get_mixin_key() -> str:
    """Fetch & cache the WBI mixin key (official reorder table). Empty on failure.

    注意：未登录（code=-101）时 nav 仍返回 wbi_img，因此只看字段在场与否，
    不能按 code != 0 直接放弃——否则 AI 字幕主链路会在无 cookie 时静默失效。
    """
    now = time.time()
    if _wbi_mixin_cache["key"] and now - _wbi_mixin_cache["ts"] < 3600:
        return _wbi_mixin_cache["key"]
    try:
        r = httpx.get(_BILI_API_NAV, headers=_get_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        wbi = (data.get("data") or {}).get("wbi_img") or {}
        img_url = wbi.get("img_url", "")
        sub_url = wbi.get("sub_url", "")
        if not img_url or not sub_url:
            return ""
        img_key = img_url.rsplit("/", 1)[1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
        raw = img_key + sub_key
        mixin = "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]
        _wbi_mixin_cache.update(key=mixin, ts=now)
        return mixin
    except Exception:
        return ""


def _sign_wbi(params: dict) -> dict:
    """Apply WBI signature to params. Falls back to unsigned params on failure."""
    mixin = _get_mixin_key()
    if not mixin:
        return params
    params = dict(sorted(params.items()))
    params["wts"] = int(time.time())
    query = "&".join(f"{k}={v}" for k, v in params.items())
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params


def fetch_bilibili_audio_url(bvid: str, cid: int) -> tuple[str, str] | None:
    """Get DASH audio URL from Bilibili playurl API. Returns (url, mime_type) or None.
    优先取 DASH 音频流；若该视频无 DASH（仅 durl 分段流），回退到第一个分段的
    durl URL（视频流，whisper/ffmpeg 可提取其中的音频轨）。"""
    try:
        params = {"bvid": bvid, "cid": cid, "fnval": 4048, "fourk": 1}
        r = _retry_get(_BILI_API_PLAYURL, params=params)
        data = r.json()
        d = data.get("data") or {}
        audios = d.get("dash", {}).get("audio", [])
        if audios:
            best = max(audios, key=lambda a: a.get("bandwidth", 0))
            return best["baseUrl"], best.get("mimeType", "audio/mp4")
        # 回退：无 DASH 时使用 durl 分段流（首段）
        durl = d.get("durl") or []
        if durl:
            return durl[0].get("url"), durl[0].get("mimeType", "video/mp4")
        return None
    except Exception:
        return None


def expand_collection(url: str) -> tuple[list[str], str]:
    """Expand a Bilibili collection URL into (video_urls, author_name).
    Uses yt-dlp's BilibiliCollectionList extractor.
    """
    m = _BILI_COLLECTION_URL_PATTERN.search(url)
    if not m:
        return [], ""

    import yt_dlp as _yt_dlp
    opts: dict = {"quiet": True, "extract_flat": True}
    if _COOKIE_STR:
        # 合集页在无登录态下常被 412 风控拦（实测旧格式 URL）；带 cookie 后改走 API 可展开
        opts["http_headers"] = {"Cookie": _COOKIE_STR, "User-Agent": _USER_AGENTS[0]}
    try:
        with _yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get("entries", [])
        uploader = info.get("uploader", "") or ""
        urls = [
            e.get("url") or f"https://www.bilibili.com/video/{e['id']}"
            for e in entries if e.get("id")
        ]
        return urls, uploader
    except Exception:
        return [], ""


def expand_pages(url: str) -> tuple[list[str], str]:
    """展开 B站分P 视频（一个 BV 内的多个分节）为 N 个 ?p=N 链接。

    - 单 P（普通视频）：返回空 —— 不劫持常规单视频解析路径
    - 多 P：返回 [`https://www.bilibili.com/video/{BV}?p=1..N`] + 作者名
    数据源：x/player/pagelist（免登录可读，已实测 200）
    """
    m = _BILI_URL_PATTERN.search(url)
    if not m:
        return [], ""
    bvid = m.group(1)
    try:
        r = _retry_get(_BILI_API_PAGELIST, params={"bvid": bvid})
        data = r.json()
        if data.get("code") != 0:
            return [], ""
        pages = data.get("data") or []
        if len(pages) <= 1:
            return [], ""
        urls = [
            f"https://www.bilibili.com/video/{bvid}?p={i}"
            for i in range(1, len(pages) + 1)
        ]
        author = ""
        try:
            info = _retry_get(_BILI_API_INFO, params={"bvid": bvid}).json().get("data") or {}
            author = (info.get("owner") or {}).get("name", "")
        except Exception:
            pass
        return urls, author
    except Exception:
        return [], ""


class BilibiliExtractor(BaseExtractor):
    platform = "bilibili"

    def match(self, url: str) -> bool:
        return bool(_BILI_URL_PATTERN.search(url))

    def extract(self, url: str) -> VideoMeta:
        bvid = self._parse_bvid(url)
        page = self._parse_page(url)
        info = self._fetch_info(bvid, page=page)
        cid = info.get("cid", 0)
        self.last_subtitle_source = ""
        subs = []
        try:
            subs = self._fetch_subtitles(bvid, cid, aid=info.get("aid"))
        except RuntimeError:
            pass  # 无字幕，server.py 会走 Whisper 回退

        # 分P：duration 以对应 P 为准（view 顶层 duration 是所有 P 的总和，会误导分块/超时）
        pages = info.get("pages") or []
        duration = int(info.get("duration", 0) or 0)
        if pages and 1 <= page <= len(pages):
            duration = int(pages[page - 1].get("duration", duration) or duration)
        title = str(info.get("title", "") or "")
        if re.search(r"[?&]p=\d+", url):
            # 显式带 ?p= 的请求统一加前缀标记（含 P1）：pipeline 会按标题前 60 字符
            # 截断生成输出目录（src/web/pipeline.py base_name[:60]），标记放**开头**
            # 才不会被截掉 —— 否则 7 个 P 全部塌缩进同一目录互相覆盖
            title = f"[P{page}] {title}"
        return VideoMeta(
            platform="bilibili",
            video_id=bvid,
            title=title,
            url=clean_url(url),
            duration=duration,
            author=info.get("owner", {}).get("name", ""),
            thumbnail=info.get("pic", ""),
            subtitles=subs,
            subtitle_source=self.last_subtitle_source,
        )

    def _parse_bvid(self, url: str) -> str:
        m = _BILI_URL_PATTERN.search(url)
        if not m:
            raise ValueError(f"无法解析 Bilibili URL: {url}")
        return m.group(1)

    @staticmethod
    def _parse_page(url: str) -> int:
        """从 URL 取分P 序号（?p=N），缺省 1。"""
        m = re.search(r"[?&]p=(\d+)", url)
        return max(1, int(m.group(1))) if m else 1

    def _fetch_info(self, bvid: str, page: int = 1) -> dict:
        # p 参数让 view 接口返回对应分P 的 cid（默认 1 = 第一 P）
        r = _retry_get(_BILI_API_INFO, params={"bvid": bvid, "p": page})
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(
                f"B站 API 错误 (code={data.get('code')}): {data.get('message', 'unknown')}"
            )
        return data["data"]

    last_subtitle_source: str = ""

    def _fetch_subtitles(self, bvid: str, cid: int, aid: int | None = None) -> list[SubtitleEntry]:
        """Fetch subtitles with priority: B站 AI 字幕 → CC 字幕.

        AI 字幕（ai_status==2）来自 wbi/v2 端点，是 B 站服务端已转写好的结果，
        纯 HTTP 拉取即可（普通网络、零模型、零 GPU）——这是离线识别的主链路。
        """
        params = {"bvid": bvid, "cid": cid}
        if aid:
            params["aid"] = aid

        # 1) WBI-signed endpoint: returns CC + AI subtitles
        subs_list = self._list_subtitles(_BILI_API_PLAYER_WBI, _sign_wbi(params))
        endpoint_note = "wbi/v2"
        if not subs_list:
            # 2) unsigned player/v2: CC subtitles only
            subs_list = self._list_subtitles(_BILI_API_PLAYER, params)
            endpoint_note = "player/v2"

        if not subs_list:
            raise RuntimeError(
                "该视频没有可用字幕（无 CC 字幕，也无 B 站 AI 字幕，"
                "将回退 Whisper 语音识别）"
            )

        chosen = _pick_best_subtitle(subs_list)
        lan = str(chosen.get("lan", ""))
        is_ai = chosen.get("ai_status") == 2 or lan.lower().startswith("ai-")
        self.last_subtitle_source = f"{'AI字幕' if is_ai else 'CC字幕'}({lan}@{endpoint_note})"
        return self._download_subtitle(chosen)

    def _list_subtitles(self, endpoint: str, params: dict) -> list[dict]:
        """Return the raw subtitle entry list from a player endpoint ([] on failure)."""
        try:
            r = _retry_get(endpoint, params=params)
            data = r.json()
            return data.get("data", {}).get("subtitle", {}).get("subtitles", []) or []
        except Exception:
            return []

    def _download_subtitle(self, sub: dict) -> list[SubtitleEntry]:
        sub_url = sub.get("subtitle_url", "")
        if not sub_url:
            return []
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        elif sub_url.startswith("/"):
            sub_url = "https://i0.hdslb.com" + sub_url
        try:
            sub_r = _retry_get(sub_url)
            entries = sub_r.json().get("body", [])
        except Exception:
            return []
        return [
            SubtitleEntry(start=float(e["from"]), end=float(e["to"]), text=e["content"])
            for e in entries
        ]


def _pick_best_subtitle(subtitles: list[dict]) -> dict:
    """Prefer ready AI subtitles (ai_status==2), then zh, then anything."""
    def _score(s: dict) -> int:
        lan = str(s.get("lan", "")).lower().replace("-", "")
        ai = s.get("ai_status") == 2 or lan.startswith("ai")
        score = 100 if ai else 0
        if "zh" in lan:
            score += 10
        return score

    return max(subtitles, key=_score)
