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
    pdf_backend: str = "auto"  # "auto" | "weasyprint" | "chrome"

    rag_rerank: bool = True  # enable cross-encoder reranking for RAG
    rag_recall_multiplier: int = 4  # how many extra candidates to fetch for reranker pool

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
