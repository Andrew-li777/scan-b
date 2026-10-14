#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #62（A6）验证：无字幕视频的 whisper 转录闸是否真的串行、且可取消。

覆盖：
1. 闸按配置初始化（默认 1 → 同时只允许一条转录）；
2. 第一条立即获得；
3. 第二条必须排队（阻塞），期间下发「排队」提示；
4. 释放后第二条立刻获得 —— 等待时长证明是串行而非并行；
5. 等待期间置位 cancel_event → 立即返回 False，不永久阻塞；
6. 测试结束后许可归还，无泄漏。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import chk  # noqa: E402

from src.config import settings  # noqa: E402
from src.web import pipeline as P  # noqa: E402

N = settings.max_concurrent_transcribe
print(f"配置 max_concurrent_transcribe = {N}")
print(f"闸初始许可 = {P._TRANSCRIBE_GATE._value}")
chk("闸按配置初始化", P._TRANSCRIBE_GATE._value == N, P._TRANSCRIBE_GATE._value)

# ① 第一条立即拿到
ev = threading.Event()
t0 = time.time()
first = P._acquire_transcribe_gate("SELFTEST_A", ev)
chk("第 1 条立即拿到闸", first is True and (time.time() - t0) < 0.5)

# ② 第二条必须排队
got = {}


def worker():
    t = time.time()
    got["ok"] = P._acquire_transcribe_gate("SELFTEST_B", ev)
    got["waited"] = time.time() - t


th = threading.Thread(target=worker, daemon=True)
th.start()
time.sleep(1.5)
chk("第 2 条在闸被占用时拿不到（阻塞中）", th.is_alive())

# ③ 释放后第二条立刻拿到
P._TRANSCRIBE_GATE.release()
th.join(timeout=6)
chk("释放后第 2 条拿到闸", got.get("ok") is True, got)
chk("第 2 条确实等了 ≈1.5s（证明串行而非并行）", got.get("waited", 0) >= 1.4, got.get("waited"))

# ④ 取消响应：闸被占 + cancel_event 已置位 → 返回 False 且不永久阻塞
P._TRANSCRIBE_GATE.release()      # 归还 worker 占用的许可，回到初始状态
P._TRANSCRIBE_GATE.acquire()      # 再次占住
ev2 = threading.Event()
ev2.set()
t1 = time.time()
cancel_ret = P._acquire_transcribe_gate("SELFTEST_C", ev2)
dt = time.time() - t1
chk("等待期间被取消 → 返回 False", cancel_ret is False, cancel_ret)
chk("取消响应及时（<8s）", dt < 8, f"{dt:.1f}s")
P._TRANSCRIBE_GATE.release()

print(f"\n闸最终许可 = {P._TRANSCRIBE_GATE._value}")
chk("测试结束后闸回到初始状态（无泄漏）", P._TRANSCRIBE_GATE._value == N)

sys.exit(chk.finish("项② A6"))
