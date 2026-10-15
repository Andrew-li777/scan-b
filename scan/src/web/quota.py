#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析配额账本（常规用户）

用户 2026-10-15 拍板的口径
--------------------------
- 配额**只约束常规用户**（`role=user`）；管理员不受限，游客根本不解析。
- 计量口径是 **(b) 按实际产出的记录数**：提交时**不**扣，真正写出记录才 +1；
  失败/取消/命中缓存回放都不计（失败返还）。
- 每个账号**同时在飞**任务 ≤ `settings.user_inflight_limit`（默认 2）。
  这条与配额是两件事：配额限**速率**（日上限），在飞限**并发**；
  防 OOM 靠的是 `pipeline` 里的全局流水线闸，三者都要有。

为什么提交时不预扣
------------------
预扣 + 事后返还看起来更"严"，但一旦进程被 OOM 杀掉（本机 3.9GB 是常态风险），
预扣的记录永远还不回来 —— 用户会莫名其妙丢配额。只在**产出时** +1，
进程崩了最多是那次解析白送，不会错扣。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from src.config import PROJECT_ROOT, settings
from src.web.roles import ADMIN, USER

_lock = threading.Lock()

# 已产出的条数按天存盘；在飞名额只活在内存里（进程重启 = 在飞任务全没了）
_days: dict[str, dict[str, int]] = {}
_inflight: dict[str, int] = {}
_loaded = False

_KEEP_DAYS = 7


def _path() -> Path:
    p = Path(settings.quota_file or "data/quota.json")
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load_locked() -> None:
    """懒加载（调用方必须已持锁）。文件坏了就当空账本，不阻断服务。"""
    global _days, _loaded
    if _loaded:
        return
    _loaded = True
    p = _path()
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            days = raw.get("days") if isinstance(raw, dict) else None
            if isinstance(days, dict):
                _days = {str(k): {str(u): int(v) for u, v in (d or {}).items()}
                         for k, d in days.items() if isinstance(d, dict)}
    except Exception:
        _days = {}


def _save_locked() -> None:
    """写盘（调用方必须已持锁）。先写临时文件再替换，避免半截 JSON。"""
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"days": _days}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _prune_locked() -> None:
    keys = sorted(_days.keys())
    if len(keys) > _KEEP_DAYS:
        for k in keys[:-_KEEP_DAYS]:
            _days.pop(k, None)


def used_today(user: str) -> int:
    with _lock:
        _load_locked()
        return int(_days.get(_today(), {}).get(user, 0))


def inflight(user: str) -> int:
    with _lock:
        return int(_inflight.get(user, 0))


def limit_for(role: str) -> int | None:
    """该角色的日配额上限；None = 不限。"""
    return None if role == ADMIN else int(settings.user_daily_quota or 0)


def check(user: str, role: str) -> tuple[bool, str]:
    """能不能再提交一条？返回 (可否, 原因)。**不占名额**（用 reserve 才占）。"""
    if role == ADMIN:
        return True, ""
    if role != USER:
        return False, "需要登录后才能解析"
    lim = limit_for(role)
    if lim is not None and used_today(user) >= lim:
        return False, f"今日解析配额已用完（{lim} 条/天），明天再试"
    if _inflight.get(user, 0) >= int(settings.user_inflight_limit or 1):
        return False, f"你还有任务正在解析（同时最多 {settings.user_inflight_limit} 个），请等它跑完"
    return True, ""


def reserve(user: str, role: str) -> tuple[bool, str]:
    """检查并占一个在飞名额。成功返回 (True, "")，失败 (False, 原因)。"""
    with _lock:
        _load_locked()
        if role == ADMIN:
            return True, ""
        if role != USER:
            return False, "需要登录后才能解析"
        lim = limit_for(role)
        if lim is not None and int(_days.get(_today(), {}).get(user, 0)) >= lim:
            return False, f"今日解析配额已用完（{lim} 条/天），明天再试"
        if _inflight.get(user, 0) >= int(settings.user_inflight_limit or 1):
            return False, f"你还有任务正在解析（同时最多 {settings.user_inflight_limit} 个），请等它跑完"
        _inflight[user] = _inflight.get(user, 0) + 1
        return True, ""


def settle(user: str, produced: bool) -> None:
    """任务结束：释放名额；`produced=True`（真写出了记录）才计一条。"""
    if not user:
        return
    with _lock:
        _load_locked()
        _inflight[user] = max(0, _inflight.get(user, 0) - 1)
        if produced:
            day = _days.setdefault(_today(), {})
            day[user] = int(day.get(user, 0)) + 1
            _prune_locked()
            _save_locked()


def status(user: str, role: str) -> dict:
    """给 /api/whoami 用：剩余额度概览。"""
    with _lock:
        _load_locked()
        lim = None if role == ADMIN else int(settings.user_daily_quota or 0)
        used = int(_days.get(_today(), {}).get(user, 0)) if user else 0
        fly = int(_inflight.get(user, 0)) if user else 0
    return {
        "date": _today(),
        "limit": lim,                       # None = 不限（管理员）
        "used": used,
        "remaining": None if lim is None else max(0, lim - used),
        "inflight": fly,
        "inflight_limit": None if role == ADMIN else int(settings.user_inflight_limit or 0),
    }


def reset(user: str | None = None) -> None:
    """清零（运维/测试用）：给 user 就清它，否则全清。"""
    with _lock:
        _load_locked()
        if user:
            for day in _days.values():
                day.pop(user, None)
            _inflight.pop(user, None)
        else:
            _days.clear()
            _inflight.clear()
        _save_locked()
