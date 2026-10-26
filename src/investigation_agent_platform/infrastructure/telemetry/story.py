# src/investigation_agent_platform/infrastructure/telemetry/story.py
"""Story-logging primitives (D1/D2/D8/D9). Sandbox-safe leaf: stdlib only.

Call sites emit semantics (``comp/func/stage/step/outcome/inputs/outputs``);
template, ordering, caps, and level routing resolve from configuration
(``story_config``: env > logging.yaml > code defaults). Changing any of those
requires no call-site edits.

Temporal-sandbox note: workflow code may call these helpers. All
configuration reads are cached process-global behind broad ``try/except`` so
a restricted sandbox (or missing YAML) falls back to code defaults; values
never branch workflow decisions, so replay determinism is unaffected.
"""

from __future__ import annotations

import contextvars
import logging
import time
from typing import Any

TRACE_LEVEL = 5

# Component codes (D3, final). Stable once shipped.
COMP_API = "API"
COMP_WORKER = "TMP-WRKR"
COMP_ACTVTY = "ACTVTY"
COMP_WFLW = "WFLW"
COMP_SVC = "SVC"
COMP_DOMAIN = "DOMAIN"
COMP_REPO = "REPO"
COMP_PROV = "PROV"
COMP_TOOL = "TOOL"

COMPONENTS = (
    COMP_API,
    COMP_WORKER,
    COMP_ACTVTY,
    COMP_WFLW,
    COMP_SVC,
    COMP_DOMAIN,
    COMP_REPO,
    COMP_PROV,
    COMP_TOOL,
)

# Closed stage vocabulary (D3).
STAGES = ("intake", "reason", "force", "execute", "verify", "conclude", "persist", "publish")

OUTCOME_STARTED = "started"
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"


def register_trace_level() -> None:
    """Register stdlib TRACE=5 with a ``Logger.trace`` method (idempotent)."""
    if logging.getLevelName(TRACE_LEVEL) != f"Level {TRACE_LEVEL}":
        return  # already registered (name set)
    logging.addLevelName(TRACE_LEVEL, "TRACE")

    def _trace(self: logging.Logger, msg: object, *args: object, **kwargs: object) -> None:
        if self.isEnabledFor(TRACE_LEVEL):
            self._log(TRACE_LEVEL, msg, args, **kwargs)

    if not hasattr(logging.Logger, "trace"):
        logging.Logger.trace = _trace  # type: ignore[attr-defined]


def _sanitize_mapping(mapping: dict[str, Any], extra_keys: list[str]) -> dict[str, Any]:
    try:
        from investigation_agent_platform.api.errors import sanitize_extra

        redacted = sanitize_extra(mapping)
    except Exception:
        redacted = dict(mapping)
    for key in extra_keys or []:
        if key in redacted:
            redacted[key] = "[REDACTED]"
    return redacted


def _truncate(value: Any, *, max_str: int, max_list: int) -> Any:
    if isinstance(value, str):
        return value[:max_str]
    if isinstance(value, (list, tuple)):
        return [_truncate(v, max_str=max_str, max_list=max_list) for v in list(value)[:max_list]]
    if isinstance(value, dict):
        return {
            str(k)[:128]: _truncate(v, max_str=max_str, max_list=max_list)
            for k, v in list(value.items())[:max_list]
        }
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:max_str]


def _cfg() -> dict[str, Any]:
    try:
        from investigation_agent_platform.infrastructure.telemetry import story_config

        return story_config.load_story_config()
    except Exception:
        return {}


def _caps() -> dict[str, int]:
    cfg = _cfg()
    base = {"max_keys": 20, "max_str": 1000, "max_list": 20, "max_key_len": 128}
    try:
        base.update({k: int(v) for k, v in dict(cfg.get("caps", {})).items() if k in base})
    except Exception:
        pass
    return base


