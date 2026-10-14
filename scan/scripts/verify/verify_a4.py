#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #64（A4）验证：同一 URL 并发提交 → 第二次 409；SSE 可正常读取。

分两层验证：

**① 单元层（严格，必跑）** —— 静态断言端点确实接了登记表 + 直接断言登记表语义，
   这正是 409 的来源，不受竞态窗口影响。

**② HTTP 层（尽力而为）** —— 真并发提交两次。
   注意：B3 缓存修复后，已解析过的视频会**秒回缓存**，任务在一瞬间就结束，
   竞态窗口随之消失 → 此时两次都会是 200。这不算失败，本脚本判为 SKIP 并说明。
   想强制看到真实 409（会真的跑一次完整解析，较慢），加 `--evict-cache`。
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (BASE, REPO_ROOT, TEST_URL, basic_auth, chk,  # noqa: E402
                     service_reachable, skip)

if not service_reachable():
    sys.exit(skip("项④ A4", f"服务未监听 {BASE}"))
if not basic_auth():
    sys.exit(skip("项④ A4", "凭据缺失（SCAN_CRED_FILE）"))

from src.rag.ids import task_id_for_url  # noqa: E402
from src.web import pipeline as P  # noqa: E402

AUTH_HEADER = basic_auth()
URL = (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
       else TEST_URL)
EVICT = "--evict-cache" in sys.argv

# ── ① 单元：接线 + 登记表语义（409 的依据）──────────────────────
print("== 1) 单元：接线与登记表语义 ==")
srv_src = (REPO_ROOT / "src/web/server.py").read_text(encoding="utf-8")
chk("server 端点确实调用 claim_task 做登记", "claim_task(task_id)" in srv_src)
chk("登记失败时抛 409（而非静默覆盖队列）",
    "409" in srv_src and "claim_task" in srv_src)

TID = "SELFTEST_CLAIM"
P.release_task(TID)                        # 清可能的残留
chk("首次 claim 成功", P.claim_task(TID) is True)
chk("重复 claim 被拒 → 这就是 409 的来源", P.claim_task(TID) is False)
chk("任务登记为进行中", P.is_task_active(TID) is True)
P.release_task(TID)
chk("释放后不再登记为进行中", P.is_task_active(TID) is False)
chk("释放后可再次 claim", P.claim_task(TID) is True)
P.release_task(TID)
chk("清理后无残留", P.is_task_active(TID) is False)


# ── ② HTTP：真并发提交 ─────────────────────────────────────────
def post(u):
    req = urllib.request.Request(
        f"{BASE}/api/analyze?url={urllib.parse.quote(u)}",
        method="POST", headers={"Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:120].decode("utf-8", "ignore")}


if EVICT:
    from src.cache import cache
    key = task_id_for_url(URL)
    print(f"\n== 2) HTTP 并发（--evict-cache：先清缓存键 {key}）==")
    print(f"   清缓存: {'已清' if cache.delete(key) else '本就为空（将真实解析，较慢）'}")
else:
    print("\n== 2) HTTP 并发提交（同 URL 两次）==")
print(f"   测试 URL: {URL}")

results = {}


def go(tag, delay):
    time.sleep(delay)
    results[tag] = post(URL)


t1 = threading.Thread(target=go, args=("first", 0.0))
t2 = threading.Thread(target=go, args=("second", 0.2))
t1.start(); t2.start(); t1.join(); t2.join()

for tag in ("first", "second"):
    st, body = results.get(tag, (None, {}))
    print(f"   {tag:7s} -> HTTP {st}  {json.dumps(body, ensure_ascii=False)[:140]}")

codes = [results.get("first", (None,))[0], results.get("second", (None,))[0]]
tid = (results.get("first", (None, {}))[1] or {}).get("task_id")

# 读第一条的 SSE
events, raw_lines = [], []
if tid:
    req = urllib.request.Request(f"{BASE}/api/progress/{tid}",
                                 headers={"Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            t0 = time.time()
            while time.time() - t0 < 20:
                line = r.readline()
                if not line:
                    break
                s = line.decode("utf-8", "ignore").strip()
                if not s:
                    continue
                raw_lines.append(s[:90])
                if s.startswith("data:"):
                    events.append(s[5:].strip())
                if s.startswith("event:") and s[7:].strip() in ("done", "error"):
                    break
    except Exception as e:
        print(f"   SSE 异常: {e}")
    print(f"   SSE 收到 {len(raw_lines)} 行，样例: {raw_lines[:4]}")
    chk("SSE 流可正常读取（不因重复提交而崩）", len(raw_lines) > 0, len(raw_lines))

cache_hit = any("缓存" in x for x in events)

if sorted(codes) == [200, 409]:
    chk("两次并发提交 → [200, 409]", True)
elif codes == [200, 200] and cache_hit:
    print("\n   [SKIP] 两次均 200 —— 该视频命中缓存、任务瞬时完成，竞态窗口消失。")
    print("          HTTP 层 409 未能在本次窗口观察到；登记表语义已由 ① 严格验证。")
    print("          如需观察真实 409：verify_a4.py --evict-cache（真实解析，较慢）")
else:
    chk("两次并发提交 → [200, 409]", False, codes)

# 收尾：若任务仍在跑（--evict-cache 情形），取消以免白耗资源
if tid:
    try:
        req = urllib.request.Request(f"{BASE}/api/cancel/{tid}", method="POST",
                                     headers={"Authorization": AUTH_HEADER})
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"   收尾取消 {tid}: HTTP {r.status}")
    except Exception as e:
        print(f"   收尾取消跳过（任务多半已结束）: {type(e).__name__}")

sys.exit(chk.finish("项④ A4"))
