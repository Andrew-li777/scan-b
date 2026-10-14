#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #67（B4）验证：统一路径校验（穿越必须挡）+ 顺带复查缓存 e2e。

前置：服务已启动 + 凭据就位。

覆盖：
1. 单元：`resolve_within` / `find_in_roots` / `_resolve_folder` 的越界拒绝与正常定位；
2. HTTP：明文与 URL 编码两种穿越请求 → 404/400；正常文件 → 200；
3. （可选，慢）同 URL 二次提交命中缓存 —— 用 `--with-e2e` 开启。

样本文件不硬编码，自动从历史记录目录中挑一个真实 `meta.json`。
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (BASE, TEST_URL, basic_auth, chk, sample_record_rel,  # noqa: E402
                     service_reachable, skip)

if not service_reachable():
    sys.exit(skip("项⑩ B4", f"服务未监听 {BASE}"))
if not basic_auth():
    sys.exit(skip("项⑩ B4", "凭据缺失（SCAN_CRED_FILE）"))

from src.web.history import (find_in_roots, resolve_within, scan_dirs,  # noqa: E402
                             _resolve_folder)

AUTH = basic_auth()
WITH_E2E = "--with-e2e" in sys.argv


def http(path, method="GET"):
    req = urllib.request.Request(f"{BASE}{path}", method=method,
                                 headers={"Authorization": AUTH})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


print("== 1) 单元：穿越/越界必须被拒 ==")
root = scan_dirs()[0]
chk("resolve_within 拒绝 ../../etc/passwd", resolve_within(root, "../../etc/passwd") is None)
chk("resolve_within 拒绝对 root 本身", resolve_within(root, ".") is None)
chk("find_in_roots 拒绝不存在的路径", find_in_roots("__no_such_thing__") is None)
chk("find_in_roots 拒绝绝对路径越界", find_in_roots("/etc/passwd") is None)

SAMPLE = sample_record_rel("meta.json")
if SAMPLE:
    real = find_in_roots(SAMPLE)
    chk("find_in_roots 能定位真实文件", real is not None and real.exists(), real)
    folder = SAMPLE.split("/")[0]
    chk("_resolve_folder 能定位真实记录目录", _resolve_folder(folder) is not None, folder)
else:
    print("  [SKIP] 未找到样本记录，跳过正向定位断言")

print("\n== 2) HTTP：穿越请求必须 404，正常文件 200 ==")
st, _ = http("/api/download/../../../etc/passwd")
chk("明文 ../../../etc/passwd → 404", st in (400, 404), st)
traversal = urllib.parse.quote("../../../../etc/passwd", safe="")
st, _ = http(f"/api/download/{traversal}")
chk("URL 编码穿越 → 404", st in (400, 404), st)
if SAMPLE:
    st, body = http("/api/download/" + urllib.parse.quote(SAMPLE, safe=""))
    chk("正常文件 → 200 且有内容", st == 200 and b"title" in body, st)
st, _ = http("/api/history/" + urllib.parse.quote("../../etc", safe=""), method="DELETE")
chk("删除接口的穿越请求 → 404", st in (400, 404), st)

if not WITH_E2E:
    print("\n（缓存 e2e 默认跳过；加 --with-e2e 开启，可能耗时数分钟）")
    sys.exit(chk.finish("项⑩ B4"))

print("\n== 3) 端到端：同 URL 二次提交应命中缓存 ==")
URL = TEST_URL


def submit():
    req = urllib.request.Request(
        f"{BASE}/api/analyze?url={urllib.parse.quote(URL)}", method="POST",
        headers={"Authorization": AUTH})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


st, body = submit()
tid = body.get("task_id")
print(f"   提交 → HTTP {st} task_id={tid}")


def run_once(limit=600):
    t0 = time.time()
    events, seen = [], []
    req = urllib.request.Request(f"{BASE}/api/progress/{tid}",
                                 headers={"Authorization": AUTH})
    try:
        with urllib.request.urlopen(req, timeout=limit + 60) as r:
            while time.time() - t0 < limit:
                line = r.readline()
                if not line:
                    break
                s = line.decode("utf-8", "ignore").strip()
                if s.startswith("event:"):
                    seen.append(s[7:].strip())
                elif s.startswith("data:"):
                    events.append(s[5:].strip()[:70])
                if seen and seen[-1] in ("done", "error"):
                    break
    except Exception as e:
        print(f"   SSE 异常: {e}")
    return time.time() - t0, events, seen


if st == 200:
    d1, e1, s1 = run_once()
    print(f"   ① 首次跑完 {d1:.1f}s  结束事件={s1[-1] if s1 else '?'}")
    chk("首次解析结束（done 或 error 均说明链路可达）",
        bool(s1) and s1[-1] in ("done", "error"), s1[-3:])
    if s1 and s1[-1] == "done":
        st2, body2 = submit()
        tid = body2.get("task_id")
        print(f"   二次提交 → HTTP {st2} task_id={tid}")
        if st2 == 200:
            d2, e2, s2 = run_once(limit=120)
            print(f"   ② 二次跑完 {d2:.1f}s  状态流: {e2[:4]}")
            chk("二次提交命中缓存", any("缓存" in x for x in e2), e2[:4])
            chk("缓存回放很快（<30s）", d2 < 30, f"{d2:.1f}s")
else:
    print("   （提交被 409：上一次任务仍在跑，稍后重试）")

sys.exit(chk.finish("项⑩ B4"))
