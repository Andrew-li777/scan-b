"""RAG reranker (bge-reranker-v2-m3) loaded from the local models/ dir (offline-first)."""

import threading

from sentence_transformers import CrossEncoder

from src.config import PROJECT_ROOT, settings

_reranker: CrossEncoder | None = None
_lock = threading.Lock()


def _get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        with _lock:
            if _reranker is None:
                d = (
                    PROJECT_ROOT
                    / (settings.model_dir or "models")
                    / (settings.rerank_model or "bge-reranker-v2-m3")
                )
                if not d.is_dir():
                    raise RuntimeError(
                        f"重排模型未就绪：{d} 不存在。"
                        f"请先运行: python scripts/download_models.py --reranker"
                    )
                _reranker = CrossEncoder(str(d))
    return _reranker


def rerank(query: str, candidates: list[dict], top_n: int = 5) -> list[dict]:
    """Rerank candidates by cross-encoder relevance score."""
    if len(candidates) <= top_n:
        return candidates

    model = _get_reranker()
    pairs = [(query, c["text"]) for c in candidates]
    scores = model.predict(pairs)
    scored = [(c, float(s)) for c, s in zip(candidates, scores)]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in scored[:top_n]]
