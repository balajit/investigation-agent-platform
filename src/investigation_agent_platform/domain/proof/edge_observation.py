# src/investigation_agent_platform/domain/proof/edge_observation.py
"""D1 typed call-edge observation record (``edge/v1``) — WP-D1, frozen.

A new, separate record — never a reinterpretation of existing evidence
rows. The envelope is tagged; the tag is the status (no separate
``status`` field, so variant/status drift is unrepresentable).
``observation_id`` is the content hash of the canonical preimage
(never self-referential): schema validation + recomputation precede
availability on every read.

Normative source: ``docs/IAP-proof-kernel-d1-spec-v1.2.md`` §§0, 2, 4.
No persistence shape is created here (§9 decision 1 pending); no
verifier consumes these records (D1-H; gated by ``IAP_TYPED_EDGES``).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from investigation_agent_platform.domain.proof.canonical import (
    CanonicalizationError,
    canonical_dict_bytes,
    normalize_path,
)

EDGE_RECORD_VERSION = "edge/v1"
EDGE_DOMAIN = "iap:call-edge"

ASSUMPTIONS_V1 = frozenset({"static-model-only", "no-execution-claim"})
"""Closed assumption vocabulary (spec §2.6): each narrows what a discharge
proves; none authorizes discharge."""

_MISSING_CLOSED = frozenset({"caller", "callee", "call_site", "target_declaration"})

_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class EdgeErrorCode(str, Enum):
    """Stable edge-record failure codes (frozen; append-only)."""

    BAD_RANGE = "BAD_RANGE"
    BAD_PATH = "BAD_PATH"
    BAD_REVISION = "BAD_REVISION"
    BAD_REPOSITORY = "BAD_REPOSITORY"
    REPO_MISMATCH = "REPO_MISMATCH"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    DUPLICATE_SOURCE = "DUPLICATE_SOURCE"
    UNKNOWN_ASSUMPTION = "UNKNOWN_ASSUMPTION"
    UNKNOWN_LIMITATION = "UNKNOWN_LIMITATION"
    UNKNOWN_MODEL = "UNKNOWN_MODEL"
    HASH_MISMATCH = "HASH_MISMATCH"
    NOT_RESOLVED = "NOT_RESOLVED"


class EdgeError(ValueError):
    """Fail-closed edge-record rejection carrying a stable code."""

    def __init__(self, code: EdgeErrorCode | str, detail: str = "") -> None:
        self.code = str(getattr(code, "value", code))
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class EdgeVariant(str, Enum):
    RESOLVED_EDGE = "RESOLVED_EDGE"
    PARTIAL_OBSERVATION = "PARTIAL_OBSERVATION"
    UNRESOLVED_OBSERVATION = "UNRESOLVED_OBSERVATION"


class LimitationCategory(str, Enum):
    MISSING_INPUT = "MISSING_INPUT"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    UNSUPPORTED_CONSTRUCT = "UNSUPPORTED_CONSTRUCT"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    INCOMPATIBLE_CONTEXT = "INCOMPATIBLE_CONTEXT"


class LimitationCode(str, Enum):
    """Closed limitation code set (``limitation/v1`` initial).

    Codes named in the spec: AMBIGUOUS_TARGET, MISSING_INPUT ("not
    found" / INPUT_NOT_FOUND, "not yet resolved" / NOT_YET_RESOLVED).
    One representative code per remaining category rounds out v1.
    """

    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    MISSING_INPUT = "MISSING_INPUT"
    INPUT_NOT_FOUND = "INPUT_NOT_FOUND"
    NOT_YET_RESOLVED = "NOT_YET_RESOLVED"
    UNSUPPORTED_CONSTRUCT = "UNSUPPORTED_CONSTRUCT"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    INCOMPATIBLE_CONTEXT = "INCOMPATIBLE_CONTEXT"


_LIMITATION_META: dict[LimitationCode, tuple[LimitationCategory, bool, str]] = {
    # code -> (category, retry meaningful, downstream scope)
    LimitationCode.AMBIGUOUS_TARGET: (
        LimitationCategory.AMBIGUOUS_TARGET,
        False,
        "invalidates_use",
    ),
    LimitationCode.MISSING_INPUT: (LimitationCategory.MISSING_INPUT, True, "invalidates_use"),
    LimitationCode.INPUT_NOT_FOUND: (LimitationCategory.MISSING_INPUT, False, "invalidates_use"),
    LimitationCode.NOT_YET_RESOLVED: (LimitationCategory.MISSING_INPUT, True, "invalidates_use"),
    LimitationCode.UNSUPPORTED_CONSTRUCT: (
        LimitationCategory.UNSUPPORTED_CONSTRUCT,
        False,
        "invalidates_use",
    ),
    LimitationCode.TRANSIENT_FAILURE: (
        LimitationCategory.TRANSIENT_FAILURE,
        True,
        "invalidates_use",
    ),
    LimitationCode.INCOMPATIBLE_CONTEXT: (
        LimitationCategory.INCOMPATIBLE_CONTEXT,
        False,
        "invalidates_use",
    ),
}
"""Downstream scope (spec §4.2) is stored per code, not narrated: every
v1 code is ``invalidates_use`` — no v1 code authorizes premise use
(D1-F). ``telemetry_only`` is reserved for future codes and never
silently assigned."""


def limitation_category(code: LimitationCode | str) -> LimitationCategory:
    """Category for a code; unknown codes fail closed (never defaulted)."""
    try:
        return _LIMITATION_META[LimitationCode(code)][0]
    except (KeyError, ValueError):
        raise EdgeError(EdgeErrorCode.UNKNOWN_LIMITATION, f"code={code!r}") from None


def limitation_retryable(code: LimitationCode | str) -> bool:
    """Whether retry with changed inputs could resolve this limitation."""
    try:
        return _LIMITATION_META[LimitationCode(code)][1]
    except (KeyError, ValueError):
        raise EdgeError(EdgeErrorCode.UNKNOWN_LIMITATION, f"code={code!r}") from None


def limitation_downstream(code: LimitationCode | str) -> str:
    """Downstream scope for a code (spec §4.2): ``invalidates_use`` vs
    ``telemetry_only``. Unknown codes fail closed (never defaulted)."""
    try:
        return _LIMITATION_META[LimitationCode(code)][2]
    except (KeyError, ValueError):
        raise EdgeError(EdgeErrorCode.UNKNOWN_LIMITATION, f"code={code!r}") from None


def _check_range(start: int, end: int, where: str) -> None:
    """1-indexed positions, exclusive ends; reversed or empty fail closed."""
    if not isinstance(start, int) or not isinstance(end, int):
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: non-integer bounds")
    if isinstance(start, bool) or isinstance(end, bool):
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: boolean bounds")
    if start < 1 or end < 1:
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: 1-indexed, got [{start}, {end})")
    if end <= start:
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: empty or reversed [{start}, {end})")


def _check_revision(revision: str, where: str) -> str:
    """Immutable commit identity only: full SHA-1/SHA-256 hex.

    Branch names, mutable tags, HEAD, and UNPINNED fail closed. Short
    SHAs are rejected: abbreviation is a lookup convenience, not an
    immutable identity for a provenance binding.
    """
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise EdgeError(EdgeErrorCode.BAD_REVISION, f"{where}: {revision!r}")
    return revision


def _check_path(path: str, where: str) -> str:
    """Repo rule: strip leading ./, collapse //, strip trailing /, case
    preserved, backslash rejected. Returns the normalized form (two
    representations hash identically exactly here)."""
    try:
        return normalize_path(path)
    except (CanonicalizationError, ValueError, TypeError) as exc:
        raise EdgeError(EdgeErrorCode.BAD_PATH, f"{where}: {exc}") from None


def _check_nfc_text(value: str, where: str) -> str:
    if not isinstance(value, str):
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: non-string")
    if value != unicodedata.normalize("NFC", value):
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: not NFC-normalized")
    return value


class BoundSymbol(BaseModel):
    """Fully bound endpoint: all fields required (no unknowns)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=512)
    declaring_type: str = Field(..., min_length=1, max_length=512)
    file_path: str = Field(..., min_length=1, max_length=1024)
    revision: str = Field(..., min_length=1, max_length=128)

    @field_validator("file_path")
    @classmethod
    def _norm_path(cls, v: str) -> str:
        return _check_path(v, "symbol.file_path")

    @field_validator("revision")
    @classmethod
    def _pin_revision(cls, v: str) -> str:
        return _check_revision(v, "symbol.revision")


