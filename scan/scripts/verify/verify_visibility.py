#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行级可见性回归（游客 / 常规用户 / 管理员 × 记录归属）。

背景（2026-10-15，用户拍板）：
    - 管理员解析的记录 + 没有 owner 的存量记录 → `public`（游客可见）
    - **常规用户解析的记录 → `members`（仅登录用户 + 管理员可见，游客不可见）**
    - 记录可见性只在**三处**生效：列表过滤、详情/导图 404、下载/导出 404；
      删除另按归属：管理员全可删，常规用户只删自己建的。

测试手法：不跑真实解析（要下载+转录，分钟级），而是**临时造一条记录**：
在历史根目录下建 `_verify_vis_tmp_<随机>/`，写 meta.json（指定 owner/visibility）
与 overview.md，用完即删（finally 保证清理）。这样能真实穿过 HTTP 层。

用法：
    python3 verify_visibility.py
环境变量：SCAN_BASE_URL / SCAN_CRED_FILE（见 _common.py）
"""
from __future__ import annotations

import base64
import json
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    BASE,
    REPO_ROOT,
    chk,
    have_creds,
    load_accounts,
    load_cred,
    service_reachable,
    skip,
)

print("=== 行级可见性回归（记录归属 × 角色）===")
print(f"仓库根 : {REPO_ROOT}")
print(f"服务   : {BASE}\n")

sys.path.insert(0, str(REPO_ROOT))
try:
    from src.web.history import can_view_record  # type: ignore  # noqa: F401
    from src.web.history import scan_dirs
except Exception as e:
    print(f"  无法导入 src.web.history：{e}")
    sys.exit(1)

ROOTS = scan_dirs()
if not ROOTS:
    sys.exit(skip("verify_visibility", "找不到历史根目录"))
TMP_ROOT = Path(ROOTS[0])


# ── 造临时记录（section 1b 与 2 都要用，故前置）─────────────────
def make_record(owner: str, owner_role: str, visibility: str | None = None) -> Path:
    d = TMP_ROOT / f"_verify_vis_tmp_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=False)
    meta = {
        "title": f"可见性回归样本 {d.name}",
        "platform": "bilibili", "author": "verify", "duration": "00:10",
        "url": f"https://www.bilibili.com/video/{d.name}",
        "thumbnail": "", "concepts": [], "created_at": time.time(),
        "owner": owner, "owner_role": owner_role,
    }
    if visibility:
        meta["visibility"] = visibility
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "overview.md").write_text(f"# 可见性回归样本 {d.name} — 内容概览\n\n**来源**: bilibili | verify\n",
                                   encoding="utf-8")
    (d / "keypoints.md").write_text("| 时间 | 要点 |\n|---|---|\n| 00:01 | 样本要点 |\n", encoding="utf-8")
    return d


def rm(p: Path) -> None:
    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


# ── 1. 规则层（不依赖服务）────────────────────────────────────
print("[1] 规则层：can_view_record 直接判定")
CHECKS = [
    ("无 owner 的存量记录 → 游客可见", "guest", {}, True),
    ("无 owner 的存量记录 → 常规用户可见", "user", {}, True),
    ("管理员建的 → 游客可见", "guest", {"owner": "a", "owner_role": "admin"}, True),
    ("常规用户建的 → 游客不可见", "guest", {"owner": "u1", "owner_role": "user"}, False),
    ("常规用户建的 → 常规用户可见（用户之间互看）", "user", {"owner": "u1", "owner_role": "user"}, True),
    ("显式 members → 游客不可见", "guest", {"visibility": "members"}, False),
    ("显式 public 覆盖归属 → 游客可见", "guest", {"owner": "u1", "owner_role": "user", "visibility": "public"}, True),
]
from src.web.roles import can_view_record as cvr  # noqa: E402
for label, role, rec, want in CHECKS:
    chk(label, cvr(role, rec) == want, f"role={role} rec={rec} → {cvr(role, rec)}，期望 {want}")

# ── 1b. 单元层：meta_for_path 向上查找 + visible_for_role ──────
# download 接口拿到的是**文件路径**（记录名/overview.md），可见性记在记录目录的
# meta.json 上 → 必须能向上找到，否则下载的可见性判定会永远判成"无 meta = public"。
print("\n[1b] 单元层：meta_for_path / visible_for_role")
from src.web.history import meta_for_path, visible_for_role  # noqa: E402

_tmp = make_record("u1", "user")
try:
    _b = _tmp.name
    _md_dir = meta_for_path(_b)
    _md_file = meta_for_path(f"{_b}/overview.md")
    chk("meta_for_path(目录) 取到 owner", _md_dir.get("owner") == "u1", str(_md_dir.get("owner")))
    chk("meta_for_path(目录内文件) 向上找到同一份 meta", _md_file.get("owner") == "u1",
        str(_md_file.get("owner")))
    chk("visible_for_role：游客 False（文件路径）", visible_for_role("guest", f"{_b}/overview.md") is False)
    chk("visible_for_role：常规用户 True", visible_for_role("user", f"{_b}/overview.md") is True)
    chk("visible_for_role：管理员 True", visible_for_role("admin", f"{_b}/overview.md") is True)
    chk("meta_for_path(不存在的路径) 返回空 dict", meta_for_path("__no_such_record__/x.md") == {})
finally:
    rm(_tmp)


# ── 2. 三角色 × 归属 端到端矩阵 ───────────────────────────────
if not service_reachable():
    print("\n[SKIP] API 层：服务未起")
    sys.exit(chk.finish("verify_visibility"))

accts = load_accounts()
user_acc = next((a for a in accts if a["role"] == "user" and a["pwd"]), None)
admin_acc = next((a for a in accts if a["role"] == "admin" and a["pwd"]), None)


def call(method: str, path: str, who: str = "guest"):
    headers = {}
    if who != "guest":
        u, p = load_cred(account=who)
        if not u or not p:
            return 0, b""
        headers["Authorization"] = "Bas" + "ic " + base64.b64encode(f"{u}:{p}".encode()).decode()
    body = None
    if method == "POST":
        body = b"{}"
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}", method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return 0, b""


def list_has(base_name: str, who: str) -> bool:
    st, body = call("GET", "/api/history", who)
    if st != 200:
        return False
    try:
        return any((it.get("base_name") or "") == base_name for it in json.loads(body.decode()))
    except Exception:
        return False


def q(base: str, suffix: str = "") -> str:
    return "/api/history/" + urllib.parse.quote(base) + suffix


def run_for(owner: str, owner_role: str, label: str, expect_guest_hidden: bool) -> None:
    """造一条 owner 记录，跑三角色矩阵，然后删掉。"""
    d = make_record(owner, owner_role)
    base = d.name
    try:
        print(f"\n[2] {label}（owner={owner or '无'} / {owner_role or '-'}）")
        chk(f"{label}：游客列表{'不含' if expect_guest_hidden else '含'}该记录",
            list_has(base, "guest") is not expect_guest_hidden)
        if expect_guest_hidden:
            chk(f"{label}：游客看详情 → 404", call("GET", q(base), "guest")[0] == 404)
            chk(f"{label}：游客看导图 → 404", call("GET", "/api/mindmap/" + urllib.parse.quote(base), "guest")[0] == 404)
            # 下载对游客是**白名单整段拦截**（403），轮不到路由层的可见性 404 ——
            # 路由里的 visible_for_role 是纵深防御（白名单哪天放宽了仍然拦得住），
            # 它的真实行为在 [1b] 单元层直接验证。
            chk(f"{label}：游客下载 → 403（白名单拦截）",
                call("GET", "/api/download/" + urllib.parse.quote(f"{base}/overview.md"), "guest")[0] == 403)
        else:
            chk(f"{label}：游客看详情 → 200", call("GET", q(base), "guest")[0] == 200)

        if user_acc:
            u = user_acc["user"]
            chk(f"{label}：常规用户列表含该记录", list_has(base, u))
            chk(f"{label}：常规用户看详情 → 200", call("GET", q(base), u)[0] == 200)
            chk(f"{label}：常规用户看导图 → 200",
                call("GET", "/api/mindmap/" + urllib.parse.quote(base), u)[0] == 200)
            chk(f"{label}：常规用户下载 → 200",
                call("GET", "/api/download/" + urllib.parse.quote(f"{base}/overview.md"), u)[0] == 200)
            chk(f"{label}：常规用户导出 HTML → 200", call("GET", "/api/export/" + urllib.parse.quote(base), u)[0] == 200)
            # 删除归属：只有 owner 本人或管理员
            want_del = 200 if owner == u else 404
            st, _ = call("DELETE", q(base), u)
            chk(f"{label}：常规用户删除 → {want_del}", st == want_del, f"status={st}")
            if st == 200:
                return   # 已被本人删掉，后面不用再测
        if admin_acc:
            a = admin_acc["user"]
            chk(f"{label}：管理员列表含该记录", list_has(base, a))
            chk(f"{label}：管理员看详情 → 200", call("GET", q(base), a)[0] == 200)
            st, _ = call("DELETE", q(base), a)
            chk(f"{label}：管理员删除 → 200", st == 200, f"status={st}")
    finally:
        rm(d)


user_name = user_acc["user"] if user_acc else ""

# 常规用户建的记录：游客看不见，登录用户互相可见
run_for(user_name or "verify_user", "user", "常规用户建的记录", expect_guest_hidden=True)
# 管理员建的记录：游客可见
run_for(admin_acc["user"] if admin_acc else "verify_admin", "admin", "管理员建的记录", expect_guest_hidden=False)
# 无归属的存量记录：游客可见
run_for("", "", "无归属存量记录", expect_guest_hidden=False)

# 剩下的临时目录兜底清理（run_for 的 finally 已删，这里防漏）
for p in TMP_ROOT.glob("_verify_vis_tmp_*"):
    rm(p)

sys.exit(chk.finish("verify_visibility"))
