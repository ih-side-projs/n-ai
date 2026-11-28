"""X7: configuration. Precedence: command-line option > environment > TOML file > built-in default.

TOML layout::

    default_profile = "default"
    [server]
    base_url = "http://localhost:8080/v1"
    api_key = "none"
    timeout = 3600
    stream = true
    swap_threshold_seconds = 10
    [profiles.default]
    swap_cost_seconds = 25
    [profiles.default.reasoner]
    model_id = "qwen3.6-reasoning"
    ctx = 32768
    max_tokens = 16384
    temperature = 0.6
    top_p = 0.95
    top_k = 20
    [profiles.default.coder]
    model_id = "qwen3-coder"
    repeat_penalty = 1.05      # any unknown key is forwarded as a sampling parameter

Environment: LLAMA_SWAP_URL, LLAMA_SWAP_KEY, REASONER_MODEL, CODER_MODEL, NAI_CONFIG.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import ModelProfile, default_profile, with_overrides


class ConfigError(Exception):
    pass


@dataclass
class ServerConfig:
    base_url: str = "http://localhost:8080/v1"
    api_key: str = "none"
    timeout: float = 3600.0
    stream: bool = True
    swap_threshold_seconds: float = 10.0


@dataclass
class SizeLimits:
    """X9: size limits are config, not constants."""
    max_requirements_per_component: int = 8
    max_public_functions_per_component: int = 10
    max_components_per_level: int = 7
    max_depth: int = 4
    # D2: candidate count for component-map generation (separate from X9)
    candidates_root: int = 3
    candidates_nonroot: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> SizeLimits:
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    profiles: dict[str, ModelProfile] = field(default_factory=lambda: {"default": default_profile()})
    default_profile: str = "default"
    limits: SizeLimits = field(default_factory=SizeLimits)

    def get_profile(self, name: str | None = None, *, reasoner: str | None = None, coder: str | None = None,
                    ctx: int | None = None, coder_ctx: int | None = None, max_tokens: int | None = None,
                    env=os.environ) -> ModelProfile:
        """Named profile with overrides applied. Explicit arguments beat REASONER_MODEL / CODER_MODEL."""
        name = name or self.default_profile
        if name not in self.profiles:
            raise ConfigError(f"unknown model profile {name!r}; known: {', '.join(sorted(self.profiles))}")
        return with_overrides(self.profiles[name], reasoner=reasoner or env.get("REASONER_MODEL"),
                              coder=coder or env.get("CODER_MODEL"), ctx=ctx, coder_ctx=coder_ctx, max_tokens=max_tokens)


def _toml_load(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:  # Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError as e:
            raise ConfigError("reading a TOML config needs Python 3.11+ or the 'tomli' package") from e
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e


def find_config(explicit: str | Path | None = None, env=os.environ, cwd: Path | None = None) -> Path | None:
    for cand in (explicit, env.get("NAI_CONFIG")):
        if cand:
            p = Path(cand)
            if not p.is_file():
                raise ConfigError(f"config file not found: {p}")
            return p
    p = (cwd or Path.cwd()) / "nai.toml"
    return p if p.is_file() else None


def load_config(path: str | Path | None = None, env=os.environ, cwd: Path | None = None) -> Config:
    cfg = Config()
    p = find_config(path, env, cwd)
    if p:
        data = _toml_load(p)
        srv = data.get("server", {})
        unknown = set(srv) - set(ServerConfig.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"unknown [server] keys: {', '.join(sorted(unknown))}")
        cfg.server = ServerConfig(**srv)
        for name, d in data.get("profiles", {}).items():
            try:
                cfg.profiles[name] = ModelProfile.from_dict(name, d)
            except (ValueError, TypeError) as e:
                raise ConfigError(f"profile {name!r}: {e}") from e
        cfg.default_profile = data.get("default_profile", cfg.default_profile)
        if "limits" in data:
            cfg.limits = SizeLimits.from_dict(data["limits"] or {})
    if env.get("LLAMA_SWAP_URL"):
        cfg.server.base_url = env["LLAMA_SWAP_URL"]
    if env.get("LLAMA_SWAP_KEY"):
        cfg.server.api_key = env["LLAMA_SWAP_KEY"]
    return cfg
