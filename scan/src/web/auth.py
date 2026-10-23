#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鉴权中间件（通用，两服务共用逻辑）

三种角色（2026-10-15 起，见 `src/web/roles.py`）
------------------------------------------------
- 无 Authorization 头                  → 游客（只读白名单）
- 有头且 bcrypt 校验通过              → 按凭据文件里的角色：`admin`（全放行）/ `user`（受限）
- 有头但校验失败                      → 401（不降级为游客，防伪装）

凭据文件格式
------------
单账号（旧格式，仍完全支持；**缺 `角色:` 行 = admin**）：

    账号: scanuser
    密码: ********        # 明文仅供人读，程序不使用
    bcrypt: $2a$14$...

多账号：出现第二个 `账号:` 行即开始新记录（顺序无关，键名支持中英文）：

    账号: scanuser
    bcrypt: $2a$14$...
    角色: admin

    账号: alice
    bcrypt: $2a$14$...
    角色: user

用法：
    app.add_middleware(AuthMiddleware,
                       accounts=load_accounts(),          # {账号: (bcrypt, 角色)}
                       is_read_allowed=fn(method, path) -> bool,   # 游客白名单
                       member_allowed=fn(method, path) -> bool,    # 登录用户白名单
                       rate_limits=[(prefix, limit_per_min, window_sec)])

