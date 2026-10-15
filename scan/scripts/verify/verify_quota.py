#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析配额与资源闸回归（阶段 3）。

用户 2026-10-15 拍板：
    D2=(b) 常规用户能给解析、**日配额 100 条**；口径 **(b) 按实际产出记录数 + 失败返还**；
    全局流水线闸 = **2**（转录闸仍是 1）。

这一份用例覆盖**配额账本**（纯逻辑，不碰真解析）与**线上只读校验**：
    [1] 单元层：在飞名额 / 产出计数 / 失败返还 / 用尽文案 / 落盘持久化
    [2] 静态层：接线没被删（server.py 里 reserve→429、pipeline 里全局闸 + settle）
    [3] 线上层：/api/whoami 三角色的 quota 字段

「用尽 → HTTP 429」这种要改账本+重启服务的端到端验证放在
`/root/scripts/scan_quota_e2e.sh`（有副作用，不进仓库回归）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import BASE, REPO_ROOT, chk, have_creds, http, load_accounts, load_cred, service_reachable, skip  # noqa: E402

print("=== 配额与资源闸回归 ===")
sys.path.insert(0, str(REPO_ROOT))

from src.config import settings            # noqa: E402
from src.web import quota                   # noqa: E402


def _reload_from(path: Path) -> None:
    """把账本切到指定文件并强制重载（模拟进程重启）。"""
    settings.quota_file = str(path)
    quota._loaded = False
    quota._days.clear()
    quota._inflight.clear()


# ── [1] 单元层 ────────────────────────────────────────────────
print("\n[1] 单元层：配额账本")
tmpdir = Path(tempfile.mkdtemp(prefix="verify_quota_"))
ledger = tmpdir / "quota.json"
_reload_from(ledger)

LIM = int(settings.user_daily_quota)
FLY = int(settings.user_inflight_limit)
chk("默认日配额 = 100（用户拍板值）", LIM == 100, f"实际 {LIM}")
chk("默认在飞上限 = 2", FLY == 2, f"实际 {FLY}")

# 管理员不受限
ok_a, why_a = quota.reserve("scan_admin", "admin")
st_a = quota.status("scan_admin", "admin")
chk("管理员 reserve 永远放行", ok_a is True, why_a)
chk("管理员 status.limit = None（不限）", st_a["limit"] is None, str(st_a))
chk("管理员 status.remaining = None", st_a["remaining"] is None, str(st_a))
quota.settle("scan_admin", produced=True)
chk("管理员产出照记（供统计，但不设限）", quota.used_today("scan_admin") == 1, str(quota.used_today("scan_admin")))
chk("管理员受限字段仍为 None", quota.status("scan_admin", "admin")["remaining"] is None)

# 在飞名额：2 个可提交，第 3 个被挡
u = "alice"
ok1, _ = quota.reserve(u, "user")
ok2, _ = quota.reserve(u, "user")
ok3, why3 = quota.reserve(u, "user")
chk("第 1 条 reserve 成功", ok1 is True)
chk("第 2 条 reserve 成功（在飞上限 2）", ok2 is True)
chk("第 3 条 reserve 被挡（在飞超限）", ok3 is False, why3)
chk("在飞超限文案含「正在解析」", "正在解析" in why3, why3)
chk("status.inflight = 2", quota.status(u, "user")["inflight"] == 2)

# 失败返还：settle(produced=False) 只还名额、不计数
quota.settle(u, produced=False)
chk("失败结算后在飞 -1", quota.status(u, "user")["inflight"] == 1)
chk("失败不消耗配额（按实际产出计）", quota.used_today(u) == 0, str(quota.used_today(u)))

# 成功产出：settle(produced=True) 才 +1
quota.settle(u, produced=True)
chk("产出结算后 used = 1", quota.used_today(u) == 1, str(quota.used_today(u)))
chk("产出结算后在飞归零", quota.status(u, "user")["inflight"] == 0)

# 落盘持久化（模拟进程重启后读回）
_reload_from(ledger)
chk("账本落盘：重启后 used 仍是 1", quota.used_today(u) == 1, str(quota.used_today(u)))
chk("账本文件存在且是 JSON", ledger.exists() and isinstance(
    json.loads(ledger.read_text(encoding="utf-8")), dict))

