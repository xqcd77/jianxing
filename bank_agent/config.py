from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_mode: str = "demo"
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-flash"
    model_api_key: str = field(default="", repr=False)
    demo_enabled: bool = True
    demo_verification_enabled: bool = True
    model_timeout: float = 45.0
    model_thinking: bool = False
    model_reasoning_effort: str = "max"
    model_max_tokens: int = 16384
    model_context_tokens: int = 131072
    model_token_safety_margin: int = 2048
    task_token_budget: int = 1048576
    agent_runtime: str = "agents_sdk"
    memory_backend: str = "chroma"
    memory_chroma_url: str = ""
    memory_embedding: str = "lexical"
    memory_model: str = "BAAI/bge-small-zh-v1.5"
    memory_top_k: int = 6
    memory_context_chars: int = 6000
    memory_maintenance_enabled: bool = True
    memory_maintenance_interval: float = 60.0
    memory_maintenance_batch: int = 64
    memory_active_dialogues: int = 128
    max_replans: int = 3
    research_max_rounds: int = 2
    research_concurrency: int = 3
    research_queue_limit: int = 100
    research_timeout: float = 300
    memory_strategy: str = "compressed"
    scheduler_mode: str = "external"
    scheduler_interval: float = 15.0
    orchestration_mode: str = "supervisor"
    chat_concurrency: int = 3
    chat_queue_limit: int = 100
    chat_lease_seconds: float = 30
    chat_worker_mode: str = 'embedded'
    model_providers: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        from .model_registry import provider_environment
        return cls(
            data_dir=Path(os.getenv("BANK_DATA_DIR", ".data")).resolve(),
            model_mode=os.getenv("BANK_MODEL_MODE", "demo"),
            model_base_url=os.getenv("BANK_MODEL_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model_name=os.getenv("BANK_MODEL_NAME", "deepseek-flash"),
            model_api_key=os.getenv("BANK_MODEL_API_KEY", ""),
            model_providers=provider_environment(),
            demo_enabled=os.getenv("BANK_DEMO_ENABLED", "true").lower() == "true",
            demo_verification_enabled=os.getenv("BANK_DEMO_VERIFICATION_ENABLED", "true").lower() == "true",
            model_timeout=float(os.getenv("BANK_MODEL_TIMEOUT", "120")),
            model_thinking=os.getenv("BANK_MODEL_THINKING", "true").lower() == "true",
            model_reasoning_effort=os.getenv("BANK_MODEL_REASONING_EFFORT", "max"),
            model_max_tokens=int(os.getenv("BANK_MODEL_MAX_TOKENS", "16384")),
            model_context_tokens=int(os.getenv("BANK_MODEL_CONTEXT_TOKENS", "131072")),
            model_token_safety_margin=int(os.getenv("BANK_MODEL_TOKEN_SAFETY_MARGIN", "2048")),
            task_token_budget=int(os.getenv("BANK_TASK_TOKEN_BUDGET", "1048576")),
            agent_runtime=os.getenv("BANK_AGENT_RUNTIME", "agents_sdk"),
            memory_backend=os.getenv("BANK_MEMORY_BACKEND", "chroma"),
            memory_chroma_url=os.getenv("BANK_CHROMA_URL", "").strip(),
            memory_embedding=os.getenv("BANK_MEMORY_EMBEDDING", "fastembed"),
            memory_model=os.getenv("BANK_MEMORY_MODEL", "BAAI/bge-small-zh-v1.5"),
            memory_top_k=int(os.getenv("BANK_MEMORY_TOP_K", "6")),
            memory_context_chars=int(os.getenv("BANK_MEMORY_CONTEXT_CHARS", "6000")),
            memory_maintenance_enabled=os.getenv('BANK_MEMORY_MAINTENANCE_ENABLED', 'true').lower() == 'true',
            memory_maintenance_interval=float(os.getenv('BANK_MEMORY_MAINTENANCE_INTERVAL', '60')),
            memory_maintenance_batch=int(os.getenv('BANK_MEMORY_MAINTENANCE_BATCH', '64')),
            memory_active_dialogues=int(os.getenv('BANK_MEMORY_ACTIVE_DIALOGUES', '128')),
            max_replans=int(os.getenv("BANK_MAX_REPLANS", "3")),
            research_max_rounds=int(os.getenv("BANK_RESEARCH_MAX_ROUNDS", "2")),
            research_concurrency=int(os.getenv("BANK_RESEARCH_CONCURRENCY", "3")),
            research_queue_limit=int(os.getenv("BANK_RESEARCH_QUEUE_LIMIT", "100")),
            research_timeout=float(os.getenv("BANK_RESEARCH_TIMEOUT", "300")),
            memory_strategy=os.getenv("BANK_MEMORY_STRATEGY", "compressed"),
            scheduler_mode=os.getenv("BANK_SCHEDULER_MODE", "external"),
            scheduler_interval=float(os.getenv("BANK_SCHEDULER_INTERVAL", "15")),
            orchestration_mode=os.getenv("BANK_ORCHESTRATION_MODE", "supervisor"),
            chat_concurrency=int(os.getenv('BANK_CHAT_CONCURRENCY','3')),
            chat_queue_limit=int(os.getenv('BANK_CHAT_QUEUE_LIMIT','100')),
            chat_lease_seconds=float(os.getenv('BANK_CHAT_LEASE_SECONDS','30')),
            chat_worker_mode=os.getenv('BANK_CHAT_WORKER_MODE','embedded'),
        )
