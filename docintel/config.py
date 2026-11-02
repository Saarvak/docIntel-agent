from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    """Minimal .env loader; real environment variables always win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    agent_name: str
    agent_seed: str
    agent_port: int
    agent_mailbox: bool
    agentverse_api_key: str
    mineru_api_url: str
    mineru_backend: str
    mineru_lang: str
    mineru_timeout: int
    mineru_offline_fallback: bool
    samples_dir: Path
    gemini_api_keys: tuple[str, ...]
    gemini_model: str
    review_confidence_threshold: float
    max_download_mb: int


def get_settings() -> Settings:
    load_env_file()
    keys = [os.getenv("GEMINI_API_KEY", "").strip()]
    index = 1
    while os.getenv(f"GEMINI_API_KEY{index}"):
        keys.append(os.environ[f"GEMINI_API_KEY{index}"].strip())
        index += 1

    return Settings(
        agent_name=os.getenv("AGENT_NAME", "docintel-agent"),
        agent_seed=os.getenv("AGENT_SEED", "docintel-agent local development seed - change me"),
        agent_port=int(os.getenv("AGENT_PORT", "8002")),
        agent_mailbox=_bool("AGENT_MAILBOX", False),
        agentverse_api_key=os.getenv("AGENTVERSE_API_KEY", "").strip(),
        mineru_api_url=os.getenv("MINERU_API_URL", "http://localhost:8000").rstrip("/"),
        mineru_backend=os.getenv("MINERU_BACKEND", "pipeline"),
        mineru_lang=os.getenv("MINERU_LANG_LIST", "en"),
        mineru_timeout=int(os.getenv("MINERU_TIMEOUT_SECONDS", "600")),
        mineru_offline_fallback=_bool("MINERU_OFFLINE_FALLBACK", True),
        samples_dir=Path(os.getenv("SAMPLES_DIR", str(PROJECT_ROOT / "samples"))),
        gemini_api_keys=tuple(dict.fromkeys(k for k in keys if k)),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
        review_confidence_threshold=float(os.getenv("REVIEW_CONFIDENCE_THRESHOLD", "0.80")),
        max_download_mb=int(os.getenv("MAX_DOWNLOAD_MB", "25")),
    )
