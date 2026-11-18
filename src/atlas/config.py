"""Configuration: settings.toml (+ ATLAS_* env overrides), policy.yaml and time_baselines.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


def project_root() -> Path:
    """Folder that holds config/ and data/. Override with ATLAS_HOME."""
    if env := os.environ.get("ATLAS_HOME"):
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "policy.yaml").exists():
            return candidate
    return here


class RateLimitSettings(BaseModel):
    requests_per_minute: int = 0  # 0 = unlimited


class ModelSettings(BaseModel):
    provider: Literal["ollama"] = "ollama"
    host: str = "http://localhost:11434"
    model_id: str = "qwen3.5:4b"  # chosen by `atlas bench`, see docs/adr/0005-default-model.md
    temperature: float = 0.2
    keep_alive: str = "10m"
    num_ctx: int = 8192
    think: Literal["auto"] | bool = "auto"  # auto = off when the model allows it (see agent/model.py)
    rate_limit: RateLimitSettings = RateLimitSettings()


class WebSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class BriefingSettings(BaseModel):
    hour: int = 7
    minute: int = 0


class CanvasSettings(BaseModel):
    enabled: bool = False
    mode: Literal["api", "ics"] = "api"  # api = personal token (has submission status); ics = calendar feed fallback
    base_url: str = ""  # e.g. "https://YOUR-SCHOOL.instructure.com" (https only)


class GcalSettings(BaseModel):
    enabled: bool = False
    mode: Literal["api", "ics"] = "api"  # api = OAuth (read-only scope); ics = "secret address in iCal format"
    calendars: list[str] = ["primary"]


class RoutineSettings(BaseModel):
    enabled: bool = True
    path: str = "config/routine.yaml"


class SourcesSettings(BaseModel):
    canvas: CanvasSettings = CanvasSettings()
    gcal: GcalSettings = GcalSettings()
    routine: RoutineSettings = RoutineSettings()


class SyncSettings(BaseModel):
    interval_minutes: int = 30
    past_days: int = 1  # sync window: [now - past_days, now + future_days)
    future_days: int = 30
    announcement_days: int = 14
    max_backoff_hours: int = 6


class Settings(BaseSettings):
    timezone: str = "America/New_York"
    data_dir: str = "data"
    model: ModelSettings = ModelSettings()
    web: WebSettings = WebSettings()
    briefing: BriefingSettings = BriefingSettings()
    sources: SourcesSettings = SourcesSettings()
    sync: SyncSettings = SyncSettings()

    model_config = SettingsConfigDict(env_prefix="ATLAS_", env_nested_delimiter="__", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # ATLAS_SETTINGS_FILE overrides the location (the test suite points it at nothing, so tests never
        # depend on the developer's own config/settings.toml)
        toml_path = Path(os.environ.get("ATLAS_SETTINGS_FILE") or project_root() / "config" / "settings.toml")
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if toml_path.exists():
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_path))
        return tuple(sources)

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else project_root() / p

    @property
    def db_url(self) -> str:
        return f"sqlite:///{(self.data_path / 'atlas.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------- policy

Tier = Literal["T0", "T1", "T2", "T3"]
Trust = Literal["trusted", "untrusted"]


@dataclass(frozen=True)
class ToolPolicy:
    tier: Tier = "T3"
    output_trust: Trust = "untrusted"


@dataclass
class Policy:
    tools: dict[str, ToolPolicy] = field(default_factory=dict)
    contexts: dict[str, list[str]] = field(default_factory=dict)
    graduated: list[str] = field(default_factory=list)

    def tool(self, name: str) -> ToolPolicy:
        """Unknown tools are T3 (deny) with untrusted output."""
        return self.tools.get(name, ToolPolicy())

    def allowed(self, context: str, tool_name: str) -> bool:
        return tool_name in self.contexts.get(context, [])

    @classmethod
    def from_dict(cls, raw: dict) -> "Policy":
        tools = {name: ToolPolicy(**(spec or {})) for name, spec in (raw.get("tools") or {}).items()}
        return cls(
            tools=tools,
            contexts={k: list(v or []) for k, v in (raw.get("contexts") or {}).items()},
            graduated=list(raw.get("graduated") or []),
        )


def load_policy(path: Path | None = None) -> Policy:
    path = path or project_root() / "config" / "policy.yaml"
    with open(path, encoding="utf-8") as f:
        return Policy.from_dict(yaml.safe_load(f) or {})


def load_time_baselines(path: Path | None = None) -> dict:
    path = path or project_root() / "config" / "time_baselines.yaml"
    if not path.exists():
        return {"tasks": {}, "tool_to_task": {}}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {"tasks": {}, "tool_to_task": {}}