class ExpressionRange(BaseModel):
    """Call-expression range: lexicographic exclusive end (columns)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_line: StrictInt = Field(...)
    start_col: StrictInt = Field(...)
    end_line: StrictInt = Field(...)
    end_col: StrictInt = Field(...)

    @field_validator("start_col", "end_col")
    @classmethod
    def _cols(cls, v: int) -> int:
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise EdgeError(EdgeErrorCode.BAD_RANGE, f"column: {v!r}")
        return v

    def model_post_init(self, _context: Any) -> None:
        for name in ("start_line", "end_line"):
            value = getattr(self, name)
            if isinstance(value, bool):
                raise EdgeError(EdgeErrorCode.BAD_RANGE, f"expression_range: boolean {name}")
        if self.start_line < 1 or self.end_line < 1:
            raise EdgeError(EdgeErrorCode.BAD_RANGE, "expression_range: 1-indexed lines")
        if (self.end_line, self.end_col) <= (self.start_line, self.start_col):
            raise EdgeError(
                EdgeErrorCode.BAD_RANGE,
                f"expression_range reversed/empty: {self.start_line}:{self.start_col}-"
                f"{self.end_line}:{self.end_col}",
            )


class CallSite(BaseModel):
    """Bound call site: line-span outer span + expression range."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str = Field(..., min_length=1, max_length=1024)
    revision: str = Field(..., min_length=1, max_length=128)
    start_line: StrictInt = Field(...)
    end_line: StrictInt = Field(...)
    expression_range: ExpressionRange = Field(...)
    window_text: str = Field(..., max_length=65536)
    syntax_kind: str = Field(..., min_length=1, max_length=128)

    @field_validator("file_path")
    @classmethod
    def _norm_path(cls, v: str) -> str:
        return _check_path(v, "call_site.file_path")

    @field_validator("revision")
    @classmethod
    def _pin_revision(cls, v: str) -> str:
        return _check_revision(v, "call_site.revision")

    @field_validator("window_text")
    @classmethod
    def _nfc_window(cls, v: str) -> str:
        return _check_nfc_text(v, "call_site.window_text")

    def model_post_init(self, _context: Any) -> None:
        _check_range(self.start_line, self.end_line, "call_site lines")
        pos = self.expression_range
        if (pos.end_line, pos.end_col) <= (pos.start_line, pos.start_col):
            raise EdgeError(
                EdgeErrorCode.BAD_RANGE,
                f"expression_range reversed/empty: {pos.start_line}:{pos.start_col}-"
                f"{pos.end_line}:{pos.end_col}",
            )
        if pos.start_line < 1 or pos.end_line < 1:
            raise EdgeError(EdgeErrorCode.BAD_RANGE, "expression_range: 1-indexed lines")


