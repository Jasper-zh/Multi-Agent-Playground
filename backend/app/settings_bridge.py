from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


PROJECT_ROOT_PATH = Path(__file__).resolve().parents[2]
BACKEND_ROOT_PATH = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT_PATH / ".env"


def _load_bootstrap_env_files() -> None:
    env_paths: list[Path] = []
    app_env_path = str(os.getenv("AGENT_PLAYGROUND_ENV_PATH", "")).strip()
    if app_env_path:
        env_paths.append(Path(app_env_path))
    env_paths.append(ENV_PATH)

    seen: set[str] = set()
    for env_path in env_paths:
        try:
            normalized = str(env_path.resolve())
        except OSError:
            normalized = str(env_path)
        if normalized in seen:
            continue
        seen.add(normalized)
        if env_path.exists() and env_path.is_file():
            load_dotenv(env_path, override=False)


_load_bootstrap_env_files()


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
        "BUNDLED_RUNTIME_ROOT": _env_str(
            "AGENT_PLAYGROUND_BUNDLED_RUNTIME_ROOT",
            "",
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
    BUNDLED_RUNTIME_ROOT: str
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


def default_structured_settings() -> dict[str, object]:
    return {
        "model_profiles": [
            {
                "id": "default",
                "provider": "custom",
                "name": "Default",
                "api_key": settings.OPENAI_API_KEY,
                "base_url": settings.OPENAI_BASE_URL,
                "model": settings.OPENAI_MODEL,
            }
        ],
        "active_model_profile_id": "default",
        "env_vars": [],
    }


def _normalize_model_profiles(raw_profiles: object) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not isinstance(raw_profiles, list):
        raw_profiles = []
    for index, item in enumerate(raw_profiles):
        if not isinstance(item, dict):
            continue
        profile_id = str(item.get("id") or f"profile_{index + 1}").strip() or f"profile_{index + 1}"
        normalized.append(
            {
                "id": profile_id,
                "provider": str(item.get("provider") or "custom").strip() or "custom",
                "name": str(item.get("name") or f"Profile {index + 1}").strip() or f"Profile {index + 1}",
                "api_key": str(item.get("api_key") or "").strip(),
                "base_url": str(item.get("base_url") or "https://api.openai.com/v1").strip()
                or "https://api.openai.com/v1",
                "model": str(item.get("model") or "gpt-4o-mini").strip() or "gpt-4o-mini",
            }
        )
    if normalized:
        return normalized
    return default_structured_settings()["model_profiles"]  # type: ignore[return-value]


def _normalize_env_vars(raw_env_vars: object) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not isinstance(raw_env_vars, list):
        return normalized
    seen: set[str] = set()
    for item in raw_env_vars:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "key": key,
                "value": str(item.get("value") or ""),
            }
        )
    return normalized


def normalize_structured_settings(raw: dict[str, object] | None = None) -> dict[str, object]:
    payload = raw or default_structured_settings()
    model_profiles = _normalize_model_profiles(payload.get("model_profiles"))
    active_model_profile_id = str(payload.get("active_model_profile_id") or "").strip() or model_profiles[0]["id"]
    if active_model_profile_id not in {profile["id"] for profile in model_profiles}:
        active_model_profile_id = model_profiles[0]["id"]
    env_vars = _normalize_env_vars(payload.get("env_vars"))
    return {
        "model_profiles": model_profiles,
        "active_model_profile_id": active_model_profile_id,
        "env_vars": env_vars,
    }


def _resolve_active_profile(payload: dict[str, object]) -> dict[str, str]:
    normalized = normalize_structured_settings(payload)
    active_id = str(normalized["active_model_profile_id"])
    profiles = normalized["model_profiles"]
    for profile in profiles:  # type: ignore[assignment]
        if profile["id"] == active_id:
            return profile
    return profiles[0]  # type: ignore[index]


def apply_structured_settings(
    previous_payload: dict[str, object] | None,
    current_payload: dict[str, object],
) -> Path:
    previous = normalize_structured_settings(previous_payload)
    current = normalize_structured_settings(current_payload)
    previous_keys = {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        *[item["key"] for item in previous["env_vars"]],  # type: ignore[index]
    }
    active_profile = _resolve_active_profile(current)
    managed_values = {
        "OPENAI_API_KEY": active_profile["api_key"],
        "OPENAI_BASE_URL": active_profile["base_url"],
        "OPENAI_MODEL": active_profile["model"],
    }
    for item in current["env_vars"]:  # type: ignore[index]
        managed_values[str(item["key"])] = str(item["value"])

    env_path = Path(settings.APP_ENV_PATH)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_app_env_file()
    merged = {key: value for key, value in existing.items() if key not in previous_keys}
    merged.update(managed_values)
    lines = [f"{key}={merged[key]}" for key in sorted(merged.keys())]
    env_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    for key in previous_keys:
        if key not in managed_values and key in os.environ:
            os.environ.pop(key, None)
    for key, value in managed_values.items():
        os.environ[key] = str(value or "")

    reload_settings()
    return env_path


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
