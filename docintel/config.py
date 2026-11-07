from __future__ import annotations

import os
from pathlib import Path

try:
    _HERE = Path(__file__).resolve().parent
    # Local: this file lives in docintel/. Hosted Agentverse flattens it next to agent.py.
    PROJECT_ROOT = _HERE if (_HERE / "agent.py").exists() else _HERE.parent
except NameError:
    # Agentverse Hosted executes files without __file__.
    PROJECT_ROOT = Path(".")


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


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if value is None:
        return default
    return str(value).strip().strip('"').strip("'")


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value == "":
        return default
    return value.lower() in {"1", "true", "yes", "on"}


class Settings:
    """Runtime config. Plain class so Agentverse Hosted can import it (no dataclass)."""

    def __init__(
        self,
        agent_name: str,
        agent_seed: str,
        agent_port: int,
        agent_mailbox: bool,
        agentverse_api_key: str,
        mineru_api_url: str,
        mineru_api_token: str,
        mineru_backend: str,
        mineru_lang: str,
        mineru_ocr: bool | None,
        mineru_timeout: int,
        samples_dir: Path,
        gemini_api_keys: tuple[str, ...],
        gemini_model: str,
        review_confidence_threshold: float,
        max_download_mb: int,
    ) -> None:
        self.agent_name = agent_name
        self.agent_seed = agent_seed
        self.agent_port = agent_port
        self.agent_mailbox = agent_mailbox
        self.agentverse_api_key = agentverse_api_key
        self.mineru_api_url = mineru_api_url
        self.mineru_api_token = mineru_api_token
        self.mineru_backend = mineru_backend
        self.mineru_lang = mineru_lang
        self.mineru_ocr = mineru_ocr
        self.mineru_timeout = mineru_timeout
        self.samples_dir = samples_dir
        self.gemini_api_keys = gemini_api_keys
        self.gemini_model = gemini_model
        self.review_confidence_threshold = review_confidence_threshold
        self.max_download_mb = max_download_mb

    def replace(self, **changes) -> Settings:
        data = dict(self.__dict__)
        data.update(changes)
        return Settings(**data)


def get_settings() -> Settings:
    load_env_file()
    keys = [_env("GEMINI_API_KEY")]
    index = 1
    while _env(f"GEMINI_API_KEY{index}"):
        keys.append(_env(f"GEMINI_API_KEY{index}"))
        index += 1

    return Settings(
        agent_name=_env("AGENT_NAME", "docintel-agent"),
        agent_seed=_env("AGENT_SEED", "docintel-agent local development seed - change me"),
        agent_port=int(_env("AGENT_PORT", "8002") or "8002"),
        agent_mailbox=_bool("AGENT_MAILBOX", False),
        agentverse_api_key=_env("AGENTVERSE_API_KEY"),
        mineru_api_url=(_env("MINERU_API_URL", "https://mineru.net") or "https://mineru.net").rstrip("/"),
        mineru_api_token=_env("MINERU_API_TOKEN") or _env("MINERU_API_KEY"),
        mineru_backend=_env("MINERU_BACKEND", "pipeline") or "pipeline",
        mineru_lang=(_env("MINERU_LANG_LIST", "en").split(",")[0].strip() or "en"),
        mineru_ocr=None if _env("MINERU_OCR") == "" else _bool("MINERU_OCR", False),
        mineru_timeout=int(_env("MINERU_TIMEOUT_SECONDS", "600") or "600"),
        samples_dir=Path(_env("SAMPLES_DIR") or str(PROJECT_ROOT / "samples")),
        gemini_api_keys=tuple(dict.fromkeys(k for k in keys if k)),
        gemini_model=_env("GEMINI_MODEL", "gemini-flash-latest") or "gemini-flash-latest",
        review_confidence_threshold=float(_env("REVIEW_CONFIDENCE_THRESHOLD", "0.80") or "0.80"),
        max_download_mb=int(_env("MAX_DOWNLOAD_MB", "25") or "25"),
    )
