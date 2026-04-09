from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT_PATH = Path(__file__).resolve().parents[2]
BACKEND_ROOT_PATH = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT_PATH / ".env"

# Load local project .env only.
if ENV_PATH.exists():
    load_dotenv(ENV_PATH, override=False)


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _load_settings_values() -> dict[str, str | int]:
    return {
        "PROJECT_ROOT": str(PROJECT_ROOT_PATH),
        "BACKEND_ROOT": str(BACKEND_ROOT_PATH),
        "APP_HOME": _env_str("AGENT_PLAYGROUND_APP_HOME", str(BACKEND_ROOT_PATH)),
        "BUNDLED_SKILLS_ROOT": _env_str(
            "AGENT_PLAYGROUND_BUNDLED_SKILLS_ROOT",
            str(BACKEND_ROOT_PATH / "skills"),
        ),
        "APP_ENV_PATH": _env_str(
            "AGENT_PLAYGROUND_ENV_PATH",
            str(ENV_PATH),
        ),
        "OPENAI_API_KEY": _env_str("OPENAI_API_KEY", ""),
        "OPENAI_BASE_URL": _env_str("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "OPENAI_MODEL": _env_str("OPENAI_MODEL", "gpt-4o-mini"),
        "SKILLHUB_API_KEY": _env_str("SKILLHUB_API_KEY", ""),
        "SKILLHUB_BASE_URL": _env_str("SKILLHUB_BASE_URL", "https://www.skillhub.club/api/v1"),
        "SKILLHUB_TIMEOUT_SECONDS": _env_int("SKILLHUB_TIMEOUT_SECONDS", 20),
    }


@dataclass(frozen=True)
class Settings:
    PROJECT_ROOT: str
    BACKEND_ROOT: str
    APP_HOME: str
    BUNDLED_SKILLS_ROOT: str
    APP_ENV_PATH: str
    OPENAI_API_KEY: str
    OPENAI_BASE_URL: str
    OPENAI_MODEL: str
    SKILLHUB_API_KEY: str
    SKILLHUB_BASE_URL: str
    SKILLHUB_TIMEOUT_SECONDS: int


settings = Settings(**_load_settings_values())


def reload_settings() -> Settings:
    values = _load_settings_values()
    for key, value in values.items():
        object.__setattr__(settings, key, value)
    return settings


def read_app_env_file() -> dict[str, str]:
    env_path = Path(settings.APP_ENV_PATH)
    if not env_path.exists() or not env_path.is_file():
        return {}
    try:
        loaded = dotenv_values(env_path)
    except Exception:  # noqa: BLE001
        return {}

    result: dict[str, str] = {}
    for key, value in loaded.items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        result[key_text] = str(value) if value is not None else ""
    return result


def write_app_env_values(values: dict[str, str]) -> Path:
    env_path = Path(settings.APP_ENV_PATH)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_app_env_file()
    merged = dict(existing)
    for key, value in values.items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        merged[key_text] = str(value or "")

    lines = [f"{key}={merged[key]}" for key in sorted(merged.keys())]
    env_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    for key, value in values.items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        os.environ[key_text] = str(value or "")

    reload_settings()
    return env_path
