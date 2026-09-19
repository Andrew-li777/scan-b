#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游客/管理员鉴权中间件（通用，两服务共用逻辑）

- 无 Authorization 头            → 游客（只读白名单）
- 有头且 bcrypt 校验通过（用户+密码）→ 管理员（全功能）
- 有头但校验失败                → 401

用法：
    app.add_middleware(AuthMiddleware,
                       admin_user="translate",
                       admin_hash="$2a$14$...",
                       is_read_allowed=fn(method, path) -> bool,
                       rate_limits=[(prefix, limit_per_min, window_sec)])

is_read_allowed 返回 True 表示游客放行；返回 False 表示需管理员。
"""
import base64
import hmac
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

try:
    import bcrypt
except ImportError:  # pragma: no cover
    bcrypt = None


def _basic_ok(auth_header: str, admin_user: str, admin_hash: str) -> bool:
    """解析 Basic 凭据并校验。常量时间比较，防时序攻击。"""
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
        user, _, password = raw.partition(":")
    except Exception:
        return False
    if not hmac.compare_digest(user, admin_user):
        return False
    if bcrypt is None:
        # 无 bcrypt 库时退回明文常量比较（仅当 admin_hash 是明文时可用）
        return hmac.compare_digest(password, admin_hash)
    try:
        return bcrypt.checkpw(password.encode("utf-8"), admin_hash.encode("utf-8"))
    except ValueError:
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        admin_user: str,
        admin_hash: str,
        is_read_allowed: Callable[[str, str], bool],
        rate_limits: list[tuple[str, int, int]] | None = None,
    ):
        super().__init__(app)
        self.admin_user = admin_user
        self.admin_hash = admin_hash
        self.is_read_allowed = is_read_allowed
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

        # 管理员：凭据有效 → 全放行
        if _basic_ok(auth, self.admin_user, self.admin_hash):
            return await call_next(request)

        # 有凭据但无效 → 401（不降级为游客，防伪装）
        if auth.strip():
            return JSONResponse({"detail": "凭据无效"}, status_code=401)

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


def load_admin_cred(path: str | None = None) -> tuple[str, str]:
    """从**服务器本地**凭据文件读取管理员账号与 bcrypt 哈希。

    ⚠️ 哈希绝不能写进代码仓库（仓库是公开的）：bcrypt 不可逆，但可离线爆破，
    且 hash 一旦泄漏，不轮换密码就无法撤回。

    文件格式（键名支持中英文，分隔符 `:` 或 `=`）：
        账号: scanuser           # 或 user=scanuser
        密码: ********            # 明文仅供人读，程序不使用
        bcrypt: $2a$14$...       # 或 hash=

    路径优先级：参数 > 环境变量 `ADMIN_CRED_FILE` > `/root/.scan-web-cred`。
    读不到哈希时返回 `(user, "")` —— 管理员登录**一律失败（fail closed）**，
    游客只读浏览不受影响。
    """
    p = Path(path or os.environ.get("ADMIN_CRED_FILE", "/root/.scan-web-cred"))
    user, admin_hash = "", ""
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
            if k in ("账号", "user", "username"):
                user = v
            elif k in ("bcrypt", "hash", "bcrypt_hash", "passhash"):
                admin_hash = v
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return user, admin_hash