class TargetDeclaration(BaseModel):
    """Resolved declaration site (pure line span, no columns)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str = Field(..., min_length=1, max_length=1024)
    revision: str = Field(..., min_length=1, max_length=128)
    start_line: StrictInt = Field(...)
    end_line: StrictInt = Field(...)

    @field_validator("file_path")
    @classmethod
    def _norm_path(cls, v: str) -> str:
        return _check_path(v, "target_declaration.file_path")

    @field_validator("revision")
    @classmethod
    def _pin_revision(cls, v: str) -> str:
        return _check_revision(v, "target_declaration.revision")

    def model_post_init(self, _context: Any) -> None:
        _check_range(self.start_line, self.end_line, "target_declaration lines")


class RepositoryId(BaseModel):
    """Owning repository identity (immutable revision required)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=256)
    revision: str = Field(..., min_length=1, max_length=128)
    build_identity: str | None = Field(default=None, max_length=256)

    @field_validator("revision")
    @classmethod
    def _pin_revision(cls, v: str) -> str:
        return _check_revision(v, "repository.revision")


class ResolverId(BaseModel):
    """Three-identity resolver binding (spec §4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle: str = Field(..., min_length=1, max_length=128)
    bundle_version: str = Field(..., min_length=1, max_length=64)
    contract_digest: str = Field(..., min_length=1, max_length=256)
    artifact_digest: str = Field(..., min_length=1, max_length=256)
    parser_version: str = Field(..., min_length=1, max_length=64)
    language: str = Field(..., min_length=1, max_length=64)
    language_version: str = Field(..., min_length=1, max_length=64)
    analysis_model: str = Field(..., min_length=1, max_length=128)


class LineageSource(BaseModel):
    """One consumed source: full-tuple identity, reader-convenience extras."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(..., min_length=1, max_length=256)
    evidence_digest: str = Field(..., min_length=1, max_length=256)
    artifact_digest: str = Field(..., min_length=1, max_length=256)
    scope: str = Field(..., min_length=1, max_length=256)
    revision: str = Field(..., min_length=1, max_length=128)
    repository: RepositoryId = Field(...)

    @field_validator("revision")
    @classmethod
    def _pin_revision(cls, v: str) -> str:
        return _check_revision(v, "lineage.source.revision")


