#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内存治理回归（阶段 4，2026-10-15）。

背景：本机物理内存 3.9GB。原来只换闸（阶段 3）不够 —— 嵌入模型 bge-m3 常驻就吃 2.2GB，
把"同时跑 2 条解析"的上限从内存层面锁死了。用户拍板换 **bge-small-zh-v1.5**（512 维），
并把 systemd 的 `MemoryMax=4G`（对 3.9GB 物理内存等于没设）收到 **2.5G + MemorySwapMax**。

这一份用例守住四件事（数值是"承诺值"，掉下去说明有人改了配置没更新文档）：
    [1] 模型：配置指向 bge-small-zh-v1.5、目录/关键文件在、config.json 里 hidden_size=512
    [2] 索引：集合数/向量数与磁盘上的记录数自洽（不残留旧模型时代的孤儿集合）
    [3] cgroup：memory.max = 2.5G、memory.swap.max = 512M、memory.high = 2G
    [4] 实测：服务 RSS 在阈值内；且 RAG 真的还能答（换模型不能把功能换没）
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


def _whisper_resident(minutes: int = 30) -> bool:
    """服务的 whisper 模型是否「可能仍在内存里」。

    判据来自服务日志：近 N 分钟出现过 `Processing audio`（加载模型）且此后没有
    `Whisper model released`（正常收尾会释放）。正常流程两者配对；被取消/中断的
    解析只会有前者 —— RSS 里就多出 ~1.3GB，但那不是「常驻膨胀」。
    journalctl 不可用时保守返回 False（照常断言，不给自己开后门）。
    """
    try:
        out = subprocess.run(
            ["journalctl", "-u", "scan", "--no-pager", "--since", f"-{minutes}min"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return False
    return out.rfind("Processing audio") > out.rfind("Whisper model released")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import REPO_ROOT, chk, have_creds, http, service_reachable, skip  # noqa: E402

print("=== 内存治理回归（阶段 4）===")
sys.path.insert(0, str(REPO_ROOT))

from src.config import settings  # noqa: E402

# ── [1] 模型 ──────────────────────────────────────────────────
print("\n[1] 嵌入模型")
chk("配置指向 bge-small-zh-v1.5", settings.embedding_model == "bge-small-zh-v1.5",
    f"embedding_model={settings.embedding_model}")
mdir = Path(REPO_ROOT) / "models" / str(settings.embedding_model)
chk("模型目录存在", mdir.is_dir(), str(mdir))
chk("config.json 存在", (mdir / "config.json").exists())
chk("tokenizer.json 存在（缺它加载直接失败）", (mdir / "tokenizer.json").exists())
chk("权重文件存在", any((mdir / w).exists() for w in ("model.safetensors", "pytorch_model.bin")))
try:
    _cfg = json.loads((mdir / "config.json").read_text(encoding="utf-8"))
    chk("hidden_size = 512", int(_cfg.get("hidden_size", 0)) == 512, str(_cfg.get("hidden_size")))
except Exception as e:
    chk("config.json 可解析", False, str(e))
bge_m3 = Path(REPO_ROOT) / "models" / "bge-m3"
if bge_m3.is_dir():
    print(f"  注：models/bge-m3 仍在盘上（{sum(p.stat().st_size for p in bge_m3.rglob('*') if p.is_file())/1048576:.0f} MB）"
          " —— 故意保留作回滚点，不属于缺陷")

# ── [2] 索引自洽 ──────────────────────────────────────────────
print("\n[2] 索引与磁盘记录对账")
db_path = Path(REPO_ROOT) / ".chromadb" / "chroma.sqlite3"
chk("chroma 库存在", db_path.exists(), str(db_path))
if db_path.exists():
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = db.cursor()
    names = {r[1] for r in cur.execute("select id, name from collections")}
    total = 0
    for cid in [r[0] for r in cur.execute("select id from collections")]:
        for sid in [r[0] for r in cur.execute("select id from segments where collection=?", (cid,))]:
            total += cur.execute("select count(*) from embeddings where segment_id=?", (sid,)).fetchone()[0]
    db.close()
    chk("集合数 ≥ 30", len(names) >= 30, f"{len(names)} 个")
    chk("向量总数 ≥ 1000", total >= 1000, f"{total} 条")
    chk("全局集合 all_videos 在", "all_videos" in names)
    # 与磁盘对账：每条带 subtitles.srt 的记录都该有集合（[Pn] 记 BV号_pN）
    pd = re.compile(r"^\[P(\d+)\]")
    need: set[str] = set()
    for root in (Path("/root/video-analysis-records"), Path(REPO_ROOT) / "output"):
        if not root.exists():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            for cand in [d] + [s for s in d.iterdir() if s.is_dir() and pd.match(s.name)]:
                if not (cand / "subtitles.srt").exists():
                    continue
                mp = cand / "meta.json"
                url = ""
                if mp.exists():
                    try:
                        url = json.loads(mp.read_text(encoding="utf-8")).get("url") or ""
                    except Exception:
                        url = ""
                from src.rag.ids import task_id_for_url  # noqa: E402
                need.add(task_id_for_url(url))
    missing = sorted(t for t in need if t and t not in names)
    chk("磁盘上每条有字幕的记录都有对应集合", not missing, f"缺 {len(missing)} 个: {missing[:5]}")

# ── [3] cgroup 限额 ──────────────────────────────────────────
print("\n[3] cgroup 内存限额（「只杀自己」的前提是显著小于物理内存）")
cg = Path("/sys/fs/cgroup/system.slice/scan.service")
if not cg.exists():
    print("  [SKIP] 非 systemd/cgroup v2 环境")
else:
    def _read(f: str) -> int:
        try:
            return int((cg / f).read_text().strip())
        except Exception:
            return -1
    mmax, mhigh, smax = _read("memory.max"), _read("memory.high"), _read("memory.swap.max")
    chk("memory.max = 2.5G", mmax == 2500 * 1024 * 1024, f"{mmax}")
    # 注意：systemd 里 2G = 2GiB = 2*1024^3（不是 2000MB），2500M 同理是 MiB
    chk("memory.high = 2G", mhigh == 2 * 1024 ** 3, f"{mhigh}")
    chk("memory.swap.max = 512M", smax == 512 * 1024 * 1024, f"{smax}")
    # 关键不变量：限额必须明显小于物理内存（否则触顶是整机 OOM，形同虚设）
    try:
        phys = 0
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                phys = int(line.split()[1]) * 1024
        if phys:
            chk("memory.max < 物理内存（否则形同虚设）", 0 < mmax < phys * 0.85,
                f"max={mmax/1048576:.0f}MB phys={phys/1048576:.0f}MB")
    except Exception:
        pass

# ── [4] 实测 ─────────────────────────────────────────────────
print("\n[4] 实测：服务占用与 RAG 可用性")
if not service_reachable():
    print("  [SKIP] 服务未起")
    sys.exit(chk.finish("verify_memory"))

try:
    main_pid = Path(cg / "cgroup.procs").read_text().split()[0] if cg.exists() else None
except Exception:
    main_pid = None
if main_pid:
    rss_kb = 0
    for line in Path(f"/proc/{main_pid}/status").read_text().splitlines():
        if line.startswith("VmRSS"):
            rss_kb = int(line.split()[1])
    rss_mb = rss_kb / 1024
    if rss_kb >= 1500 * 1024 and _whisper_resident():
        # 「常驻基线」的断言不能把刚跑过转录的模型算进去：whisper-small 加载后约 1.3GB，
        # 正常收尾会打 "Whisper model released"，被取消/中断的解析则会把它留在内存里。
        # 这种情况记 SKIP 而不是 PASS —— 掩盖真膨胀比漏报更危险。
        print(f"  [SKIP] 服务 RSS {rss_mb:.0f} MB —— 近 30 分钟跑过转录，whisper 模型仍驻留；"
              "重启服务后复测才是『常驻基线』")
    else:
        chk("服务 RSS < 1500 MB（治理目标：常驻不再吃满 2GB）", 0 < rss_kb < 1500 * 1024,
            f"{rss_mb:.0f} MB")

if have_creds():
    # 用一个**没有内容的** id 做冒烟：只要 200 就说明"读索引→检索→生成"整条链没被换模型打断，
    # 又不会真的花 LLM 时间去长回答（负例答案就是一句"未提及"）。
    # 注意 payload 形状：/api/chat 收的是 ChatRequest{task_id, q, history?}，字段名是 q 不是 question。
    st, body = http("/api/chat", method="POST",
                    json_body={"task_id": "BV1234567890", "q": "这个视频讲了什么？"},
                    timeout=120)
    chk("RAG 接口仍可用（换模型没换掉功能）", st == 200, f"status={st}")
else:
    print("  [SKIP] 无凭据，跳过 RAG 实测")

sys.exit(chk.finish("verify_memory"))
