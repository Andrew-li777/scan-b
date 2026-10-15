#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色与权限回归用例（游客 / 常规用户 / 管理员）。

背景（2026-10-15）：
    原来只有两态 ——「无凭据=游客」「有凭据=管理员」。现引入第三种角色（常规用户），
    并把记录可见性做成行级（常规用户建的记录仅登录用户+管理员可见）。

本用例四层：
    1. 单元层：凭据文件解析（多账号 / 旧单账号兼容 / 怪角色降级）
    2. 规则层：member_allowed 白名单矩阵、can_view_record 可见性矩阵
    3. 静态层：前端以 /api/whoami 角色驱动 UI（替代「有凭据即管理员」）
    4. API 层：三角色对 21 条路由的放行/拦截（服务可达 + 常规用户账号存在时执行）

用法：
    python3 verify_roles.py
环境变量：SCAN_BASE_URL / SCAN_CRED_FILE（见 _common.py）；
         `SCAN_CRED_ACCOUNT` 指定常规用户账号名（默认自动找第一个 role=user 的账号）。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    BASE,
    REPO_ROOT,
    chk,
    have_creds,
    http,          # noqa: F401  （供其它用例同源引用，保持风格一致）
    load_accounts,
    load_cred,
    service_reachable,
)

print("=== 角色与权限回归（3 角色 × 可见性 × 路由矩阵）===")
print(f"仓库根 : {REPO_ROOT}")
print(f"服务   : {BASE}\n")

# ── 1. 单元层：凭据文件解析 ────────────────────────────────────
print("[1] 单元层：凭据文件解析（多账号 + 旧格式兼容）")
try:
    from src.web.auth import load_accounts as parse_accounts
    from src.web.roles import ADMIN, GUEST, USER, normalize_role
except Exception as e:
    print(f"  无法导入 src.web.auth / roles：{e}")
    sys.exit(1)