class Lineage(BaseModel):
    """Provenance binding: entry identity excludes scope (consumer view)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources: list[LineageSource] = Field(..., min_length=1, max_length=256)
    analyzer_run_id: str = Field(..., min_length=1, max_length=256)
    execution_identity: str = Field(..., min_length=1, max_length=256)


class RangeReference(BaseModel):
    """UNRESOLVED observed range (line-span semantics)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str = Field(..., min_length=1, max_length=1024)
    revision: str = Field(..., min_length=1, max_length=128)
    start_line: StrictInt = Field(...)
    end_line: StrictInt = Field(...)

    @field_validator("file_path")
    @classmethod
    def _norm_path(cls, v: str) -> str:
        return _check_path(v, "observed_ref.file_path")

    @field_validator("revision")
    @classmethod
    def _pin_revision(cls, v: str) -> str:
        return _check_revision(v, "observed_ref.revision")

    def model_post_init(self, _context: Any) -> None:
        _check_range(self.start_line, self.end_line, "observed_ref lines")


class SymbolReference(BaseModel):
    """UNRESOLVED observed symbol (omitted means unknown, never inapplicable)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=512)
    declaring_type: str | None = Field(default=None, max_length=512)
    file_path: str | None = Field(default=None, max_length=1024)
    revision: str = Field(..., min_length=1, max_length=128)

    @field_validator("file_path")
    @classmethod
    def _norm_path(cls, v: str | None) -> str | None:
        return None if v is None else _check_path(v, "observed_ref.file_path")

    @field_validator("revision")
    @classmethod
    def _pin_revision(cls, v: str) -> str:
        return _check_revision(v, "observed_ref.revision")


class ResolvedEdgePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: BoundSymbol = Field(...)
    callee: BoundSymbol = Field(...)
    call_site: CallSite = Field(...)
    target_declaration: TargetDeclaration | None = Field(default=None)
    repository: RepositoryId = Field(...)
    resolver: ResolverId = Field(...)
    limitation_codes: list[LimitationCode] = Field(default_factory=list, max_length=16)
    lineage: Lineage = Field(...)
    assumptions: list[str] = Field(default_factory=list, max_length=16)


class PartialObservationPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: BoundSymbol | None = Field(default=None)
    callee: BoundSymbol | None = Field(default=None)
    call_site: CallSite | None = Field(default=None)
    target_declaration: TargetDeclaration | None = Field(default=None)
    limitation_codes: list[LimitationCode] = Field(..., min_length=1, max_length=16)
    missing: list[str] = Field(..., min_length=1, max_length=8)
    repository: RepositoryId = Field(...)
    resolver: ResolverId = Field(...)
    lineage: Lineage = Field(...)
    assumptions: list[str] = Field(default_factory=list, max_length=16)


class UnresolvedObservationPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_ref: RangeReference | SymbolReference = Field(...)
    limitation_codes: list[LimitationCode] = Field(..., min_length=1, max_length=16)
    repository: RepositoryId = Field(...)
    resolver: ResolverId = Field(...)
    lineage: Lineage = Field(...)
    assumptions: list[str] = Field(default_factory=list, max_length=16)


class EdgeObservation(BaseModel):
    """Tagged envelope: the tag IS the status (no separate status field)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(..., min_length=1, max_length=256)
    variant: EdgeVariant = Field(...)
    record_version: str = Field(default=EDGE_RECORD_VERSION, max_length=32)
    payload: ResolvedEdgePayload | PartialObservationPayload | UnresolvedObservationPayload = Field(
        ...
    )


