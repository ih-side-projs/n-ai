"""CORE-7: ModelProfile (section 4) and ModelClient: model id and sampling chosen by role, never hard-coded."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .llm import LLM, ChatResult

ROLES = ("reasoner", "coder")
_SAMPLING_KEYS = ("temperature", "top_p", "top_k")


@dataclass
class RoleConfig:
    model_id: str
    ctx: int = 32768
    max_tokens: int = 16384
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int | None = 20
    extras: dict[str, Any] = field(default_factory=dict)   # any other sampling parameter forwarded to the server

    def sampling(self) -> dict:
        s: dict[str, Any] = {"temperature": self.temperature, "top_p": self.top_p}
        if self.top_k is not None:
            s["top_k"] = self.top_k
        s.update(self.extras)
        return s

    @classmethod
    def from_dict(cls, d: dict) -> RoleConfig:
        if "model_id" not in d:
            raise ValueError("role block needs model_id")
        d = dict(d)
        known = {k: d.pop(k) for k in ("model_id", "ctx", "max_tokens", "temperature", "top_p", "top_k") if k in d}
        extras = dict(d.pop("extras", {}) or {})
        extras.update(d)   # unknown keys are extra sampling parameters (e.g. repeat_penalty)
        return cls(extras=extras, **known)

    def to_dict(self) -> dict:
        d = {"model_id": self.model_id, "ctx": self.ctx, "max_tokens": self.max_tokens,
             "temperature": self.temperature, "top_p": self.top_p}
        if self.top_k is not None:
            d["top_k"] = self.top_k
        d.update(self.extras)
        return d


@dataclass
class ModelProfile:
    name: str
    reasoner: RoleConfig
    coder: RoleConfig
    swap_cost_seconds: float | None = None

    def role(self, role: str) -> RoleConfig:
        if role not in ROLES:
            raise ValueError(f"unknown model role {role!r}; expected one of {ROLES}")
        return getattr(self, role)

    @classmethod
    def from_dict(cls, name: str, d: dict) -> ModelProfile:
        for r in ROLES:
            if r not in d:
                raise ValueError(f"profile {name!r}: missing [{r}] block")
        return cls(name, RoleConfig.from_dict(d["reasoner"]), RoleConfig.from_dict(d["coder"]), d.get("swap_cost_seconds"))

    def to_dict(self) -> dict:
        d = {"reasoner": self.reasoner.to_dict(), "coder": self.coder.to_dict()}
        if self.swap_cost_seconds is not None:
            d["swap_cost_seconds"] = self.swap_cost_seconds
        return d


def default_profile() -> ModelProfile:
    """The built-in profile: v1's models and Qwen-recommended sampling."""
    return ModelProfile(
        "default",
        RoleConfig("qwen3.6-reasoning", temperature=0.6, top_p=0.95, top_k=20),
        RoleConfig("qwen3-coder", temperature=0.7, top_p=0.8, top_k=20, extras={"repeat_penalty": 1.05}))


class ModelClient:
    """An LLM plus a ModelProfile: callers say `chat("coder", ...)`, not which model or sampling."""

    def __init__(self, llm: LLM, profile: ModelProfile):
        self.llm, self.profile = llm, profile

    def model_id(self, role: str) -> str:
        return self.profile.role(role).model_id

    def ctx(self, role: str) -> int:
        return self.profile.role(role).ctx

    def max_tokens(self, role: str) -> int:
        return self.profile.role(role).max_tokens

    def sampling(self, role: str, **overrides) -> dict:
        """Role sampling with per-call overrides (e.g. higher temperature for candidate generation, D8)."""
        s = self.profile.role(role).sampling()
        s.update(overrides)
        return s

    def chat(self, role: str, messages, tools=None, max_tokens: int | None = None, tag: str | None = None,
             response_format: dict | None = None, grammar: str | None = None,
             sampling: dict | None = None, meta: dict | None = None, cancel_event=None) -> ChatResult:
        rc = self.profile.role(role)
        s = rc.sampling()
        s.update(sampling or {})
        return self.llm.chat(rc.model_id, messages, s, tools=tools, max_tokens=max_tokens or rc.max_tokens, tag=tag or role,
                             response_format=response_format, grammar=grammar,
                             meta={"role": role, **(meta or {})}, cancel_event=cancel_event)


def with_overrides(profile: ModelProfile, *, reasoner: str | None = None, coder: str | None = None,
                   ctx: int | None = None, coder_ctx: int | None = None, max_tokens: int | None = None) -> ModelProfile:
    """A copy of `profile` with the given values replaced (None = keep)."""
    def tweak(rc: RoleConfig, model, c):
        return replace(rc, model_id=model or rc.model_id, ctx=c or rc.ctx, max_tokens=max_tokens or rc.max_tokens,
                       extras=dict(rc.extras))
    return replace(profile, reasoner=tweak(profile.reasoner, reasoner, ctx), coder=tweak(profile.coder, coder, coder_ctx))
