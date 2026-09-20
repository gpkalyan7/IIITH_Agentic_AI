"""Runtime configuration for inboxHero.

Everything that could differ between machines -- model provider, endpoint,
credentials, pacing -- is read from the environment here and nowhere else.
No module outside this one touches os.environ.

Copy .env.example to .env and edit it. .env is never committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Directories the system writes to. Nothing else is writable.
DATA_DIR = ROOT / "data"
OUTBOX_DIR = ROOT / "outbox"
CACHE_DIR = DATA_DIR / "llm_cache"

INBOX_PATH = ROOT / "inbox.json"
TRACE_PATH = ROOT / "trace.jsonl"
PREFS_PATH = DATA_DIR / "prefs.json"
STATE_PATH = DATA_DIR / "state.json"
DASHBOARD_HTML = ROOT / "dashboard.html"
DASHBOARD_JSON = ROOT / "dashboard.json"
DECISIONS_PATH = ROOT / "decisions.json"

# The mailbox owner. Used to tell "mail to me" from "mail I sent", and as the
# reference point for look-alike domain detection.
OWNER_ADDRESS = "sam@paperjet.io"
OWNER_DOMAIN = "paperjet.io"


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader so the project has no dependency just to read a file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment variables win over the file.
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    provider: str
    model: str
    ollama_host: str
    api_key: str
    request_interval: float
    max_retries: int
    timeout: float
    use_cache: bool
    temperature: float
    owner: str = OWNER_ADDRESS
    owner_domain: str = OWNER_DOMAIN
    paths: dict = field(default_factory=dict)

    @property
    def is_offline(self) -> bool:
        return self.provider == "offline"


def load_config() -> Config:
    provider = _env("LLM_PROVIDER", "ollama").lower()
    default_model = {
        "ollama": "llama3.1:8b",
        "gemini": "gemini-1.5-flash",
        "openai": "gpt-4o-mini",
        "offline": "deterministic-offline",
    }.get(provider, "llama3.1:8b")

    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()

    return Config(
        provider=provider,
        model=_env("LLM_MODEL", default_model),
        ollama_host=_env("OLLAMA_HOST", "http://localhost:11434"),
        api_key=api_key,
        # Free API tiers are often ~15 requests/minute. Four seconds between
        # calls keeps us under that without any special-casing. A local model
        # has no such limit, so the default there is no delay at all.
        request_interval=_env_float(
            "LLM_REQUEST_INTERVAL", 0.0 if provider in ("ollama", "offline") else 4.0
        ),
        max_retries=_env_int("LLM_MAX_RETRIES", 4),
        timeout=_env_float("LLM_TIMEOUT", 120.0),
        use_cache=_env("LLM_CACHE", "1") not in ("0", "false", "no"),
        temperature=_env_float("LLM_TEMPERATURE", 0.1),
        paths={
            "inbox": INBOX_PATH,
            "trace": TRACE_PATH,
            "prefs": PREFS_PATH,
            "state": STATE_PATH,
            "outbox": OUTBOX_DIR,
            "cache": CACHE_DIR,
        },
    )


def ensure_dirs() -> None:
    for directory in (DATA_DIR, OUTBOX_DIR, CACHE_DIR):
        directory.mkdir(parents=True, exist_ok=True)
