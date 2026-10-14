#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #65（B1）验证之一：多轮 history 是否真的拼进了 messages。

前置：服务已启动 + 凭据就位 + 目标视频已建好 RAG 索引（用 SCAN_TEST_URL 或 argv[1]）。

覆盖：
1. 单元：`_build_messages()` 的去重 / 角色白名单 / 截断 / 条数上限；
2. 真实两轮问答：带历史时能否完成「指代」型追问（对照组：不带历史）。

本脚本是「宽口径」验证（关键词命中）；决定性验证见 verify_b1b.py（暗号对照）。
"""
import base64
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import BASE, TEST_URL, basic_auth, chk, service_reachable, skip  # noqa: E402

if not service_reachable():
    sys.exit(skip("项⑤ B1", f"服务未监听 {BASE}"))
if not basic_auth():
    sys.exit(skip("项⑤ B1", "凭据缺失（SCAN_CRED_FILE）"))

from src.rag.chat import _build_messages  # noqa: E402

AUTH = basic_auth()
TASK = sys.argv[1] if len(sys.argv) > 1 else TEST_URL.rstrip("/").split("/")[-1]

print("== 1) _build_messages 单元行为 ==")
q = "那第四种呢"
hist = [{"role": "user", "content": "四种特质是什么"},
        {"role": "assistant", "content": "1...2...3...4..."},
        {"role": "user", "content": q}]          # 前端会把当前问题也塞进来
msgs = _build_messages(hist, q, "PROMPT")
chk("末尾重复的当前问题被去重", sum(1 for m in msgs if m["content"] == q) == 0, msgs)
chk("历史被保留（user/assistant 交替）",
    [m["role"] for m in msgs] == ["user", "assistant", "user"], [m["role"] for m in msgs])
chk("最后一条是带检索片段的 prompt", msgs[-1]["content"] == "PROMPT")
bad = _build_messages([{"role": "system", "content": "x"},
                       {"role": "assistant", "content": "y"}], q, "P")
chk("非法角色被丢弃 + 不以 assistant 开头", bad == [{"role": "user", "content": "P"}], bad)
long_hist = [{"role": "user", "content": "a" * 5000}]
chk("单条超长被截断", len(_build_messages(long_hist, q, "P")[0]["content"]) == 2000)
many = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(20)]
chk("条数上限 6", len(_build_messages(many, q, "P")) <= 7)


def ask(qq, history):
    body = json.dumps({"task_id": TASK, "q": qq, "history": history}).encode()
    req = urllib.request.Request(f"{BASE}/api/chat", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["answer"]


print(f"\n== 2) 真实两轮问答（task_id={TASK}）==")
q1 = "视频里说的四种特质分别是什么？"
a1 = ask(q1, [])
print(f"  Q1: {q1}\n  A1: {a1[:150]}...")

q2 = "那第四种的具体表现是什么？"
a_wo = ask(q2, [])                      # 对照：无历史
a_w = ask(q2, [{"role": "user", "content": q1},
               {"role": "assistant", "content": a1},
               {"role": "user", "content": q2}])   # 带历史
print(f"\n  Q2(无历史): {q2}\n  A: {a_wo[:220]}...")
print(f"\n  Q2(有历史): {q2}\n  A: {a_w[:220]}...")

kw4 = ["颜值", "原生家庭", "打压", "忽视"]
hit_w = any(k in a_w for k in kw4)
hit_wo = any(k in a_wo for k in kw4)
chk("带历史 → 能指代到第 4 种特质", hit_w, a_w[:80])
print(f"  （对照：无历史命中第 4 种关键词 = {hit_wo}）")

sys.exit(chk.finish("项⑤ B1"))
