"""SS-BUILD: Build pipeline, target environment, and escalation handling."""

from .context_pack import ContextPack, assemble_context_pack
from .escalation import (
    EscalationPacket,
    build_escalation_packet,
    raise_change_request,
    redact_escalation_packet,
    resolve_escalation,
)
from .plugin import plan, register
from .stubs import StubResult, extract_stub_signatures, generate_contract_stubs
from .tenv import (
    LintResult,
    TargetEnv,
    TargetEnvError,
    TargetEnvManager,
    detect_stale_api,
    format_env_block,
    load_api_notes,
    parse_target_env,
)
from .traceability import TraceabilityResult, check_traceability

__all__ = [
    "ContextPack",
    "EscalationPacket",
    "LintResult",
    "StubResult",
    "TargetEnv",
    "TargetEnvError",
    "TargetEnvManager",
    "TraceabilityResult",
    "assemble_context_pack",
    "build_escalation_packet",
    "check_traceability",
    "detect_stale_api",
    "extract_stub_signatures",
    "format_env_block",
    "generate_contract_stubs",
    "load_api_notes",
    "parse_target_env",
    "plan",
    "raise_change_request",
    "redact_escalation_packet",
    "register",
    "resolve_escalation",
]
