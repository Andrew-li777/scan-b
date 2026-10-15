#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封面缩略图回归用例（CSP / scheme 归一）。

背景（2026-10 线上真机）：
    B 站 `pic` 字段返回 **http://iX.hdslb.com/...**，而线上 Caddy 给 scan
    下发的 CSP 是 `img-src 'self' data: https://*.hdslb.com https://*.ytimg.com`
    —— scheme-source 严格匹配，http 不匹配 https → 封面被浏览器拒载。
    修法：入库/出库两端统一 `https_url()` 归一 + 前端兜底。

本用例的三层断言（修复前第 3 层必 FAIL，可作负例验证）：
    1. 单元层：`src.urlutil.https_url` 的边界行为
    2. 静态层：前端 setThumb 有 http→https 提升 + img 带 no-referrer
    3. API 层：详情接口下发的 thumbnail 一律 https（或为空）

用法：
    python3 verify_thumb.py
环境变量：SCAN_BASE_URL / SCAN_CRED_FILE（见 _common.py）
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    BASE,
    REPO_ROOT,
    chk,
    http,
    have_creds,
    service_reachable,
    skip,
)

print("=== 封面缩略图回归（scheme 归一 + CSP 兼容）===")
print(f"仓库根 : {REPO_ROOT}")
print(f"服务   : {BASE}\n")

# ── 1. 单元层 ─────────────────────────────────────────────────
print("[1] 单元层：https_url() 边界行为")
try:
    from src.urlutil import https_url
except Exception as e:  # pragma: no cover
    print(f"  无法导入 src.urlutil：{e}")
    sys.exit(skip("verify_thumb", "src.urlutil 不存在（尚未合入修复）"))

UNIT = [
    ("http 提升为 https", "http://i2.hdslb.com/a.jpg", "https://i2.hdslb.com/a.jpg"),
    ("已是 https", "https://i0.hdslb.com/a.jpg", "https://i0.hdslb.com/a.jpg"),
    ("协议相对", "//i1.hdslb.com/a.jpg", "https://i1.hdslb.com/a.jpg"),
    ("大写 scheme", "HTTP://a.b/c.jpg", "https://a.b/c.jpg"),
    ("两侧空白", "  http://a.b/c.jpg  ", "https://a.b/c.jpg"),
    ("空串", "", ""),
    ("None", None, ""),
    ("data URI 不动", "data:image/png;base64,AAA", "data:image/png;base64,AAA"),
    ("本地相对路径不动", "/static/a.jpg", "/static/a.jpg"),
]
for name, src, want in UNIT:
    chk(f"https_url：{name}", https_url(src) == want,
        f"{src!r} -> {https_url(src)!r}，期望 {want!r}")

# ── 2. 静态层：前端兜底 ────────────────────────────────────────
print("\n[2] 静态层：前端 setThumb / img 属性")
idx = REPO_ROOT / "src" / "web" / "static" / "index.html"
if not idx.is_file():
    chk("index.html 存在", False, str(idx))
    sys.exit(chk.finish("verify_thumb"))

html = idx.read_text(encoding="utf-8", errors="ignore")

# 抽出 setThumb 函数体做局部断言（避免全文误命中）
m = re.search(r"function setThumb\(u\)\s*\{(.*?)\n\}", html, re.S)
body = m.group(1) if m else ""
chk("index.html 有 setThumb()", bool(body))
chk("setThumb 内做 http→https 提升",
    bool(re.search(r"http:\\/\\/|'http://'|startsWith\('http:'\)", body)),
    "未找到 scheme 归一逻辑")
chk("setThumb 最终用归一后的变量赋 src", "el.src = src" in body)
chk("img#thumb 带 referrerpolicy=no-referrer",
    bool(re.search(r'id="thumb"[^>]*referrerpolicy="no-referrer"', html)),
    "B 站图床有 Referer 防盗链，缺此属性会 403")

# ── 3. CSP 与页面 img-src 白名单是否匹配 https ────────────────
print("\n[3] CSP：scan 段 img-src 是否覆盖 https 图床域")
caddy = Path("/etc/caddy/Caddyfile")
if not caddy.is_file():
    print("  (跳过：本机无 /etc/caddy/Caddyfile)")
else:
    text = caddy.read_text(encoding="utf-8", errors="ignore")
    csp_lines = [l for l in text.splitlines() if "img-src" in l and "hdslb" in l]
    chk("Caddyfile 有 scan 段 img-src（含 hdslb）", bool(csp_lines))
    joined = " ".join(csp_lines)
    chk("白名单含 https://*.hdslb.com", "https://*.hdslb.com" in joined)
    chk("白名单未放行 http://*.hdslb.com（应保持 https-only）",
        "http://*.hdslb.com" not in joined)

# ── 4. API 层：详情接口下发值 ─────────────────────────────────
print("\n[4] API 层：GET /api/history/{base_name} 的 thumbnail")
if not service_reachable():
    print("[SKIP] verify_thumb：服务未起")
    sys.exit(chk.finish("verify_thumb"))
if not have_creds():
    print("[SKIP] 详情接口需凭据（SCAN_CRED_FILE 缺失）")
    sys.exit(chk.finish("verify_thumb"))

st, body_bytes = http("/api/history")
ok_list = st == 200
lst = []
if ok_list:
    try:
        lst = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        ok_list = False
chk("GET /api/history 返回 200 JSON 列表", ok_list and isinstance(lst, list),
    f"status={st}")

ALLOWED_HOSTS = (".hdslb.com", ".ytimg.com")
checked = http_n = empty_n = 0
bad_samples: list[str] = []
for item in (lst or [])[:5]:
    bn = item.get("base_name") if isinstance(item, dict) else None
    if not bn:
        continue
    st2, b2 = http("/api/history/" + urllib.parse.quote(bn))
    if st2 != 200:
        chk(f"详情 {bn[:18]} 返回 200", False, f"status={st2}")
        continue
    try:
        det = json.loads(b2.decode("utf-8"))
    except Exception:
        chk(f"详情 {bn[:18]} 可解析 JSON", False)
        continue
    t = (det.get("thumbnail") or "").strip()
    checked += 1
    if not t:
        empty_n += 1
        continue
    if t.startswith("http://"):
        http_n += 1
        bad_samples.append(t[:60])
    if not t.startswith(("https://", "data:", "/")):
        chk(f"详情 {bn[:18]} thumbnail 为 https/相对", False, t[:60])
    host = re.sub(r"^https://", "", t).split("/")[0]
    if host.endswith(ALLOWED_HOSTS):
        pass  # 命中 CSP 白名单域

chk("抽样详情 non-empty 的记录数 > 0", (checked - empty_n) > 0,
    f"checked={checked} empty={empty_n}")
chk("抽样详情的 thumbnail 无一条是 http://（修复前必 FAIL）", http_n == 0,
    f"仍有 {http_n} 条：{bad_samples}")

sys.exit(chk.finish("verify_thumb"))
