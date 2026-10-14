#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #65（B1）决定性验证：只有多轮历史才能回答的问题（暗号对照）。

设计：让模型记住一个只有对话历史里才存在的「暗号」，再追问。
  A) 无 history → 不可能知道暗号（预期答不出）
  B) 带 history → 必须复述出暗号
只有 B 命中、A 不命中，才证明 history 真的进了 LLM 的 messages
（而不是靠检索片段碰巧覆盖）。

前置：服务已启动 + 凭据就位（RAG 索引非必需，因为问的是对话内容本身）。
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import BASE, TEST_URL, basic_auth, service_reachable, skip  # noqa: E402

if not service_reachable():
    sys.exit(skip("项⑤ B1b", f"服务未监听 {BASE}"))
if not basic_auth():
    sys.exit(skip("项⑤ B1b", "凭据缺失（SCAN_CRED_FILE）"))

AUTH = basic_auth()
TASK = sys.argv[1] if len(sys.argv) > 1 else TEST_URL.rstrip("/").split("/")[-1]
SECRET = "紫色犀牛"


def ask(qq, history):
    body = json.dumps({"task_id": TASK, "q": qq, "history": history}).encode()
    req = urllib.request.Request(f"{BASE}/api/chat", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["answer"]


q = "我们刚才约定的暗号是什么？只回暗号本身。"
hist = [{"role": "user", "content": f"请记住这次对话的暗号：{SECRET}"},
        {"role": "assistant", "content": "好的，我记住了。"},
        {"role": "user", "content": q}]

a_wo = ask(q, [])
a_w = ask(q, hist)
print(f"无历史: {a_wo[:120]}")
print(f"有历史: {a_w[:120]}")

hit_wo = SECRET in a_wo
hit_w = SECRET in a_w
print(f"\n  无历史命中暗号: {hit_wo}   有历史命中暗号: {hit_w}")

rc = 0
if hit_w and not hit_wo:
    print("[OK] 决定性验证通过：history 确实进了 messages")
elif hit_w:
    print("[OK] 带历史可复述暗号（对照组无历史也命中的话，说明检索片段本身覆盖了，区分度不足）")
else:
    print(f"[FAIL] 带历史仍未复述暗号 —— history 可能没进 messages（{hit_w=} / {hit_wo=}）")
    rc = 1
sys.exit(rc)
