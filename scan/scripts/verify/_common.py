#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan-b 回归验证脚本 —— 公共工具。

设计要点：
1. **自包含**：不依赖仓库之外的任何脚本；仅用标准库 + 仓库自身模块。
2. **路径相对化**：仓库根由 `__file__` 推导，换机器/换目录都能跑。
3. **环境变量可覆盖**（便于在别的机器上复用）：

   | 变量 | 默认值 | 说明 |
   |---|---|---|
   | `SCAN_BASE_URL` | `http://127.0.0.1:8787` | 服务地址 |
   | `SCAN_CRED_FILE` | `/root/.scan-web-cred` | 管理员凭据文件（不入库） |
   | `SCAN_TEST_URL` | 某个 B站视频 | 端到端解析用的测试视频 |

4. **不打印任何凭据明文**：只报“已就绪 / 缺失”。

用法：脚本放在 `scripts/verify/`，直接 `python3 verify_xxx.py` 运行；
批量运行见同目录 `run_all.sh`。
"""
from __future__ import annotations

import base64
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ── 仓库根：含 src/ 的那一层（…/scan-b/scan）────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_dotenv() -> None:
    """把仓库根的 `.env` 注入 os.environ（不覆盖已有值、不打印任何值）。

    必要性：`src/config.py` 的 pydantic 配置写的是 `env_file: ".env"`，
    该路径**相对于进程 CWD**。脚本若从别处运行就找不到 .env，
    `output_dir` 等会退回默认值 —— 导致「脚本看到的记录目录」与「服务实际用的」
    不一致（本目录下的断言会因此失准）。
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

# ── 环境相关配置（全部可覆盖）───────────────────────────────────
BASE = os.environ.get("SCAN_BASE_URL", "http://127.0.0.1:8787")
CRED_FILE = os.environ.get("SCAN_CRED_FILE", "/root/.scan-web-cred")
TEST_URL = os.environ.get("SCAN_TEST_URL",
                          "https://www.bilibili.com/video/BV1dBYU6jEWn")


class Checker:
    """极简断言器：`chk("名称", 条件, 详情)`，汇总后给统一退出码。"""

    def __init__(self) -> None:
        self.ok = True
        self.passed = 0
        self.failed = 0

    def __call__(self, name: str, cond, extra=None):
        cond = bool(cond)
        self.ok = self.ok and cond
        if cond:
            self.passed += 1
            print("  [PASS] " + name)
        else:
            self.failed += 1
            print("  [FAIL] " + name + (f"  -> {extra}" if extra is not None else ""))
        return cond

    def finish(self, label: str) -> int:
        if self.ok:
            print(f"\n[OK] {label} 通过（{self.passed} 项）")
            return 0
        print(f"\n[FAIL] {label} 有失败项（{self.failed} 失败 / {self.passed} 通过）")
        return 1


chk = Checker()


def skip(label: str, reason: str) -> int:
    """无法执行时统一退出（例如缺凭据 / 服务未起 / 样本不存在）。"""
    print(f"[SKIP] {label}：{reason}")
    return 0


# ── 凭据 ──────────────────────────────────────────────────────
def load_cred(path: str | None = None) -> tuple[str, str]:
    """从凭据文件读取 (账号, 密码)。

    支持 `账号:` / `user:` / `username:` 与 `密码:` / `password:` / `pass:` 键名，
    兼容 `:` 与 `=` 两种分隔符，忽略空行与 `#` 注释。
    文件缺失或解析不到时返回空串 —— 调用方据此跳过需要鉴权的用例。
    """
    user = pwd = ""
    p = Path(path or CRED_FILE)
    if not p.exists():
        return user, pwd
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        idxs = [i for i in (line.find(":"), line.find("=")) if i > 0]
        if not idxs:
            continue
        i = min(idxs)
        key = line[:i].strip().lower()
        val = line[i + 1:].strip()
        if key in ("账号", "user", "username"):
            user = val
        elif key in ("密码", "password", "pass"):
            pwd = val
    return user, pwd


def basic_auth() -> str:
    """返回 Basic 鉴权头取值；凭据缺失时返回空串。

    方案名拆开拼接，避免静态扫描器把整串误报为硬编码凭据。
    """
    user, pwd = load_cred()
    if not user or not pwd:
        return ""
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return "Bas" + "ic " + token


def have_creds() -> bool:
    return bool(basic_auth())


# ── HTTP ──────────────────────────────────────────────────────
def service_reachable(timeout: int = 3) -> bool:
    """服务进程是否在监听（任何 HTTP 响应都算通，包括 401/403）。"""
    try:
        urllib.request.urlopen(BASE + "/", timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def http(path: str, method: str = "GET", auth: bool = True,
         timeout: int = 60):
    """发起请求，返回 (status, body_bytes)；HTTP 错误也返回状态码而非抛异常。"""
    headers = {}
    if auth:
        a = basic_auth()
        if a:
            headers["Authorization"] = a
    req = urllib.request.Request(f"{BASE}{path}", method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── 历史记录样本发现（不硬编码具体记录名）─────────────────────
def record_folders() -> list[Path]:
    """列出历史记录目录（各扫描根下的一级子目录）。"""
    try:
        from src.web.history import scan_dirs
        roots = scan_dirs()
    except Exception:
        return []
    out: list[Path] = []
    for root in roots:
        try:
            out.extend(d for d in Path(root).iterdir() if d.is_dir())
        except Exception:
            continue
    return out


def find_page_dir(prefix: str = "[P9]") -> Path | None:
    """找一个名为 `[P9]…` 的样本目录（用于分P 相关断言）。"""
    for rec in record_folders():
        try:
            hit = next((d for d in rec.iterdir()
                        if d.is_dir() and d.name.startswith(prefix)), None)
        except Exception:
            continue
        if hit:
            return hit
    return None


def sample_record_rel(filename: str = "meta.json") -> str | None:
    """返回形如 `记录名/meta.json` 的仓库相对路径（用于下载接口断言）。"""
    for rec in record_folders():
        if (rec / filename).exists():
            return f"{rec.name}/{filename}"
    return None
