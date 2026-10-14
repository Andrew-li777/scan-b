from pathlib import Path

from pydantic_settings import BaseSettings


PROJECT_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    anthropic_auth_token: str = ""
    anthropic_base_url: str = "https://api.deepseek.com/anthropic"
    anthropic_model: str = "deepseek-v4-flash[1m]"
    anthropic_reasoning_model: str = "deepseek-v4-pro"

    host: str = "127.0.0.1"
    port: int = 8787

    bilibili_cookies: str = ""
    output_dir: str = ""
    obsidian_vault: str = ""
    whisper_model: str = ""
    # 长音频分段转录：时长超过该阈值(秒)时切成多段，降低峰值内存防 OOM
    whisper_chunk_seconds: int = 600
    # 相邻切片的重叠秒数，防止说话人在切片边界被截断丢字
    whisper_chunk_overlap: float = 2.0
    pdf_backend: str = "auto"  # "auto" | "weasyprint" | "chrome"

    rag_rerank: bool = True  # enable cross-encoder reranking for RAG
    rag_recall_multiplier: int = 4  # how many extra candidates to fetch for reranker pool
    # RAG 总开关：False = 跳过构建检索索引步骤，省 ~1.6GB 常驻内存（whisper+LLM 主流程不受影响）
    rag_enabled: bool = False

    # 转录并发闸：whisper 是全流程内存峰值所在（实测本机 OOM 杀过 python，RSS 3.0–3.4GB），
    # 且 RAG 常驻后余量更小。串行化"无字幕视频"的转录阶段；有字幕的视频不受限。
    max_concurrent_transcribe: int = 1

    # --- 离线化配置（P0：启动零外网） ---
    model_dir: str = "models"                      # 本地模型根目录（相对 PROJECT_ROOT 或绝对路径）
    embedding_model: str = "bge-m3"                # 向量嵌入模型子目录名
    rerank_model: str = "bge-reranker-v2-m3"       # 重排模型子目录名
    offline_models_required: bool = False          # True = 启动强校验模型，缺失直接报错退出

    @property
    def anthropic_kwargs(self) -> dict:
        return {
            "api_key": self.anthropic_auth_token,
            "base_url": self.anthropic_base_url,
        }


settings = Settings()
