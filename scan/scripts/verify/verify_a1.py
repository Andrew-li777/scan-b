#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #63（A1）验证：临时目录清理是否真的生效（原来是 NameError 死代码）。

覆盖：
1. `cleanup_empty_tempdirs()` 能力与返回值语义；
2. pipeline 命名空间/源码中已无未定义的 `_TEMP_VIDEO_DIR` 引用；
3. pipeline 确已导入清理函数；
4. 幂等（再调一次返回 0）。

注意：会真的清理 tempvideo/ 下的空目录与孤儿音频残留（这正是修复目标行为）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import REPO_ROOT, chk  # noqa: E402

import src.transcriber as T  # noqa: E402
from src.web import pipeline as P  # noqa: E402

TD = T._TEMP_VIDEO_DIR
print(f"tempvideo = {TD}")

before_dirs = [d for d in TD.iterdir() if d.is_dir()] if TD.exists() else []
before_empty = [d for d in before_dirs if not any(d.iterdir())]
before_files = [(d.name, f.name, f.stat().st_size)
                for d in before_dirs for f in d.iterdir() if f.is_file()]
print(f"清理前：目录 {len(before_dirs)} 个（其中空目录 {len(before_empty)}），残留文件 {len(before_files)} 个")
for n, f, s in before_files:
    print(f"   残留: {n}/{f}  {s / 1024 / 1024:.1f} MB")

removed = T.cleanup_empty_tempdirs()
after_dirs = [d for d in TD.iterdir() if d.is_dir()] if TD.exists() else []
after_empty = [d for d in after_dirs if not any(d.iterdir())]
print(f"\ncleanup_empty_tempdirs() 返回 {removed}；清理后目录 {len(after_dirs)} 个，其中空目录 {len(after_empty)}")
chk("空目录已被清空", len(after_empty) == 0, after_empty)
chk("返回值为实际清理数", removed == len(before_empty),
    f"removed={removed} before_empty={len(before_empty)}")

print("\n2) pipeline 中的未定义名引用是否已消除")
chk("pipeline 命名空间无 _TEMP_VIDEO_DIR", "_TEMP_VIDEO_DIR" not in vars(P))
src = (REPO_ROOT / "src/web/pipeline.py").read_text(encoding="utf-8")
usage = [l for l in src.splitlines()
         if "_TEMP_VIDEO_DIR" in l and not l.strip().startswith("#")]
chk("pipeline 源码中已无 _TEMP_VIDEO_DIR 的代码引用", not usage, usage)
chk("pipeline 已导入 cleanup_empty_tempdirs", hasattr(P, "cleanup_empty_tempdirs"))
chk("再次调用 = 0（幂等）", T.cleanup_empty_tempdirs() == 0)

sys.exit(chk.finish("项③ A1"))
