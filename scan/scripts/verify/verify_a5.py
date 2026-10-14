#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #66（A5）验证：cookie 文件改动后无需重启即可生效（60s 节流语义保持）。

覆盖：
1. 能读出 cookie；未改动文件时值稳定（命中缓存）；
2. 追加一条假 cookie → mtime 变化 → 必须立即重读（绕过节流时间戳）；
3. 缓存命中时极快（< 5ms，说明 60s 内不重复 stat）；
4. 还原文件后与原值一致。

**会短暂修改 cookies 文件，finally 中完整还原。**
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import chk, skip  # noqa: E402

import src.extractors.bilibili as B  # noqa: E402

path = B._cookie_file_path()
print(f"cookie 文件: {path}")
if not path or not Path(path).exists():
    sys.exit(skip("项⑥ A5", "cookie 文件不存在（未配置 bilibili cookie）"))

orig = Path(path).read_text(encoding="utf-8")
first = B.get_cookie_str()
print(f"首次读取: {len(first)} 字符")
chk("能读出 cookie", len(first) > 0, len(first))
chk("未改动时值稳定（命中缓存）", B.get_cookie_str() == first)

try:
    extra_line = "bilibili.com\tFALSE\t/\tFALSE\t0\tSELFTEST_COOKIE\t1\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(extra_line)
    B._cookie_cache["checked"] = 0.0          # 模拟已过 60s 节流
    second = B.get_cookie_str()
    print(f"改文件后: {len(second)} 字符")
    chk("文件变更后立刻重读（含新 cookie）", "SELFTEST_COOKIE=1" in second, second[-60:])
    chk("长度确实变了", len(second) > len(first), f"{len(first)} -> {len(second)}")

    t0 = time.monotonic()
    B.get_cookie_str()
    dt = time.monotonic() - t0
    chk("缓存命中时极快（<5ms）", dt < 0.005, f"{dt * 1000:.2f}ms")
finally:
    Path(path).write_text(orig, encoding="utf-8")
    B._cookie_cache.update({"key": None, "value": "", "checked": 0.0})
    restored = B.get_cookie_str()
    print(f"还原后: {len(restored)} 字符")
    chk("还原后与原始一致", restored == first, f"{len(restored)} vs {len(first)}")

sys.exit(chk.finish("项⑥ A5"))
