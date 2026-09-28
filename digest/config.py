import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# 免费档白名单：任何不在白名单内、或带 Pro/ 前缀的模型一律拒绝
FREE_MODELS = [
    "THUDM/GLM-4-9B-0414",
    "Qwen/Qwen3-8B",
    "Qwen/Qwen2.5-7B-Instruct",
]


@dataclass
class Config:
    feed_url: str = field(default_factory=lambda: _env("FEED_URL"))
    transcript_base: str = field(default_factory=lambda: _env("TRANSCRIPT_BASE"))
    target_folder_id: str = field(default_factory=lambda: _env("TARGET_FOLDER_ID"))
    siliconflow_key: str = field(default_factory=lambda: _env("SILICONFLOW_API_KEY"))
    tdocs_token: str = field(default_factory=lambda: _env("TENCENT_DOCS_TOKEN"))
    wecom_webhook: str = field(default_factory=lambda: _env("WECOM_WEBHOOK"))
    siliconflow_base: str = field(default_factory=lambda: _env("SILICONFLOW_BASE", "https://api.siliconflow.cn/v1"))
    tdocs_mcp_url: str = field(default_factory=lambda: _env("TDOCS_MCP_URL", "https://docs.qq.com/openapi/mcp"))
    models: list[str] = field(default_factory=lambda: [m for m in (_env("TRANSLATE_MODELS").split(",") if _env("TRANSLATE_MODELS") else FREE_MODELS) if m])
    chunk_words: int = field(default_factory=lambda: _env_int("CHUNK_WORDS", 1100))
    max_chunk_retries: int = field(default_factory=lambda: _env_int("MAX_CHUNK_RETRIES", 3))
    max_item_minutes: float = field(default_factory=lambda: _env_float("MAX_ITEM_MINUTES", 25.0))
    daily_cutoff_beijing: str = field(default_factory=lambda: _env("DAILY_CUTOFF_BEIJING", "22:30"))
    daily_start_beijing: str = field(default_factory=lambda: _env("DAILY_START_BEIJING", "12:00"))
    max_job_minutes: float = field(default_factory=lambda: _env_float("MAX_JOB_MINUTES", 330.0))
    request_timeout: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT", 180))
    concurrency: int = field(default_factory=lambda: _env_int("TRANSLATE_CONCURRENCY", 3))

    def validate(self, need_upload: bool = True) -> list[str]:
        problems = []
        if not self.feed_url:
            problems.append("FEED_URL missing")
        if not self.transcript_base:
            problems.append("TRANSCRIPT_BASE missing")
        if not self.siliconflow_key:
            problems.append("SILICONFLOW_API_KEY missing")
        if need_upload:
            if not self.tdocs_token:
                problems.append("TENCENT_DOCS_TOKEN missing")
            if not self.target_folder_id:
                problems.append("TARGET_FOLDER_ID missing")
        bad = [m for m in self.models if m not in FREE_MODELS or m.startswith("Pro/")]
        if bad:
            problems.append(f"model not in free whitelist: {bad}")
        return problems