def _payload_dict(payload: BaseModel) -> dict[str, Any]:
    """JSON-scalar payload with absent optionals omitted (never nulled).

    Sole exception: an explicit ``target_declaration: null`` is preserved
    — it is a meaningful exemption claim (§2.5), not an absent optional.
    Set-valued fields are sorted (assumptions, missing); lineage source
    order is significant (consumption order) and preserved.
    Lineage ``scope`` is excluded from identity bytes (§2.3): scope is
    the consumer's view of identical bytes, so the same bytes under two
    scopes hash identically — listing both is caught as a duplicate by
    the scope-free entry key. Scope is still stored on the record
    (reader convenience, never authoritative).
    """
    raw = payload.model_dump(mode="json")
    kept: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None and key != "target_declaration":
            continue
        kept[key] = value
    if isinstance(kept.get("assumptions"), list):
        kept["assumptions"] = sorted(kept["assumptions"])
    if isinstance(kept.get("missing"), list):
        kept["missing"] = sorted(kept["missing"])
    lineage = kept.get("lineage")
    if isinstance(lineage, dict) and isinstance(lineage.get("sources"), list):
        lineage["sources"] = [
            {k: v for k, v in src.items() if k != "scope"} for src in lineage["sources"]
        ]
    return kept


def _check_assumptions(assumptions: list[str]) -> None:
    unknown = [a for a in assumptions if a not in ASSUMPTIONS_V1]
    if unknown:
        raise EdgeError(EdgeErrorCode.UNKNOWN_ASSUMPTION, f"{unknown!r}")
    if len(set(assumptions)) != len(assumptions):
        raise EdgeError(EdgeErrorCode.UNKNOWN_ASSUMPTION, "duplicate assumptions")


def _check_lineage(payload: BaseModel, repository: RepositoryId) -> None:
    """Full-tuple identity, repo consistency, reader-convenience equalities."""
    lineage = getattr(payload, "lineage", None)
    if lineage is None:
        return
    seen: set[tuple[str, ...]] = set()
    for src in lineage.sources:
        key = (
            src.evidence_id,
            src.evidence_digest,
            src.artifact_digest,
            src.repository.name,
            src.repository.revision,
        )
        if key in seen:
            raise EdgeError(EdgeErrorCode.DUPLICATE_SOURCE, f"{src.evidence_id!r}")
        seen.add(key)
        if src.revision != src.repository.revision:
            raise EdgeError(
                EdgeErrorCode.REVISION_MISMATCH,
                f"entry revision {src.revision!r} != repository revision "
                f"{src.repository.revision!r}",
            )
        if (
            src.repository.name == repository.name
            and src.repository.revision != repository.revision
        ):
            raise EdgeError(
                EdgeErrorCode.REPO_MISMATCH,
                f"entry repo {src.repository.name}@{src.repository.revision} "
                f"vs observation repo {repository.name}@{repository.revision}",
            )
    repos = {(s.repository.name, s.repository.revision) for s in lineage.sources}
    if len(repos) == 1:
        (name, revision) = next(iter(repos))
        if name != repository.name or revision != repository.revision:
            raise EdgeError(
                EdgeErrorCode.REPO_MISMATCH,
                "single-repo lineage must equal the observation-level repository",
            )


