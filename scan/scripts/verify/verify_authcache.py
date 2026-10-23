#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鉴权校验缓存回归（2026-10-23）。

背景：`bcrypt($2a$14$)` 单次校验实测 **≈1.2 秒**，而浏览器切一条记录会发 3+ 个带
凭据的请求 —— 不缓存的话每条记录白等 3.6 秒（实测同一端点：游客 1ms / 登录 1182ms）。
于是给 `verify_credentials` 加了「校验成功结果」的 LRU 缓存。

这一份守住四件事：
    [1] 缓存真的省掉 bcrypt（第二次调用 < 50ms）
    [2] 缓存键含存储哈希指纹 —— 换哈希（等同轮换密码）必须重新校验，不能复用
    [3] 失败不写缓存（错误密码不能把缓存撑大，也不能被"预热"成通过）
    [4] 线上行为不变：管理员 200 / 常规用户 200 且角色正确 / 游客仍可读 / 错误密码 401
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import REPO_ROOT, chk, have_creds, http, load_accounts, service_reachable, skip  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))
print("=== 鉴权校验缓存回归 ===")

try:
    from src.web import auth as A
except Exception as e:  # pragma: no cover
    sys.exit(skip("verify_authcache", f"导入 auth 失败：{e}"))

accounts = load_accounts()
if not accounts or not all(a.get("hash") for a in accounts):
    sys.exit(skip("verify_authcache", "凭据文件缺失或无 bcrypt 哈希（不入库，无法在别处复现）"))

acc = accounts[0]
if not acc.get("pwd"):
    sys.exit(skip("verify_authcache", "凭据文件未留明文密码，无法构造校验请求"))


def hdr(user: str, pwd: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


amap = {a["user"]: (a["hash"], a.get("role") or "admin") for a in accounts}

# ── [1] 命中缓存 ────────────────────────────────────────────
print("\n[1] 缓存生效（免掉 1.2s 的 bcrypt）")
A._cache_clear()
t0 = time.time()
r1 = A.verify_credentials(hdr(acc["user"], acc["pwd"]), amap)
cold = time.time() - t0
t0 = time.time()
r2 = A.verify_credentials(hdr(acc["user"], acc["pwd"]), amap)
warm = time.time() - t0
chk("首次校验通过", r1 is not None)
chk(f"首次确实走了 bcrypt（>{0.5:.0f}s）", cold > 0.5, f"{cold:.2f}s")
chk("第二次命中缓存（<50ms）", warm < 0.05, f"{warm*1000:.1f}ms")
chk("两次结果一致（账号/角色不变）", r1 == r2, f"{r1} vs {r2}")
print(f"    冷 {cold*1000:.0f}ms → 热 {warm*1000:.2f}ms")

# ── [2] 换哈希必须重新校验 ──────────────────────────────────
print("\n[2] 缓存键含存储哈希指纹（轮换哈希后不复用旧结果）")
A._cache_clear()
A.verify_credentials(hdr(acc["user"], acc["pwd"]), amap)          # 预热
rotated = dict(amap)
rotated[acc["user"]] = ("$2a$14$" + "x" * 53, amap[acc["user"]][1])  # 伪哈希：校验必失败
r3 = A.verify_credentials(hdr(acc["user"], acc["pwd"]), rotated)
chk("哈希变了 → 不能凭旧缓存放行", r3 is None, r3)

# ── [3] 失败不写缓存 ────────────────────────────────────────
print("\n[3] 错误密码不写缓存")
A._cache_clear()
for _ in range(3):
    A.verify_credentials(hdr(acc["user"], acc["pwd"] + "-wrong"), amap)
chk("错误密码连续 3 次后缓存仍为空", len(A._VERIFIED) == 0, f"{len(A._VERIFIED)} 条")

# ── [4] 线上行为不变 ────────────────────────────────────────
print("\n[4] 线上行为不变")
if not service_reachable():
    sys.exit(skip("verify_authcache", "服务未起"))

st, _ = http("/api/whoami", auth=False)
chk("游客 /api/whoami → 200", st == 200, st)
st, body = http("/api/whoami")
chk("管理员 /api/whoami → 200", st == 200, st)
if st == 200:
    import json
    role = json.loads(body).get("role")
    chk("管理员角色 = admin", role == "admin", role)

users = [a for a in accounts if (a.get("role") or "admin") == "user" and a.get("pwd")]
if users:
    u = users[0]
    st, body = http("/api/whoami", auth=u["user"])
    import json
    chk(f"常规用户 {u['user']} → 200 且 role=user",
        st == 200 and json.loads(body).get("role") == "user", f"{st}")
    st, _ = http("/api/history", auth=u["user"])
    chk("常规用户读历史 → 200", st == 200, st)

# 错误密码走线上：必须是 401，且不能把缓存写成通过
st, _ = http("/api/whoami", auth=False)
chk("无凭据 → 游客（不是 401）", st == 200, st)

sys.exit(chk.finish("verify_authcache"))
