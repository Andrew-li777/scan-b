#!/usr/bin/env python3
"""Download offline models via China-direct sources (ModelScope → hf-mirror).

用法:
    python scripts/download_models.py --all          # 下载全部模型
    python scripts/download_models.py --whisper      # 仅 Whisper（B站无字幕兜底转录）
    python scripts/download_models.py --embedding    # 仅 bge-m3 向量嵌入（RAG）
    python scripts/download_models.py --reranker     # 仅 bge-reranker-v2-m3（RAG 精排）

模型落地目录: models/{whisper-small, bge-m3, bge-reranker-v2-m3}
下载完成后写入 models/.manifest.json；脚本幂等，已完整的模型自动跳过。
全程只访问国内直连源（ModelScope 魔搭 / hf-mirror.com），无需代理。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"
MANIFEST_PATH = MODELS_DIR / ".manifest.json"

# name -> 目标子目录 / 国内源（modelscope 优先，hf_mirror 兜底）/ 完整性关键文件
MODELS = {
    "whisper-small": {
        "dir": "whisper-small",
        "modelscope": "pengzhendong/faster-whisper-small",
        "hf_mirror": "Systran/faster-whisper-small",
        "key_files": ["model.bin", "config.json"],
    },
    "whisper-medium": {
        "dir": "whisper-medium",
        "modelscope": "pengzhendong/faster-whisper-medium",
        "hf_mirror": "Systran/faster-whisper-medium",
        "key_files": ["model.bin", "config.json"],
    },
    "bge-m3": {
        "dir": "bge-m3",
        "modelscope": "BAAI/bge-m3",
        "hf_mirror": "BAAI/bge-m3",
        "key_files": ["config.json"],
    },
    "bge-reranker-v2-m3": {
        "dir": "bge-reranker-v2-m3",
        "modelscope": "BAAI/bge-reranker-v2-m3",
        "hf_mirror": "BAAI/bge-reranker-v2-m3",
        "key_files": ["config.json"],
    },
}

GROUPS = {
    "whisper": ["whisper-small"],
    "embedding": ["bge-m3"],
    "reranker": ["bge-reranker-v2-m3"],
}


def _target_dir(name: str) -> Path:
    return MODELS_DIR / MODELS[name]["dir"]


def _complete(name: str) -> bool:
    d = _target_dir(name)
    if not d.is_dir():
        return False
    return all((d / f).exists() for f in MODELS[name]["key_files"])


def _write_manifest(name: str, source: str) -> None:
    data = {}
    if MANIFEST_PATH.exists():
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    d = _target_dir(name)
    sizes = {f.name: f.stat().st_size for f in d.iterdir() if f.is_file()}
    data[name] = {
        "source": source,
        "files": sizes,
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _download_modelscope(name: str) -> bool:
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("  [modelscope] 未安装（pip install modelscope），跳过该源")
        return False
    repo = MODELS[name]["modelscope"]
    print(f"  [modelscope] {repo} → {_target_dir(name)}")
    try:
        snapshot_download(repo, local_dir=str(_target_dir(name)))
    except Exception as e:
        print(f"  [modelscope] 失败: {e}")
        return False
    return _complete(name)


def _download_hf_mirror(name: str) -> bool:
    try:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  [hf-mirror] huggingface_hub 不可用，跳过")
        return False
    repo = MODELS[name]["hf_mirror"]
    print(f"  [hf-mirror] {repo} → {_target_dir(name)}")
    try:
        snapshot_download(repo, local_dir=str(_target_dir(name)))
    except Exception as e:
        print(f"  [hf-mirror] 失败: {e}")
        return False
    return _complete(name)


def download(name: str) -> bool:
    if _complete(name):
        print(f"[OK] {name} 已就绪，跳过")
        return True
    print(f"[..] {name} -> {_target_dir(name)}")
    source = ""
    if _download_modelscope(name):
        source = "modelscope"
    elif _download_hf_mirror(name):
        source = "hf-mirror"
    if source:
        _write_manifest(name, source)
        print(f"[OK] {name} 下载完成（来源: {source}）")
        return True
    print(f"[MISS] {name} 下载失败，请检查网络后重试")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="下载离线模型（国内直连）")
    parser.add_argument("--all", action="store_true", help="下载全部模型")
    parser.add_argument("--whisper", action="store_true", help="下载 Whisper 模型")
    parser.add_argument("--embedding", action="store_true", help="下载 bge-m3 嵌入模型")
    parser.add_argument("--reranker", action="store_true", help="下载 bge-reranker 重排模型")
    args = parser.parse_args()

    names: list[str] = []
    if args.all:
        names = list(MODELS.keys())
    else:
        for flag, group in (("whisper", "whisper"), ("embedding", "embedding"), ("reranker", "reranker")):
            if getattr(args, flag):
                names += GROUPS[group]
    if not names:
        parser.print_help()
        return 2

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    ok = all(download(n) for n in names)
    print()
    if ok:
        print("全部模型就绪 [OK] 现在可运行: python scripts/check_offline.py")
        return 0
    print("部分模型下载失败 [MISS] 请检查网络（国内直连即可）后重试")
    return 1


if __name__ == "__main__":
    sys.exit(main())