# 用尽：把 used 拉到上限
for _ in range(LIM - 1):
    quota.settle(u, produced=True)
chk(f"used 拉到上限 {LIM}", quota.used_today(u) == LIM, str(quota.used_today(u)))
ok4, why4 = quota.reserve(u, "user")
chk("配额用尽后 reserve 被挡", ok4 is False, why4)
chk("用尽文案含「配额已用完」", "配额已用完" in why4, why4)
chk("status.remaining = 0", quota.status(u, "user")["remaining"] == 0)
ok5, why5 = quota.check(u, "user")
chk("check() 与 reserve() 结论一致", ok5 is False and "配额已用完" in why5, why5)

# reset 清零
quota.reset(u)
chk("reset 后 used 归零", quota.used_today(u) == 0)
chk("reset 后 reserve 恢复可用", quota.reserve(u, "user")[0] is True)

# 游客不能解析
ok6, why6 = quota.check("", "guest")
chk("游客 check 被拒", ok6 is False and "登录" in why6, why6)

# ── [2] 静态接线检查 ─────────────────────────────────────────
print("\n[2] 静态层：接线没被删")
server_src = (Path(REPO_ROOT) / "src/web/server.py").read_text(encoding="utf-8")
pipe_src = (Path(REPO_ROOT) / "src/web/pipeline.py").read_text(encoding="utf-8")
chk("server: /api/analyze 里调用了 quota.reserve", "quota.reserve(_user, _role)" in server_src)
chk("server: 配额不足返回 429", "429, why" in server_src)
chk("server: whoami 回包带 quota", '"quota": quota.status(user, role)' in server_src)
chk("pipeline: 有全局流水线闸 _PIPELINE_GATE", "_PIPELINE_GATE = threading.BoundedSemaphore" in pipe_src)
chk("pipeline: 闸用 to_thread 取（不卡事件循环）", "to_thread(_PIPELINE_GATE.acquire" in pipe_src)
chk("pipeline: 只在取到闸时才释放", "if got_gate:" in pipe_src)
chk("pipeline: 写出记录才置 produced=True", "produced = True" in pipe_src)
chk("pipeline: finally 里结算配额（失败返还）", "quota.settle(owner, produced)" in pipe_src)
chk("pipeline: 缓存回放不占闸（在闸之前 return）",
    pipe_src.index("命中本地缓存") < pipe_src.index("_acquire_pipeline_gate(task_id, cancel_event)"))

# ── [3] 线上层：/api/whoami 的 quota ─────────────────────────
print("\n[3] 线上层：/api/whoami quota 字段")
if not service_reachable():
    print("  [SKIP] 服务未起")
    sys.exit(chk.finish("verify_quota"))

st, body = http("/api/whoami", auth=False)
try:
    d = json.loads(body.decode())
except Exception:
    d = {}
chk("游客 whoami 可达且 role=guest", st == 200 and d.get("role") == "guest", f"{st} {d}")
chk("游客 quota 不带账号信息（used=0）", (d.get("quota") or {}).get("used") == 0, str(d.get("quota")))

accts = load_accounts()
usr = next((a["user"] for a in accts if a["role"] == "user"), None)
if usr and have_creds(usr):
    st2, b2 = http("/api/whoami", auth=usr)
    d2 = json.loads(b2.decode()) if st2 == 200 else {}
    q2 = d2.get("quota") or {}
    chk("常规用户 whoami: limit = 100", q2.get("limit") == 100, str(q2))
    chk("常规用户 whoami: remaining = limit - used",
        q2.get("remaining") == (100 - int(q2.get("used", -1))), str(q2))
    chk("常规用户 whoami: 带在飞上限", q2.get("inflight_limit") == 2, str(q2))
else:
    print("  [SKIP] 无常规用户账号/密码，跳过角色配额校验")

if have_creds():
    st3, b3 = http("/api/whoami")           # auth=True → 管理员账号
    d3 = json.loads(b3.decode()) if st3 == 200 else {}
    q3 = d3.get("quota") or {}
    chk("管理员 whoami: quota.limit = None（不限）", q3.get("limit") is None, str(q3))

sys.exit(chk.finish("verify_quota"))
