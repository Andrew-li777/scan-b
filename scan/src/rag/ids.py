"""RAG 集合名规则 —— 唯一权威实现（前端/后端/补索引三处共用）。

规则（可从记录内容复现，不依赖运行时的临时 task_id）：
- bilibili 普通视频:  <BV号>            例: BV1dBYU6jEWn
- bilibili 分P/合集:  <BV号>_p<N>       例: BV1DbYL65EGh_p9
- 其他平台:           沿用 pipeline 的 URL 尾段规则（sanitize）

为什么要有这个模块：
批量/分P 解析时 pipeline 给每个任务分配的是随机 UUID（v_xxx），页面一刷新就
无处可查 —— 历史记录因此永远找不到自己的索引，问答只能得到"未提及"。
改成「从 meta.json 的 url 派生」后，任何时刻打开任意历史记录都能算出同一集合名。
"""
from __future__ import annotations

import hashlib
import re

_BV = re.compile(r"(BV[0-9A-Za-z]{10})")
_P = re.compile(r"[?&]p=(\d+)")
_BAD = re.compile(r"[^a-zA-Z0-9._-]")


def sanitize(raw: str) -> str:
    """ChromaDB 安全名（与 src/web/pipeline.py 原 sanitize_task_id 规则一致）。"""
    cleaned = _BAD.sub("_", raw or "").strip("_.-")
    if not cleaned or len(cleaned) < 3:
        cleaned = "task_" + hashlib.md5((raw or "").encode()).hexdigest()[:16]
    elif not cleaned[0].isalpha():
        cleaned = "v_" + cleaned
    if cleaned and not cleaned[-1].isalnum():
        cleaned = cleaned.rstrip("_.-")
    return cleaned[:63]


def url_tail_id(url: str) -> str:
    """单条解析场景的历史命名：取 URL 尾段做安全名。"""
    return sanitize((url or "").rstrip("/").rsplit("/", 1)[-1][:50])


def task_id_for_url(url: str) -> str:
    """记录 → RAG 集合名（当前规则）。"""
    url = url or ""
    m = _BV.search(url)
    if m:
        p = _P.search(url)
        return f"{m.group(1)}_p{p.group(1)}" if p else m.group(1)
    return url_tail_id(url)


def legacy_url_tail_id(url: str) -> str:
    """升级前的历史集合名（单条解析路径用的就是它；批量路径的随机 UUID 无法复现，故只列这一种）。"""
    return url_tail_id(url)


def legacy_ids(url: str) -> list[str]:
    """排查用的历史集合名候选：BV 号（旧的 BV-only 命名）+ URL 尾段命名，去重。"""
    out: list[str] = []
    m = _BV.search(url or "")
    if m:
        out.append(m.group(1))
    tail = url_tail_id(url)
    if tail and tail not in out:
        out.append(tail)
    return out
