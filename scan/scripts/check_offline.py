#!/usr/bin/env python3
"""Offline readiness check: verify local models and env before starting the service.

用法:
    python scripts/check_offline.py          # 人类可读输出
    python scripts/check_offline.py --json   # JSON 输出（供部署脚本解析）

退出码: 0 = 必需项全部就绪；1 = 有必需项缺失（仍可降级启动，但部分功能不可用）。
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings  # noqa: E402  （加载 .env，保证与运行时环境一致）

MODELS_DIR = PROJECT_ROOT / (settings.model_dir or "models")

# name -> 关键文件（存在即视为就绪）
REQUIRED_MODELS = {
    "whisper-small": ["model.bin", "config.json"],
    "bge-m3": ["config.json"],
    "bge-reranker-v2-m3": ["config.json"],
}
OPTIONAL_MODELS = {
    "whisper-medium": ["model.bin", "config.json"],
}


def _model_ok(name: str, files: list[str]) -> tuple[bool, list[str]]:
    d = MODELS_DIR / name
    if not d.is_dir():
        return False, files
    missing = [f for f in files if not (d / f).exists()]
    return (not missing), missing


def main() -> int:
    parser = argparse.ArgumentParser(description="离线就绪自检")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    result: dict = {"models_dir": str(MODELS_DIR), "models": {}, "env": {}, "ok": True}

    for name, files in REQUIRED_MODELS.items():
        ok, missing = _model_ok(name, files)
        result["models"][name] = {"ready": ok, "missing": missing}
        if not ok:
            result["ok"] = False
    for name, files in OPTIONAL_MODELS.items():
        ok, missing = _model_ok(name, files)
        result["models"][name] = {"ready": ok, "missing": missing, "optional": True}

    # 环境检查（与 src.config 同源，仅提示，不阻塞启动）
    env = result["env"]
    env["anthropic_auth_token"] = bool(settings.anthropic_auth_token)
    env["output_dir"] = settings.output_dir or ""
    env["bilibili_cookies"] = settings.bilibili_cookies or ""

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    print(f"模型目录: {MODELS_DIR}")
    for name, files in REQUIRED_MODELS.items():
        ok, missing = _model_ok(name, files)
        mark = "[OK]  " if ok else "[MISS]"
        detail = "" if ok else f" 缺失: {', '.join(missing)}"
        print(f"  {mark} {name}{detail}")
    for name, files in OPTIONAL_MODELS.items():
        ok, missing = _model_ok(name, files)
        mark = "[OK]  " if ok else "[OPT] "
        detail = "" if ok else "（可选，未下载）"
        print(f"  {mark} {name}{detail}")
    print()
    print(f"  {'[OK]  ' if env['anthropic_auth_token'] else '[WARN]'} DeepSeek API Key 已配置（LLM 分析需要，运行时联网）")
    print(f"  {'[OK]  ' if env['output_dir'] else '[WARN]'} OUTPUT_DIR={'<未配置，使用项目 output/>' if not env['output_dir'] else env['output_dir']}")
    print(f"  {'[OK]  ' if env['bilibili_cookies'] else '[WARN]'} BILIBILI_COOKIES={'<未配置，遇 412 时启用>' if not env['bilibili_cookies'] else env['bilibili_cookies']}")
    print()
    if result["ok"]:
        print("全部必需模型就绪 [OK] 可断网启动服务")
    else:
        print("存在缺失模型 [MISS] 请先运行: python scripts/download_models.py --all")
        print("（服务仍可降级启动：缺 whisper 则无字幕视频不可用，缺嵌入/重排模型则 RAG 不可用）")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