哈希**绝不进公开仓库**：读不到哈希的账号一律不可登录（fail closed），
游客浏览不受影响。
"""
import base64
import hashlib
import hmac
import os
import threading
import time
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from src.web.roles import ADMIN, GUEST, USER, normalize_role

try:
    import bcrypt
except ImportError:  # pragma: no cover
    bcrypt = None


# ── 校验结果缓存：bcrypt($2a$14$) 单次 ≈ 1.2 秒，而浏览器切一条记录要发 3+ 个带凭据的
# 请求 —— 不缓存就是每条记录白白多等 3.6 秒（实测：游客 1ms / 登录 1182ms，同一端点）。
# 键里带「密码的 sha256」和「存储哈希的前 12 位」：
#   * 不存明文密码；
#   * 换密码 → 摘要变 → 自然不复用旧记录；
#   * 轮换同一密码的哈希（比如调 cost）→ 存储哈希变 → 也立刻失效。
# 只缓存**校验成功**的结果，所以攻击者无法用错误密码把缓存打满；仍然设上限兜底。
_VERIFIED: "OrderedDict[tuple[str, str, str], tuple[str, str]]" = OrderedDict()
_VERIFIED_LOCK = threading.Lock()
_VERIFIED_MAX = 256


def _cache_get(key):
    with _VERIFIED_LOCK:
        hit = _VERIFIED.get(key)
        if hit is not None:
            _VERIFIED.move_to_end(key)
        return hit


def _cache_put(key, value):
    with _VERIFIED_LOCK:
        _VERIFIED[key] = value
        _VERIFIED.move_to_end(key)
        while len(_VERIFIED) > _VERIFIED_MAX:
            _VERIFIED.popitem(last=False)


def _cache_clear() -> None:
    """测试/轮换凭据后用：清空缓存（服务重启也会清）。"""
    with _VERIFIED_LOCK:
        _VERIFIED.clear()


def verify_credentials(
    auth_header: str, accounts: dict[str, tuple[str, str]]
) -> tuple[str, str] | None:
    """解析 Basic 凭据并核对。成功返回 `(账号, 角色)`，失败返回 `None`。

    - 账号名用 bytes 常量时间比较：`hmac.compare_digest` 对含非 ASCII 的 str 会直接抛
      TypeError，攻击者/扫描器发一个畸形 Basic 头就能把鉴权路径打成 500（真机实测过）。
    - 密码用 bcrypt 校验；无 bcrypt 库时退回明文常量比较（仅当存的是明文时可用）。
    - 不提前 return，遍历完所有账号再定论（减少"账号是否存在"的时序侧信道）。
    """
    if not auth_header or not auth_header.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
        user, _, password = raw.partition(":")
    except Exception:
        return None
    if not user:
        return None

    hit: tuple[str, str] | None = None
    for name, (stored, role) in accounts.items():
        if not stored:
            continue
        try:
            if hmac.compare_digest(user.encode("utf-8"), name.encode("utf-8")):
                hit = (name, stored)
                _ = role
        except Exception:
            continue
    if hit is None:
        return None

    name, stored = hit
    # 命中缓存 → 免掉 1.2 秒的 bcrypt（键含密码摘要 + 存储哈希指纹，改密码/换哈希都会失效）
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    ckey = (name, stored[:12], digest)
    cached = _cache_get(ckey)

    if bcrypt is None:
        try:
            ok = hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            ok = False
    elif cached is not None:
        ok = True
    else:
        try:
            ok = bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except ValueError:
            ok = False
        except Exception:
            ok = False
    if not ok:
        return None

    role = normalize_role(accounts.get(name, ("", ""))[1])
    # 游客身份不能由凭据文件授予（否则等于"有账号但无权限"的第三种怪状态）
    if role == GUEST:
        role = USER
    if cached is None:
        _cache_put(ckey, (name, role))
    return name, role


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        accounts: dict[str, tuple[str, str]] | None = None,
        admin_user: str = "",
        admin_hash: str = "",
        is_read_allowed: Callable[[str, str], bool],
        member_allowed: Callable[[str, str], bool] | None = None,
        rate_limits: list[tuple[str, int, int]] | None = None,
    ):
        super().__init__(app)
        self.accounts: dict[str, tuple[str, str]] = dict(accounts or {})
        # 兼容旧调用方式（传单个 admin_user/admin_hash）
        if admin_user and admin_hash:
            self.accounts.setdefault(admin_user, (admin_hash, ADMIN))
        self.is_read_allowed = is_read_allowed
        self.member_allowed = member_allowed
        self.rate_limits = rate_limits or []
        self._hits: dict[tuple[str, str], list[float]] = defaultdict(list)

    def _rate_ok(self, ip: str, path: str) -> tuple[bool, int]:
        """返回 (是否放行, 429 或 0)。每前缀独立计数。"""
        now = time.time()
        for prefix, limit, window in self.rate_limits:
            if path.startswith(prefix):
                key = (prefix, ip)
                bucket = [t for t in self._hits[key] if now - t < window]
                bucket.append(now)
                self._hits[key] = bucket[-limit * 2:]  # 防止无限增长
                if len(bucket) > limit:
                    return False, 429
        return True, 0

    async def dispatch(self, request, call_next):
        path = request.url.path
        method = request.method
        auth = request.headers.get("authorization", "")

        # 身份写进 scope：路由层要靠它做行级可见性判断（列表过滤 / 详情 404）
        state = request.scope.setdefault("state", {})
        state["user"], state["role"] = "", GUEST

        if auth.strip():
            hit = verify_credentials(auth, self.accounts)
            if hit is None:
                return JSONResponse({"detail": "凭据无效"}, status_code=401)
            user, role = hit
            state["user"], state["role"] = user, role

            if role != ADMIN:
                if role == USER and self.member_allowed is not None:
                    allowed = self.member_allowed(method, path)
                else:
                    allowed = method in ("GET", "HEAD", "OPTIONS") and self.is_read_allowed(method, path)
                if not allowed:
                    return JSONResponse(
                        {"detail": "权限不足：该操作需要管理员", "role": role},
                        status_code=403,
                        headers={"X-Role": role},
                    )
            ok, code = self._rate_ok(request.client.host if request.client else "?", path)
            if not ok:
                return JSONResponse({"detail": "请求过于频繁"}, status_code=code)
            return await call_next(request)

        # 游客：只读白名单
        if method in ("GET", "HEAD", "OPTIONS") and self.is_read_allowed(method, path):
            ok, code = self._rate_ok(request.client.host if request.client else "?", path)
            if not ok:
                return JSONResponse({"detail": "请求过于频繁"}, status_code=code)
            return await call_next(request)

        return JSONResponse(
            {"detail": "游客只读模式：此操作需要登录后使用"},
            status_code=403,
            headers={"X-Guest-Only": "1"},
        )


def load_accounts(path: str | None = None) -> dict[str, tuple[str, str]]:
    """从**服务器本地**凭据文件读取全部账号 → `{账号: (bcrypt 哈希, 角色)}`。

    ⚠️ 哈希绝不能写进代码仓库（仓库是公开的）：bcrypt 不可逆，但可离线爆破，
    且 hash 一旦泄漏，不轮换密码就无法撤回。

    解析规则（键名支持中英文，分隔符 `:` 或 `=`）：
    - 见到 `账号:`/`user:` 就开始一条新记录（把上一条 flush 掉）→ 天然支持多账号
    - `bcrypt:`/`hash:` 作为哈希；`角色:`/`role:` 作为角色
    - **没有 `角色:` 行 → 视为 admin**（旧单账号文件零改动继续可用）
    - 只有同时有账号名和哈希的记录才会生效（缺哈希 = 该账号不可登录，fail closed）
    - `密码:` 行仅给人读，程序忽略

    路径优先级：参数 > 环境变量 `ADMIN_CRED_FILE` > `/root/.scan-web-cred`。
    """
    p = Path(path or os.environ.get("ADMIN_CRED_FILE", "/root/.scan-web-cred"))
    accounts: dict[str, tuple[str, str]] = {}
    cur_user = ""
    cur_hash = ""
    cur_role = ""

    def flush() -> None:
        nonlocal cur_user, cur_hash, cur_role
        if cur_user and cur_hash:
            accounts[cur_user] = (cur_hash, cur_role or ADMIN)
        cur_user, cur_hash, cur_role = "", "", ""

    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            cands = [i for i in (line.find(":"), line.find("=")) if i > 0]
            if not cands:
                continue
            idx = min(cands)
            k = line[:idx].strip().lower()
            v = line[idx + 1:].strip()
            if k in ("账号", "user", "username", "name"):
                flush()
                cur_user = v
            elif k in ("bcrypt", "hash", "bcrypt_hash", "passhash"):
                cur_hash = v
            elif k in ("角色", "role"):
                cur_role = normalize_role(v)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    flush()
    return accounts


def load_admin_cred(path: str | None = None) -> tuple[str, str]:
    """相容旧接口：返回**第一个管理员账号**的 `(账号, bcrypt)`；没有则 `("", "")`。

    新代码请用 `load_accounts()`。
    """
    accounts = load_accounts(path)
    for name, (h, role) in accounts.items():
        if role == ADMIN:
            return name, h
    return "", ""
