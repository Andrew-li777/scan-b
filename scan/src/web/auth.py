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
import time
from collections import defaultdict
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
