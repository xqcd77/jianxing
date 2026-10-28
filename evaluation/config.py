"""Judge credentials and budgets are isolated from the production model."""
from dataclasses import asdict, dataclass, field
import os
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True)
class JudgeSettings:
    api_key: str = field(default="", repr=False)
    provider: str = "mimo"
    base_url: str = "https://api.xiaomimimo.com/v1"
    model: str = "mimo-v2.6-pro"
    thinking: bool = True
    max_completion_tokens: int = 8192
    timeout: float = 120
    format_retries: int = 1
    rpm: int = 20
    concurrency: int = 2

    def __post_init__(self):
        target = urlparse(self.base_url)
        if (target.scheme != "https" or target.hostname != "api.xiaomimimo.com" or target.port not in {None, 443}
                or target.username or target.password or target.query or target.fragment or target.path.rstrip("/") != "/v1"):
            raise ValueError("MiMo judge requires the configured official /v1 endpoint")
        if self.provider != "mimo" or not self.model:
            raise ValueError("Invalid judge provider or model")
        if not (512 <= self.max_completion_tokens <= 32768 and 1 <= self.timeout <= 300
                and 0 <= self.format_retries <= 2 and 1 <= self.rpm <= 100 and 1 <= self.concurrency <= 6):
            raise ValueError("Invalid judge budget")

    def public(self):
        return {k: v for k, v in asdict(self).items() if k != "api_key"}

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        return cls(
            api_key=os.getenv("BANK_JUDGE_API_KEY", ""),
            provider=os.getenv("BANK_JUDGE_PROVIDER", "mimo"),
            base_url=os.getenv("BANK_JUDGE_BASE_URL", "https://api.xiaomimimo.com/v1").rstrip("/"),
            model=os.getenv("BANK_JUDGE_MODEL", "mimo-v2.6-pro"),
            thinking=os.getenv("BANK_JUDGE_THINKING", "true").lower() == "true",
            max_completion_tokens=int(os.getenv("BANK_JUDGE_MAX_COMPLETION_TOKENS", "8192")),
            timeout=float(os.getenv("BANK_JUDGE_TIMEOUT", "120")),
            format_retries=int(os.getenv("BANK_JUDGE_FORMAT_RETRIES", "1")),
            rpm=int(os.getenv("BANK_JUDGE_RPM", "20")),
            concurrency=int(os.getenv("BANK_JUDGE_CONCURRENCY", "2")),
        )