def _check_variant(payload: BaseModel, variant: EdgeVariant) -> None:
    if variant == EdgeVariant.RESOLVED_EDGE:
        assert isinstance(payload, ResolvedEdgePayload)
        if payload.limitation_codes:
            raise EdgeError(EdgeErrorCode.NOT_RESOLVED, "RESOLVED_EDGE with limitation codes")
    else:
        codes = getattr(payload, "limitation_codes", [])
        if not codes:
            raise EdgeError(EdgeErrorCode.NOT_RESOLVED, f"{variant.value} without limitation codes")
        for code in codes:
            limitation_category(code)
        if variant == EdgeVariant.PARTIAL_OBSERVATION:
            assert isinstance(payload, PartialObservationPayload)
            unknown = [m for m in payload.missing if m not in _MISSING_CLOSED]
            if unknown:
                raise EdgeError(
                    EdgeErrorCode.NOT_RESOLVED, f"missing outside closed list: {unknown!r}"
                )
            bound = {
                "caller": payload.caller is not None,
                "callee": payload.callee is not None,
                "call_site": payload.call_site is not None,
                "target_declaration": payload.target_declaration is not None,
            }
            for part in payload.missing:
                if bound[part]:
                    raise EdgeError(
                        EdgeErrorCode.NOT_RESOLVED,
                        f"missing lists bound part: {part}",
                    )
    _check_assumptions(list(payload.assumptions))
    _check_lineage(payload, payload.repository)


def compute_observation_id(
    variant: EdgeVariant,
    payload: ResolvedEdgePayload | PartialObservationPayload | UnresolvedObservationPayload,
) -> str:
    """Content hash over the canonical preimage (never self-referential).

    Preimage: ``{"domain", "record_version", "variant", <payload>}``;
    byte layout mirrors ``canon/v1`` (sorted keys, compact, UTF-8, NFC,
    float rejection). Two representations hash identically only when the
    contract declares them equivalent (path normalization).
    """
    _check_variant(payload, variant)
    preimage = {
        "domain": EDGE_DOMAIN,
        "record_version": EDGE_RECORD_VERSION,
        "variant": variant.value,
        **_payload_dict(payload),
    }
    raw = canonical_dict_bytes(preimage)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def make_observation(
    variant: EdgeVariant,
    payload: ResolvedEdgePayload | PartialObservationPayload | UnresolvedObservationPayload,
) -> EdgeObservation:
    """Build a well-formed observation (id computed, never supplied)."""
    return EdgeObservation(
        observation_id=compute_observation_id(variant, payload),
        variant=variant,
        record_version=EDGE_RECORD_VERSION,
        payload=payload,
    )


