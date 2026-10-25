#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话 cookie 验收（bcrypt 根治：一次校验 → HMAC 放行）。

覆盖：
  1. Basic 登录下发 HttpOnly + SameSite=Lax 的 scan_sess
  2. 仅靠 cookie 访问 /api/whoami → 身份与 Basic 一致，且**明显快于**冷 bcrypt
  3. 签名被篡改 → 降级为游客（不是 500、不是放行）
  4. 过期 token → 游客
  5. token 里的角色不作数（以凭据文件为准）→ 伪造 admin 无效
  6. 哈希前缀不匹配（模拟改密码/轮换）→ 游客
  7. cookie 认证的跨站写请求 → 403（CSRF 防线）
  8. /api/logout 清 cookie → 回到游客
  9. Basic 仍然完全可用（脚本/curl 兼容，不回归）

用法：python3 scripts/verify/verify_session.py
"""
from __future__ import annotations

import http.cookiejar
import time
import urllib.error
import urllib.request

from _common import BASE, basic_auth, chk, skip

try:                      # 仅服务器本地可用（要 import 仓库模块签发测试 token）
    from src.web.auth import SESSION_COOKIE, issue_session, load_accounts, load_session_secret
except Exception as exc:  # pragma: no cover
    raise SystemExit(skip("verify_session", f"无法导入 src.web.auth：{exc}"))


def _req(path: str, *, auth: str = "", cookie: str = "", method: str = "GET",
         origin: str = "", opener=None):
    """发请求，返回 (status, headers, body, elapsed_ms)。"""
    url = BASE + path
    data = b"" if method in ("POST", "PUT", "PATCH") else None
    r = urllib.request.Request(url, data=data, method=method)
    if auth:
        r.add_header("Authorization", auth)
    if cookie:
        r.add_header("Cookie", f"{SESSION_COOKIE}={cookie}")
    if origin:
        r.add_header("Origin", origin)
    t0 = time.time()
    try:
        with (opener or urllib.request.build_opener()).open(r, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, dict(resp.headers), body, (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, dict(e.headers), body, (time.time() - t0) * 1000


def main() -> int:
    auth = basic_auth()
    if not auth:
        return skip("verify_session", "缺管理员凭据（/root/.scan-web-cred）")

    secret = load_session_secret()
    if not secret:
        return skip("verify_session", "会话密钥不可用（/root/.scanweb-sess）")
    accounts = load_accounts()
    if not accounts:
        return skip("verify_session", "凭据文件无可登录账号")

    admin_user = next((u for u, (_, r) in accounts.items() if r == "admin"), "")
    if not admin_user:
        return skip("verify_session", "凭据文件无管理员账号")

    # ── 1. Basic 登录 → 下发 cookie ──────────────────────────────
    st, hdrs, body, basic_ms = _req("/api/whoami", auth=auth)
    cookie_hdr = hdrs.get("Set-Cookie", "") or hdrs.get("set-cookie", "")
    chk("Basic 登录成功（200）", st == 200, st)
    chk("响应带 Set-Cookie: scan_sess", SESSION_COOKIE in cookie_hdr, cookie_hdr[:80])
    chk("cookie 为 HttpOnly", "httponly" in cookie_hdr.lower(), cookie_hdr[:120])
    chk("cookie 为 SameSite=Lax", "samesite=lax" in cookie_hdr.replace(" ", "").lower(), cookie_hdr[:120])

    token = ""
    for part in cookie_hdr.split(";"):
        if part.strip().startswith(SESSION_COOKIE + "="):
            token = part.split("=", 1)[1].strip()
    if not token:
        return chk.finish("verify_session（无 cookie，后续用例无法执行）")

    basic_user = '"user":' in body and admin_user in body

    # ── 2. 仅 cookie：身份一致且快 ────────────────────────────────
    st2, _, body2, cookie_ms = _req("/api/whoami", cookie=token)
    chk("仅 cookie 可访问 whoami（200）", st2 == 200, st2)
    chk("cookie 身份与 Basic 一致", admin_user in body2 and '"role":"admin"' in body2.replace(" ", ""),
        body2[:120])
    chk(f"cookie 路径显著快于冷 bcrypt（{cookie_ms:.0f}ms vs {basic_ms:.0f}ms）",
        cookie_ms < 300 and cookie_ms < basic_ms, f"cookie={cookie_ms:.0f} basic={basic_ms:.0f}")

    # ── 3. 篡改签名 ─────────────────────────────────────────────
    bad = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    st3, _, body3, _ = _req("/api/whoami", cookie=bad)
    chk("签名被篡改 → 当作游客", st3 == 200 and '"role":"guest"' in body3.replace(" ", ""),
        f"{st3} {body3[:80]}")

    # ── 4. 过期 token ───────────────────────────────────────────
    stored = accounts[admin_user][0]
    expired = issue_session(admin_user, "admin", stored, secret, ttl=-60)
    st4, _, body4, _ = _req("/api/whoami", cookie=expired)
    chk("过期 token → 游客", '"role":"guest"' in body4.replace(" ", ""), body4[:80])

    # ── 5. 伪造角色（token 写 admin，账号其实是 user）─────────────
    user_acct = next(((u, h) for u, (h, r) in accounts.items() if r == "user"), None)
    if user_acct:
        forged = issue_session(user_acct[0], "admin", user_acct[1], secret)
        _, _, body5, _ = _req("/api/whoami", cookie=forged)
        chk("token 里伪造 admin 不生效（以凭据文件为准）",
            '"role":"user"' in body5.replace(" ", ""), body5[:100])
    else:
        print("  [SKIP] 无常规用户账号，跳过伪造角色用例")

    # ── 6. 哈希前缀不符（改密码/轮换哈希）───────────────────────
    rotated = issue_session(admin_user, "admin", "$2a$14$DIFFERENTHASH", secret)
    _, _, body6, _ = _req("/api/whoami", cookie=rotated)
    chk("哈希前缀不符（模拟轮换密码）→ 游客", '"role":"guest"' in body6.replace(" ", ""), body6[:80])

    # ── 7. CSRF：cookie 认证下的跨站写请求 ──────────────────────
    st7, _, _, _ = _req("/api/logout", cookie=token, method="POST", origin="https://evil.example")
    chk("跨站 Origin 的写请求被拒（403）", st7 == 403, st7)
    st7b, _, _, _ = _req("/api/logout", cookie=token, method="POST", origin=BASE)
    chk("同站 Origin 的写请求放行（200）", st7b == 200, st7b)

    # ── 8. logout 清 cookie ────────────────────────────────────
    st8, hdrs8, _, _ = _req("/api/logout", cookie=token, method="POST", origin=BASE)
    clear = hdrs8.get("Set-Cookie", "") or hdrs8.get("set-cookie", "")
    chk("logout 返回清除指令（Max-Age=0 / expires 过去）",
        st8 == 200 and ("max-age=0" in clear.lower() or "expires=thu, 01 jan 1970" in clear.lower()),
        clear[:120])

    # ── 9. Basic 兼容性 ────────────────────────────────────────
    st9, _, body9, _ = _req("/api/history", auth=auth)
    chk("Basic 仍可用（/api/history 200）", st9 == 200, st9)

    return chk.finish("verify_session")


if __name__ == "__main__":
    raise SystemExit(main())