CASES = [
    ("旧单账号（无角色行 → admin）",
     "账号: admin1\n密码: x\nbcrypt: $2a$14$aaa\n", {"admin1": "admin"}),
    ("多账号（角色行生效）",
     "账号: admin1\nbcrypt: $2a$14$aaa\n角色: admin\n\n账号: u1\nbcrypt: $2a$14$bbb\n角色: user\n",
     {"admin1": "admin", "u1": "user"}),
    ("无法识别的角色 → 降级 user（fail closed）",
     "账号: weird\nbcrypt: $2a$14$ccc\n角色: 超级管理员\n", {"weird": "user"}),
    ("缺 bcrypt → 账号不生效（fail closed）",
     "账号: ghost\n密码: x\n", {}),
    ("中英键名 + 等号分隔",
     "user: u2\nhash=$2a$14$ddd\nrole=user\n", {"u2": "user"}),
]
for name, text, want in CASES:
    f = tempfile.NamedTemporaryFile("w", suffix=".cred", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    try:
        got = {k: v[1] for k, v in parse_accounts(f.name).items()}
    finally:
        os.unlink(f.name)
    chk(f"解析：{name}", got == want, f"得到 {got}，期望 {want}")

chk("normalize_role：空值降级 user", normalize_role("") == USER)
chk("normalize_role：admin/管理员 归一", normalize_role("管理员") == ADMIN and normalize_role("ADMIN") == ADMIN)
chk("三常量互不相同", len({GUEST, USER, ADMIN}) == 3)

# ── 2. 规则层：白名单矩阵 ──────────────────────────────────────
print("\n[2] 规则层：member_allowed / can_view_record")
from src.web.guest_rules import guest_read_allowed  # noqa: E402
from src.web.roles import can_view_record, member_allowed  # noqa: E402

ALLOW = [
    ("GET", "/api/history"), ("GET", "/api/history/BV1x"), ("GET", "/api/whoami"),
    ("GET", "/api/mindmap/BV1x"), ("GET", "/api/download/a.mp4"), ("GET", "/api/export/BV1x"),
    ("GET", "/api/chat/global"), ("GET", "/api/progress/t1"),
    ("POST", "/api/analyze"), ("POST", "/api/chat"), ("POST", "/api/cancel/t1"),
    ("POST", "/api/history/batch_delete"), ("DELETE", "/api/history/BV1x"),
]
DENY = [
    ("POST", "/api/analyze/playlist"), ("POST", "/api/export/batch"),
    ("POST", "/api/compare"), ("PUT", "/api/history/BV1x"),
    ("GET", "/api/nonexistent"),
]
bad = [f"{m} {p}" for m, p in ALLOW if not member_allowed(m, p, guest_read_allowed)]
bad += [f"{m} {p}" for m, p in DENY if member_allowed(m, p, guest_read_allowed)]
chk(f"常规用户：{len(ALLOW)} 放行 + {len(DENY)} 拦截", not bad, f"异常项 {bad}")

chk("游客仍不能下载（本层不被放宽）", not guest_read_allowed("GET", "/api/download/a.mp4"))
chk("游客可读历史", guest_read_allowed("GET", "/api/history"))

RECS = {
    "存量无 owner": ({}, True, True, True),
    "管理员建的": ({"owner": "admin1", "owner_role": "admin"}, True, True, True),
    "常规用户建的": ({"owner": "u1", "owner_role": "user"}, True, True, False),
    "常规用户建的(显式 public)": ({"owner": "u1", "owner_role": "user", "visibility": "public"}, True, True, True),
    "显式 members": ({"visibility": "members"}, True, True, False),
}
bad = []
for label, (rec, exp_admin, exp_user, exp_guest) in RECS.items():
    got = (can_view_record(ADMIN, rec), can_view_record(USER, rec), can_view_record(GUEST, rec))
    if got != (exp_admin, exp_user, exp_guest):
        bad.append(f"{label}={got}")
chk(f"可见性矩阵：{len(RECS)} 记录 × 3 角色", not bad, f"异常 {bad}")

# ── 3. 静态层：前端角色驱动 ────────────────────────────────────
print("\n[3] 静态层：前端按角色显示功能")
idx = REPO_ROOT / "src" / "web" / "static" / "index.html"
if not idx.is_file():
    chk("index.html 存在", False, str(idx))
    sys.exit(chk.finish("verify_roles"))
html = idx.read_text(encoding="utf-8", errors="ignore")
chk("isAdmin() 基于角色而非『有凭据』",
    "function isAdmin(){ return getRole() === 'admin'; }" in html,
    "仍是 !!getAuth() 的话，常规用户会被当成管理员显示全部按钮")
chk("有 isMember()（任意登录角色）", "function isMember()" in html)
chk("有 requireAdmin()（管理员专属操作）", "function requireAdmin()" in html)
chk("启动时调用 /api/whoami", "refreshMe()" in html and "'/api/whoami'" in html)
chk("常规用户徽标样式存在的 .auth-user", ".auth-user{" in html)
chk("批量解析按钮受管理员门控", "batchBtn" in html and "isAdmin() ? '' : 'none'" in html)
chk("对比/打包按钮受管理员门控", "cbx.style.display = isAdmin()" in html and "pbx.style.display = isAdmin()" in html)
chk("前端不再用『登录即可下载』提示旧文案",
    "if (!isMember()) b.title = '需登录后下载';" in html)

# ── 4. API 层：三角色路由矩阵 ─────────────────────────────────
print("\n[4] API 层：三角色路由矩阵")
if not service_reachable():
    print("[SKIP] API 层：服务未起（先起 scan.service 再跑）")
    sys.exit(chk.finish("verify_roles"))
if not have_creds():
    print("[SKIP] API 层：凭据文件缺管理员明文密码（SCAN_CRED_FILE）")
    sys.exit(chk.finish("verify_roles"))

accts = load_accounts()
user_acc = next((a for a in accts if a["role"] == "user" and a["pwd"]), None)
admin_acc = next((a for a in accts if a["role"] == "admin" and a["pwd"]), None)


def call(method: str, path: str, who: str = "guest") -> tuple[int, bytes]:
    headers = {}
    if who != "guest":
        u, p = load_cred(account=who)
        if not u or not p:
            return 0, b""
        headers["Authorization"] = "Bas" + "ic " + base64.b64encode(f"{u}:{p}".encode()).decode()
    req = urllib.request.Request(f"{BASE}{path}", method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return 0, b""


# whoami 三角色
for who, want in (("guest", "guest"), (user_acc["user"] if user_acc else None, "user"),
                  (admin_acc["user"] if admin_acc else None, "admin")):
    if who is None:
        continue
    st, body = call("GET", "/api/whoami", who)
    try:
        role = json.loads(body.decode()).get("role")
    except Exception:
        role = None
    chk(f"whoami：{who} → role={want}", st == 200 and role == want, f"status={st} role={role}")

# 游客：只读
chk("游客 GET /api/history → 200", call("GET", "/api/history")[0] == 200)
chk("游客 POST /api/analyze → 403", call("POST", "/api/analyze")[0] == 403)
chk("游客 GET /api/download/x → 403", call("GET", "/api/download/x.mp4")[0] == 403)

if user_acc:
    u = user_acc["user"]
    # 用「缺必填参数」当探针：中间件放行 → 422，被拦 → 403（不会真的起解析任务）
    chk("常规用户 GET /api/history → 200", call("GET", "/api/history", u)[0] == 200)
    chk("常规用户 POST /api/analyze（缺 url）→ 422 放行", call("POST", "/api/analyze", u)[0] == 422)
    chk("常规用户 POST /api/chat（缺参）→ 422 放行", call("POST", "/api/chat", u)[0] == 422)
    chk("常规用户 POST /api/history/batch_delete（缺参）→ 422 放行",
        call("POST", "/api/history/batch_delete", u)[0] == 422)
    chk("常规用户 POST /api/analyze/playlist → 403（合集仅管理员）",
        call("POST", "/api/analyze/playlist", u)[0] == 403)
    chk("常规用户 POST /api/export/batch → 403", call("POST", "/api/export/batch", u)[0] == 403)
    chk("常规用户 POST /api/compare → 403", call("POST", "/api/compare", u)[0] == 403)
    # 错误密码 → 401
    st, _ = call("GET", "/api/history", "guest")
    u2, _p2 = load_cred(account=u)
    bad_hdr = {"Authorization": "Bas" + "ic " + base64.b64encode(f"{u}:__wrong__".encode()).decode()}
    try:
        urllib.request.urlopen(urllib.request.Request(f"{BASE}/api/history", headers=bad_hdr), timeout=15)
        chk("错误密码 → 401", False, "竟然放行了")
    except urllib.error.HTTPError as e:
        chk("错误密码 → 401", e.code == 401, f"status={e.code}")
    except Exception as e:
        chk("错误密码 → 401", False, f"{type(e).__name__}")
else:
    print("  (跳过常规用户用例：凭据文件里没有 role=user 且带明文密码的账号)")
    print("   建一个：python3 /root/scripts/scan_accounts.py add <账号> --role user")

if admin_acc:
    a = admin_acc["user"]
    chk("管理员 GET /api/history → 200", call("GET", "/api/history", a)[0] == 200)
    chk("管理员 POST /api/analyze/playlist（缺参）→ 422 放行",
        call("POST", "/api/analyze/playlist", a)[0] == 422)
    chk("管理员 POST /api/export/batch（缺参）→ 422 放行", call("POST", "/api/export/batch", a)[0] == 422)
    chk("管理员 POST /api/compare（缺参）→ 422 放行", call("POST", "/api/compare", a)[0] == 422)

sys.exit(chk.finish("verify_roles"))
