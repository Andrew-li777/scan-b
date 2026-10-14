#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #67（A3 + A7）验证：卫生项清理后的功能等价性。

覆盖：
① A3 —— transcriber 导入后 PATH 原样（不新增 `;`、不混入 nvidia/bin），
   非 Windows 下 `_inject_cuda_path()` 直接短路；
② A7 —— `generate_all()` 仍正常产出各份 md，`_gen_subtitles` 签名已精简
   （不再收 base_name），死代码 `_safe_name` 已删除；
③ global_ask 不再把 system 指令重复注入 prompt，但 system 参数照常传入。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import chk  # noqa: E402

# ① PATH
before = os.environ.get("PATH", "")
import src.transcriber as T  # noqa: E402

after = os.environ.get("PATH", "")
print("① transcriber 导入后的 PATH")
chk("PATH 未被修改", before == after)
chk("PATH 中不含分号", ";" not in after)
chk("PATH 中未混入 nvidia/bin", "nvidia" not in after.lower())
chk("非 Windows 下 _inject_cuda_path 直接短路", T.os.name != "nt", T.os.name)

# ② generate_all 功能等价
print("\n② generate_all（签名精简后）")
import src.output.markdown as MD  # noqa: E402
from src.models import AnalysisResult, KeyPoint, SubtitleEntry, VideoMeta  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="scan_gen_selftest_"))
old_dir = MD._OUTPUT_DIR
MD._OUTPUT_DIR = tmp
try:
    meta = VideoMeta(platform="bilibili", video_id="BV1TEST0001", title="自测标题",
                     url="https://www.bilibili.com/video/BV1TEST0001", duration=125,
                     author="自测作者",
                     subtitles=[SubtitleEntry(start=0.0, end=3.5, text="第一句话"),
                                SubtitleEntry(start=4.0, end=8.0, text="第二句话")],
                     subtitle_source="platform")
    result = AnalysisResult(meta=meta,
                            summary="# 概览\n\n这是摘要。\n\n## 章节一\n内容",
                            chapters=[],
                            keypoints=[KeyPoint(timestamp=1.0, content="要点", importance=5)])
    outs = MD.generate_all(result, "自测标题", concepts=["概念A"])
    print(f"   产出: {sorted(outs.keys())}")
    for k, rel in outs.items():
        fp = tmp / rel
        chk(f"{k} 文件已生成且非空", fp.exists() and fp.stat().st_size > 0, rel)
    chk("callers 不再传 base_name（签名已精简）",
        "base_name" not in MD._gen_subtitles.__code__.co_varnames,
        MD._gen_subtitles.__code__.co_varnames)
    chk("死代码 _safe_name 已删除", not hasattr(MD, "_safe_name"))
finally:
    MD._OUTPUT_DIR = old_dir
    shutil.rmtree(tmp, ignore_errors=True)

# ③ global_ask 不重复注入 system
print("\n③ global_ask 的 prompt 组装")
import src.rag.chat as C  # noqa: E402

captured = {}


def fake_chat(system_prompt, user_message="", **kw):
    captured["system"] = system_prompt
    captured["prompt"] = user_message
    return "ok"


fake_hits = [{"id": "x", "text": "片段内容", "timestamp": 65.0, "source": "某视频"}]
old_chat, old_search = C.chat, C.search_global
C.chat = fake_chat
C.search_global = lambda q, k=5: fake_hits
try:
    C.global_ask("测试问题")
finally:
    C.chat, C.search_global = old_chat, old_search

p = captured.get("prompt", "")
chk("prompt 里已无 system 指令重复注入", "基于以下所有视频的字幕片段" not in p, p[:80])
chk("prompt 仍含检索片段与问题", "片段内容" in p and "测试问题" in p)
chk("system 参数照常传入", "基于以下所有视频" in captured.get("system", ""))

sys.exit(chk.finish("项⑦⑧ A3+A7"))