def story_event(
    *,
    comp: str,
    func: str,
    stage: str,
    step: str,
    outcome: str = "",
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    elapsed_ms: int | None = None,
    error: str | None = None,
    error_type: str | None = None,
    tenant_id: str = "",
    investigation_id: Any = "",
    event: str = "",
) -> dict[str, Any]:
    """Build the story envelope as a stdlib-logging ``extra`` dict.

    Inputs/outputs are truncated then sanitized (built-in policy plus
    configured extra keys). ``event`` keeps the legacy stable name so
    ``scripts/story.py`` matchers keep working.
    """
    cfg = _cfg()
    caps = _caps()
    try:
        bound = story_context()
    except Exception:
        bound = {}
    tid = tenant_id or bound.get("tenant_id", "")
    iid = investigation_id
    if (iid is None or iid == "") and bound.get("investigation_id"):
        iid = bound["investigation_id"]
    extra: dict[str, Any] = {
        "tenant_id": tid or "",
        "investigation_id": "" if iid is None else str(iid),
        "event": event or f"{func} {outcome or OUTCOME_STARTED}",
        "comp": comp,
        "func": func,
        "stage": stage,
        "step": step,
        "outcome": outcome,
    }
    if elapsed_ms is not None:
        extra["elapsed_ms"] = elapsed_ms
    if error is not None:
        extra["error"] = error
    if error_type is not None:
        extra["error_type"] = error_type
    redact_extra: list[str] = []
    try:
        redact_extra = list(cfg.get("extra_redacted_keys", []) or [])
    except Exception:
        pass
    if inputs is not None:
        capped = {
            str(k)[: caps["max_key_len"]]: _truncate(
                v, max_str=caps["max_str"], max_list=caps["max_list"]
            )
            for k, v in list(inputs.items())[: caps["max_keys"]]
        }
        extra["inputs"] = _sanitize_mapping(capped, redact_extra)
    if outputs is not None:
        capped = {
            str(k)[: caps["max_key_len"]]: _truncate(
                v, max_str=caps["max_str"], max_list=caps["max_list"]
            )
            for k, v in list(outputs.items())[: caps["max_keys"]]
        }
        extra["outputs"] = _sanitize_mapping(capped, redact_extra)
    return extra


def story_message(
    *,
    comp: str,
    func: str,
    stage: str,
    step: str,
    outcome: str = "",
    note: str = "",
) -> str:
    """Render the fixed-head human line from the configured template.

    Unknown placeholders fail closed to the default template (logged once at
    DEBUG by the caller chain — never raised).
    """
    cfg = _cfg()
    template = "[{comp}] {func} · {stage}/{step} · {outcome}"
    try:
        template = str(cfg.get("message_template", template))
    except Exception:
        pass
    values = {"comp": comp, "func": func, "stage": stage, "step": step, "outcome": outcome}
    try:
        head = template.format(**values)
    except Exception:
        head = "[{comp}] {func} · {stage}/{step} · {outcome}".format(**values)
    if note:
        return f"{head} — {note}"
    return head


def _level_number(name: str) -> int:
    upper = (name or "").upper()
    if upper == "TRACE":
        return TRACE_LEVEL
    return int(getattr(logging, upper, logging.INFO))


def story_level(comp: str, stage: str, asked: int = logging.INFO) -> int:
    """Resolve the effective level: quietest of asked, component default, ceiling.

    Lets operators reroute a noisy component (``REPO=DEBUG``) or cap a stage
    (``persist``) via configuration with zero call-site edits.
    """
    try:
        cfg = _cfg()
        comp_default = _level_number(str((cfg.get("component_levels", {}) or {}).get(comp, "INFO")))
        stage_cap = _level_number(str((cfg.get("stage_ceiling", {}) or {}).get(stage, "INFO")))
        return min(int(asked), comp_default, stage_cap)
    except Exception:
        return int(asked)


def tick() -> float:
    """Start timestamp for elapsed measurement (``elapsed_ms(tick())``)."""
    return time.perf_counter()


def elapsed_ms(since: float) -> int:
    """Whole milliseconds since ``tick()`` (never negative)."""
    try:
        return max(0, int((time.perf_counter() - since) * 1000))
    except Exception:
        return 0


# Bound story context (D11). Same key names the API binds via structlog
# (``correlation_id``) plus worker identity (``tenant_id``,
# ``investigation_id``). A logging Filter (``logging.py``) injects these into
# every stdlib record unless the record already carries the key — explicit
# ``extra`` always wins. Async-safe: contextvars are task-local.
_CTX_TENANT: contextvars.ContextVar[str] = contextvars.ContextVar("iap_story_tenant_id", default="")
_CTX_INVESTIGATION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "iap_story_investigation_id", default=""
)
_CTX_CORRELATION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "iap_story_correlation_id", default=""
)

CONTEXT_KEYS = ("tenant_id", "investigation_id", "correlation_id")


def bind_story_context(
    *,
    tenant_id: str = "",
    investigation_id: Any = "",
    correlation_id: str = "",
) -> None:
    """Bind identity for the current task; injected into all later records."""
    if tenant_id:
        _CTX_TENANT.set(str(tenant_id))
    if investigation_id is not None and str(investigation_id):
        _CTX_INVESTIGATION.set(str(investigation_id))
    if correlation_id:
        _CTX_CORRELATION.set(str(correlation_id))


def story_context() -> dict[str, str]:
    """Current bound context (empty strings when unbound). Never raises."""
    try:
        return {
            "tenant_id": _CTX_TENANT.get(),
            "investigation_id": _CTX_INVESTIGATION.get(),
            "correlation_id": _CTX_CORRELATION.get(),
        }
    except Exception:
        return {"tenant_id": "", "investigation_id": "", "correlation_id": ""}


def clear_story_context() -> None:
    """Unbind identity (tests, task teardown)."""
    _CTX_TENANT.set("")
    _CTX_INVESTIGATION.set("")
    _CTX_CORRELATION.set("")
