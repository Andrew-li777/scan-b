#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问题记录 #61（B2）验证：删除历史记录必须同步清理向量索引。

覆盖：
1. `delete_index()` 机制是否正确（造/删假集合，幂等性）；
2. 派生键 `task_id_for_url()` 是否符合 Chroma 集合名规则、且与库中集合名一致；
3. `_purge_index()` 全路径（用临时假记录，不碰真实数据）；
4. 当前库状态（只读）。

不加载嵌入模型、不删除任何真实记录。可直接对着运行中的服务跑。
"""
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import chk, find_page_dir  # noqa: E402

import src.rag.vectorstore as vs  # noqa: E402
from src.rag.ids import task_id_for_url  # noqa: E402
from src.web import history as H  # noqa: E402

FAKE = "BV1TEST0001"

# ── 1. 机制：造一个假集合 → delete_index 必须删掉 ────────────────
print("1) delete_index 机制")
vs._get_client().get_or_create_collection(FAKE)
names = [c.name for c in vs._get_client().list_collections()]
chk("假集合已创建", FAKE in names, names[-5:])

removed = vs.delete_index(FAKE)
names = [c.name for c in vs._get_client().list_collections()]
chk("delete_index 返回 True", removed is True, removed)
chk("假集合已被删除", FAKE not in names)
chk("对不存在的集合再删一次 = 幂等 False", vs.delete_index(FAKE) is False)

# ── 2. 派生键断言：base_name 与写入键对不上（旧 bug 的根源）──────
print("\n2) 派生键 vs 真实记录")
p9 = find_page_dir("[P9]")
if p9:
    meta = json.loads((p9 / "meta.json").read_text(encoding="utf-8"))
    tid = task_id_for_url(meta.get("url") or "")
    print(f"     样本目录: {p9.name[:40]}…")
    print(f"     由 URL 派生的键: {tid}")
    chk("派生键符合集合名规则（可被 Chroma 接受）",
        bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,61}[a-zA-Z0-9]$", tid)), tid)
    chk("改用派生键后与库中集合名一致",
        tid in [c.name for c in vs._get_client().list_collections()], tid)
else:
    print("     未找到 [P9] 样本记录，跳过（设 SCAN_TEST_URL 无关；此为记录依赖）")

# ── 3. _purge_index 全路径（假记录，不碰真数据）─────────────────
print("\n3) _purge_index 全路径（假记录）")
vs._get_client().get_or_create_collection(FAKE)
tmp = Path(tempfile.mkdtemp(prefix="scan_selftest_"))
try:
    (tmp / "meta.json").write_text(
        json.dumps({"title": "自测记录",
                    "url": "https://www.bilibili.com/video/BV1TEST0001"}),
        encoding="utf-8")
    info = H._purge_index(tmp, "自测记录")
    print(f"     {info}")
    chk("_purge_index 算出的 task_id 正确", info["task_id"] == FAKE, info)
    chk("per-video 集合被清掉", info["collection_removed"] is True, info)
    chk("全局库清理不报错（假 source → 0 条）", info["global_removed"] == 0, info)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── 4. 当前库状态（只读）───────────────────────────────────────
print("\n4) 当前库状态（只读）")
cols = list(vs._get_client().list_collections())
print(f"     集合数={len(cols)}")
try:
    print(f"     all_videos 向量数={vs._get_client().get_collection('all_videos').count()}")
except Exception as e:
    print(f"     all_videos 读取失败（可能尚未建库）: {e}")

sys.exit(chk.finish("项① B2"))
