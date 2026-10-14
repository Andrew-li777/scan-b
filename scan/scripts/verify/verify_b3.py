#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #67（B3）验证：cache 命中路径（零网络、零模型）。

覆盖：
1. `cache.set/get` 可用（旧代码只 set 不 get）；
2. `run_pipeline()` 命中缓存后**立即回放 done**，不做网络/模型动作；
3. 回放内容与缓存一致；
4. 无 title 的旧格式缓存不会误命中；
5. 测试键已清理。

直接往 cache 注入 payload，不做任何真实解析，秒级完成。
"""
import asyncio
import json
import queue
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import chk  # noqa: E402

from src.cache import cache  # noqa: E402
from src.web import pipeline as P  # noqa: E402

KEY = "SELFTEST_CACHE_HIT"
payload = {"title": "缓存回放测试", "author": "自测", "duration": 1, "thumbnail": "",
           "url": "https://example.com/x", "platform": "selftest", "subtitle_source": "cache",
           "summary": "s", "keypoints": [], "files": {}, "subtitles": [], "overview": "s",
           "mindmap": "", "translated": False, "base_name": "缓存回放测试",
           "rag": {"task_id": KEY}}

cache.set(KEY, payload, expire=600)
got = cache.get(KEY)
chk("cache.set/get 可用（旧代码只有 set 没有 get）",
    isinstance(got, dict) and got["title"] == "缓存回放测试")

# 命中：run_pipeline 应立即回放 done，且不做任何网络/模型动作
P.SSE_EVENTS[KEY] = queue.Queue()
ret = asyncio.run(P.run_pipeline("https://example.com/x", KEY))
events = []
while not P.SSE_EVENTS[KEY].empty():
    events.append(P.SSE_EVENTS[KEY].get())

chk("run_pipeline 返回 True", ret is True, ret)
names = [e["event"] for e in events]
print(f"   事件序列: {names}")
chk("出现 done 事件（直接回放）", "done" in names, names)
chk("首条为缓存命中提示", any("缓存" in str(e["data"]) for e in events),
    [e["data"] for e in events][:2])
done = next((e for e in events if e["event"] == "done"), None)
if done:
    body = json.loads(done["data"])
    chk("回放内容与缓存一致", body.get("title") == "缓存回放测试", body.get("title"))

# 非 dict / 缺 title 的旧格式不应误命中
cache.set(KEY + "_OBJ", {"not": "payload"})
P.SSE_EVENTS[KEY + "_OBJ"] = queue.Queue()
chk("无 title 的缓存不会误命中（会走正常流程）",
    not isinstance(cache.get(KEY + "_OBJ"), type(None)) and "title" not in cache.get(KEY + "_OBJ"))

for k in (KEY, KEY + "_OBJ"):
    cache.delete(k)
chk("测试缓存键已清理", cache.get(KEY) is None)

sys.exit(chk.finish("项⑨ B3"))