def verify_observation(data: dict[str, Any]) -> EdgeObservation:
    """Parse + re-verify a stored record.

    Schema validation and ``observation_id`` recomputation precede
    availability: a malformed or hash-mismatched record becomes typed
    unavailability (EdgeError), never repaired, never normalized into a
    different record, never passed to the kernel.
    """
    if not isinstance(data, dict):
        raise EdgeError(EdgeErrorCode.HASH_MISMATCH, "record must be a mapping")
    if data.get("record_version", EDGE_RECORD_VERSION) != EDGE_RECORD_VERSION:
        raise EdgeError(
            EdgeErrorCode.HASH_MISMATCH, f"record_version={data.get('record_version')!r}"
        )
    try:
        variant = EdgeVariant(data.get("variant"))
    except ValueError:
        raise EdgeError(EdgeErrorCode.HASH_MISMATCH, f"variant={data.get('variant')!r}") from None
    payload_data = data.get("payload")
    if not isinstance(payload_data, dict):
        raise EdgeError(EdgeErrorCode.HASH_MISMATCH, "payload must be a mapping")
    payload_cls = {
        EdgeVariant.RESOLVED_EDGE: ResolvedEdgePayload,
        EdgeVariant.PARTIAL_OBSERVATION: PartialObservationPayload,
        EdgeVariant.UNRESOLVED_OBSERVATION: UnresolvedObservationPayload,
    }[variant]
    try:
        payload = payload_cls.model_validate(payload_data)
    except Exception as exc:
        raise EdgeError(EdgeErrorCode.HASH_MISMATCH, f"payload invalid: {exc}") from None
    _check_variant(payload, variant)
    expected = compute_observation_id(variant, payload)
    if data.get("observation_id") != expected:
        raise EdgeError(
            EdgeErrorCode.HASH_MISMATCH,
            f"id {data.get('observation_id')!r} != recomputed {expected!r}",
        )
    return EdgeObservation(
        observation_id=expected,
        variant=variant,
        record_version=EDGE_RECORD_VERSION,
        payload=payload,
    )


class SyntaxForm(str, Enum):
    """What the parser actually identified (resolver-gate input)."""

    CALL_EXPRESSION = "call_expression"
    COMMENT = "comment"
    STRING_LITERAL = "string_literal"
    IMPORT_STATEMENT = "import_statement"
    NAME_MENTION = "name_mention"


class CallerBinding(str, Enum):
    """How the caller was bound (parser scope only; never name match)."""

    PARSER_SCOPE = "parser_scope"
    NAME_MATCH = "name_match"
    UNBOUND = "unbound"


class Adjudication(BaseModel):
    """Resolver-gate decision over structured resolver outputs.

    Decides RESOLVED vs PARTIAL vs UNRESOLVED from the §2.2 triple
    (syntax + binding + target) — never from text match. Comments,
    string literals, imports, and bare name mentions never satisfy the
    syntax element; name-match binding never satisfies the caller
    element. Rejection here is a resolver-gate rejection (D1-B); the
    checker independently rejects malformed records (D1-F).
    """

    model_config = ConfigDict(frozen=True)

    variant: EdgeVariant = Field(...)
    limitation_codes: list[LimitationCode] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


def adjudicate_resolution(
    syntax_form: SyntaxForm,
    syntax_kind_present: bool,
    caller_binding: CallerBinding,
    target_resolved: bool,
    target_candidates: int = 0,
) -> Adjudication:
    """Apply the §2.2 triple to structured resolver outputs (pure).

    Malformed adjudication inputs fail closed: a negative candidate
    count is never a "zero candidates" result.
    """
    if (
        not isinstance(target_candidates, int)
        or isinstance(target_candidates, bool)
        or target_candidates < 0
    ):
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"target_candidates: {target_candidates!r}")
    if syntax_form != SyntaxForm.CALL_EXPRESSION or not syntax_kind_present:
        return Adjudication(
            variant=EdgeVariant.UNRESOLVED_OBSERVATION,
            limitation_codes=[LimitationCode.MISSING_INPUT],
        )
    if caller_binding != CallerBinding.PARSER_SCOPE:
        return Adjudication(
            variant=EdgeVariant.PARTIAL_OBSERVATION,
            limitation_codes=[LimitationCode.MISSING_INPUT],
            missing=["caller"],
        )
    if not target_resolved:
        if target_candidates > 1:
            codes = [LimitationCode.AMBIGUOUS_TARGET]
        elif target_candidates == 0:
            codes = [LimitationCode.MISSING_INPUT]
        else:
            codes = [LimitationCode.NOT_YET_RESOLVED]
        return Adjudication(
            variant=EdgeVariant.PARTIAL_OBSERVATION,
            limitation_codes=codes,
            missing=["callee"],
        )
    return Adjudication(variant=EdgeVariant.RESOLVED_EDGE)
