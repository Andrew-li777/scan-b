"""ChromaDB vector store with lazy, fully-local models (offline-first).

模型从 models/ 本地目录加载（先运行 scripts/download_models.py 下载），
首次索引/检索时才加载（懒加载 + 进程级单例）——模块导入阶段零网络、零模型加载。
"""
from __future__ import annotations

import threading

from chromadb import PersistentClient
from chromadb.config import Settings as _ChromaSettings

from src.config import PROJECT_ROOT, settings

GLOBAL_COLLECTION = "all_videos"

_client: PersistentClient | None = None
_ef = None
_lock = threading.Lock()


def _model_dir(subdir: str):
    root = PROJECT_ROOT / (settings.model_dir or "models")
    return root / subdir


def _get_client() -> PersistentClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = PersistentClient(
                    path=str(PROJECT_ROOT / ".chromadb"),
                    settings=_ChromaSettings(anonymized_telemetry=False),
                )
    return _client


def _get_ef():
    """Lazily build the bge-m3 embedding function from the local model dir."""
    global _ef
    if _ef is None:
        with _lock:
            if _ef is None:
                d = _model_dir(settings.embedding_model or "bge-m3")
                if not d.is_dir():
                    raise RuntimeError(
                        f"向量嵌入模型未就绪：{d} 不存在。"
                        f"请先运行: python scripts/download_models.py --embedding"
                    )
                from chromadb.utils import embedding_functions

                _ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=str(d)
                )
    return _ef


def get_or_create(collection_name: str):
    return _get_client().get_or_create_collection(
        name=collection_name,
        embedding_function=_get_ef(),
    )


def index_subtitles(collection_name: str, segments: list[tuple[str, str, float]]):
    """segments: list of (id, text, timestamp)"""
    col = get_or_create(collection_name)
    existing = set(col.get()["ids"])
    new_items = [(id_, text, ts) for id_, text, ts in segments if id_ not in existing]
    if not new_items:
        return
    col.add(
        ids=[x[0] for x in new_items],
        documents=[x[1] for x in new_items],
        metadatas=[{"timestamp": x[2]} for x in new_items],
    )


def index_global(segments: list[tuple[str, str, float, str]]) -> None:
    """Index subtitle segments into the global cross-video collection.
    segments: list of (id, text, timestamp, source_video_name)
    """
    col = _get_client().get_or_create_collection(
        name=GLOBAL_COLLECTION, embedding_function=_get_ef()
    )
    existing = set(col.get()["ids"])
    new_items = [(id_, text, ts, sv) for id_, text, ts, sv in segments if id_ not in existing]
    if not new_items:
        return
    col.add(
        ids=[x[0] for x in new_items],
        documents=[x[1] for x in new_items],
        metadatas=[{"timestamp": x[2], "source": x[3]} for x in new_items],
    )


def _rerank_or_fallback(query: str, candidates: list[dict], k: int) -> list[dict]:
    """Rerank with cross-encoder; fall back to raw top-k when the model is unavailable."""
    if len(candidates) <= k:
        return candidates
    try:
        from src.rag.reranker import rerank

        return rerank(query, candidates, top_n=k)
    except Exception:
        return candidates[:k]


def search_global(query: str, k: int = 5, use_rerank: bool | None = None) -> list[dict]:
    """Search across all indexed videos with optional reranking."""
    if use_rerank is None:
        use_rerank = settings.rag_rerank
    col = _get_client().get_or_create_collection(
        name=GLOBAL_COLLECTION, embedding_function=_get_ef()
    )
    recall_k = k * settings.rag_recall_multiplier if use_rerank else k
    results = col.query(query_texts=[query], n_results=recall_k)
    candidates = []
    for i in range(len(results["ids"][0])):
        candidates.append({
            "id": results["ids"][0][i],
            "text": results["documents"][0][i],
            "timestamp": results["metadatas"][0][i].get("timestamp", 0),
            "source": results["metadatas"][0][i].get("source", ""),
        })
    if use_rerank and len(candidates) > k:
        return _rerank_or_fallback(query, candidates, k)
    return candidates[:k]


def search(collection_name: str, query: str, k: int = 5, use_rerank: bool | None = None) -> list[dict]:
    """Search a single video's subtitles with optional reranking."""
    if use_rerank is None:
        use_rerank = settings.rag_rerank
    col = get_or_create(collection_name)
    recall_k = k * settings.rag_recall_multiplier if use_rerank else k
    results = col.query(query_texts=[query], n_results=recall_k)
    candidates = []
    for i, doc_id in enumerate(results["ids"][0]):
        candidates.append({
            "id": doc_id,
            "text": results["documents"][0][i],
            "timestamp": results["metadatas"][0][i].get("timestamp", 0) if results["metadatas"][0] else 0,
        })
    if use_rerank and len(candidates) > k:
        return _rerank_or_fallback(query, candidates, k)
    return candidates[:k]
