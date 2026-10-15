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

    # 全局流水线闸：同时在跑的**完整解析流水线**上限（含下载/转录/LLM/嵌入）。
    # 本机 3.9GB 物理内存，scan 常驻 ~2.0GB，无字幕视频单条峰值再 +1.3GB ≈ 3.3/3.9GB；
    # 两条并行就是 4.6GB > 3.9GB ⇒ 必然 OOM。转录闸挡不住"有字幕视频"的并行，
    # 所以必须有这一层全局闸（用户 2026-10-15 拍板：闸 = 2）。
    max_concurrent_pipeline: int = 2

    # --- 常规用户配额（2026-10-15：D2=(b)，日配额 100 条）---
    user_daily_quota: int = 100      # 条/天，按**实际产出记录数**计；失败/取消/缓存回放不计
    user_inflight_limit: int = 2     # 每账号同时在飞的解析任务上限（并发，与日配额是两件事）
    quota_file: str = "data/quota.json"   # 账本落盘（相对 PROJECT_ROOT）

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
