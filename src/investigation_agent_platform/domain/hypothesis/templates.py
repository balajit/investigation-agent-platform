# src/investigation_agent_platform/domain/hypothesis/templates.py
"""S2 dormant-activation templates (DES-S2-dormant-templates v1.0).

For each suspect path, S2 instantiates dormant-activation templates
alongside mechanism hypotheses. Each template names its discriminating
fetch at birth; every branch/guard on the path renders as an explicit,
checkable, cost-ordered assumption priced against the spread budget.
Templates that resolve beyond tenant repos terminate at template time as
named-handoff candidates — never as fetches.

Pure domain: no I/O, no providers. Callers (code-layer analysis, P6
frontier) supply the classified suspect-path sites; this module only
decides which templates activate and what each one obligates.

``classify_banked_sites`` is the conservative entry point over banked
evidence rows: it admits only pinned SOURCE_CODE range reads and maps a
small explicit table of range-read syntax onto site kinds. Anything
ambiguous stays OTHER and activates nothing — site kinds are never
inferred from free-form model prose. Tree-sitter live-checkout scans
are deliberately not an input: ``find_symbol`` accepts a revision
argument but resolves against the live checkout, so its rows cannot
prove pinned source identity.
"""

import re
from collections.abc import Callable
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Version pin for every template this module emits. In-flight rows keep
#: the semantics they started with; unmarked rows resolve to current.
TEMPLATES_VERSION_CURRENT = "templates-v1"

#: Field caps for assembled multi-site strings. The join helper guarantees
#: these bounds; the Field max_length is the fail-closed backstop.
TARGET_MAX_LENGTH = 1024
LOCALIZING_EVIDENCE_MAX_LENGTH = 2048


class TemplateKind(StrEnum):
    """The four dormant-activation template families (DES-S2 section 1)."""

    FLAG_GATED = "FLAG_GATED"
    DATA_GATED = "DATA_GATED"
    CONTRACT_DRIFT = "CONTRACT_DRIFT"
    REGIME_CROSSING = "REGIME_CROSSING"


class SuspectSiteKind(StrEnum):
    """Code-layer classification of one site on a suspect path."""

    FLAG_GATE = "FLAG_GATE"
    BRANCH_MATCH = "BRANCH_MATCH"
    EXTERNAL_CALL = "EXTERNAL_CALL"
    LOOP_POOL_RETRY = "LOOP_POOL_RETRY"
    OTHER = "OTHER"


class DiscriminatingFetchKind(StrEnum):
    """The one fetch that would discriminate each template family."""

    GATE_STATE_T_VS_G = "GATE_STATE_T_VS_G"
    FIRST_SEEN_PAYLOAD_SHAPE = "FIRST_SEEN_PAYLOAD_SHAPE"
    LIVE_RESPONSE_VS_ASSUMED_SHAPE = "LIVE_RESPONSE_VS_ASSUMED_SHAPE"
    TREND_BREAK_AT_T = "TREND_BREAK_AT_T"


#: Site kind -> template family. OTHER sites activate nothing.
SITE_KIND_TO_TEMPLATE: dict[SuspectSiteKind, TemplateKind] = {
    SuspectSiteKind.FLAG_GATE: TemplateKind.FLAG_GATED,
    SuspectSiteKind.BRANCH_MATCH: TemplateKind.DATA_GATED,
    SuspectSiteKind.EXTERNAL_CALL: TemplateKind.CONTRACT_DRIFT,
    SuspectSiteKind.LOOP_POOL_RETRY: TemplateKind.REGIME_CROSSING,
}

#: Template family -> its birth discriminating fetch.
TEMPLATE_DISCRIMINATOR: dict[TemplateKind, DiscriminatingFetchKind] = {
    TemplateKind.FLAG_GATED: DiscriminatingFetchKind.GATE_STATE_T_VS_G,
    TemplateKind.DATA_GATED: DiscriminatingFetchKind.FIRST_SEEN_PAYLOAD_SHAPE,
    TemplateKind.CONTRACT_DRIFT: DiscriminatingFetchKind.LIVE_RESPONSE_VS_ASSUMED_SHAPE,
    TemplateKind.REGIME_CROSSING: DiscriminatingFetchKind.TREND_BREAK_AT_T,
}


class SuspectSite(BaseModel):
    """One classified site on a suspect path, supplied by code-layer analysis."""

    model_config = ConfigDict(frozen=True)

    node_id: UUID
    kind: SuspectSiteKind
    file_path: str = Field(..., min_length=1, max_length=1024)
    line: int = Field(..., ge=1)
    detail: str = Field(default="", max_length=1024)
    #: True when the site resolves beyond tenant repos (ADR-BOUNDARY-001).
    cross_boundary: bool = False
    #: Upstream identity for cross-boundary sites; required when set.
    upstream_identity: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _require_upstream_identity(self) -> "SuspectSite":
        if self.cross_boundary and not self.upstream_identity:
            raise ValueError("cross-boundary sites require upstream_identity")
        return self


class SuspectPath(BaseModel):
    """One suspect path: ordered classified sites at a pinned revision."""

    model_config = ConfigDict(frozen=True)

    path_id: UUID = Field(default_factory=uuid4)
    repository: str = Field(..., min_length=1, max_length=256)
    revision: str = Field(..., min_length=1, max_length=128)
    sites: list[SuspectSite] = Field(..., min_length=1, max_length=200)

    @model_validator(mode="after")
    def _require_pinned_revision(self) -> "SuspectPath":
        # Templates inherit source identity from the path: an unpinned
        # revision would let every downstream fetch float. Same fail-closed
        # set as CodeFactResolutionV2.
        if self.revision in {"HEAD", "UNPINNED"}:
            raise ValueError("suspect paths require a pinned revision, never HEAD/UNPINNED")
        return self


class DiscriminatingFetch(BaseModel):
    """The fetch named at template birth; the cheapest observation that
    would discriminate this template family on this path."""

    model_config = ConfigDict(frozen=True)

    fetch_kind: DiscriminatingFetchKind
    target: str = Field(..., min_length=1, max_length=TARGET_MAX_LENGTH)
    window_note: str = Field(default="", max_length=512)


class TemplateAssumption(BaseModel):
    """One checkable assumption: a branch/guard expectation plus its price
    in spread-budget units. ``check_order`` is cost order, ascending."""

    model_config = ConfigDict(frozen=True)

    expects: str = Field(..., min_length=1, max_length=1024)
    check_order: int = Field(..., ge=0)
    cost_estimate: int = Field(..., ge=0)


class HandoffCandidate(BaseModel):
    """A cross-boundary template terminated at template time: a named
    handoff with upstream identity plus localizing evidence. Never a fetch."""

    model_config = ConfigDict(frozen=True)

    upstream_identity: str = Field(..., min_length=1, max_length=512)
    localizing_evidence: str = Field(..., min_length=1, max_length=LOCALIZING_EVIDENCE_MAX_LENGTH)
    reason: str = Field(..., min_length=1, max_length=1024)


class DormantTemplate(BaseModel):
    """One activated dormant template: family, birth fetch, cost-ordered
    assumptions. Population rule: exactly one of ``discriminating_fetch``
    (in-tenant) or ``handoff_candidate`` (cross-boundary) is set."""

    model_config = ConfigDict(frozen=True)

    template_id: UUID = Field(default_factory=uuid4)
    version: str = Field(default=TEMPLATES_VERSION_CURRENT, max_length=16)
    kind: TemplateKind
    suspect_path_id: UUID
    site_node_ids: list[UUID] = Field(..., min_length=1, max_length=200)
    discriminating_fetch: DiscriminatingFetch | None = None
    assumptions: list[TemplateAssumption] = Field(default_factory=list, max_length=50)
    handoff_candidate: HandoffCandidate | None = None

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> "DormantTemplate":
        if (self.discriminating_fetch is None) == (self.handoff_candidate is None):
            raise ValueError("exactly one of discriminating_fetch / handoff_candidate is required")
        if self.handoff_candidate is not None and self.assumptions:
            raise ValueError("handoff candidates carry no assumptions, only localizing evidence")
        orders = [a.check_order for a in self.assumptions]
        if orders != sorted(orders):
            raise ValueError("assumptions must be cost-ordered ascending")
        return self

    @property
    def total_cost(self) -> int:
        """Sum of assumption prices in spread-budget units (0 for handoffs)."""
        return sum(a.cost_estimate for a in self.assumptions)

    def priced_within(self, spread_budget: int) -> bool:
        """True when the whole assumption list fits the spread budget."""
        return self.total_cost <= spread_budget


def instantiate_templates(path: SuspectPath) -> list[DormantTemplate]:
    """Activate dormant templates for one suspect path.

    One template per site-kind family present among in-tenant sites; each
    names its discriminating fetch at birth with cost-ordered assumptions.
    Cross-boundary sites group by upstream identity into handoff candidates
    that terminate at template time — they never produce fetches.
    """
    templates: list[DormantTemplate] = []
    in_tenant: dict[TemplateKind, list[SuspectSite]] = {}
    cross_boundary: dict[str, list[SuspectSite]] = {}
    for site in path.sites:
        if site.cross_boundary:
            # Validated non-empty by SuspectSite.
            cross_boundary.setdefault(site.upstream_identity or "", []).append(site)
            continue
        kind = SITE_KIND_TO_TEMPLATE.get(site.kind)
        if kind is None:
            continue
        in_tenant.setdefault(kind, []).append(site)
    for kind, sites in in_tenant.items():
        fetch_kind = TEMPLATE_DISCRIMINATOR[kind]
        target = _bounded_join([_site_segment(s) for s in sites], TARGET_MAX_LENGTH)
        assumptions = _assumptions_for(kind, sites)
        templates.append(
            DormantTemplate(
                kind=kind,
                suspect_path_id=path.path_id,
                site_node_ids=[s.node_id for s in sites],
                discriminating_fetch=DiscriminatingFetch(
                    fetch_kind=fetch_kind,
                    target=target,
                    window_note="gate/payload/trend discriminator spans onset T and last-good G",
                ),
                assumptions=assumptions,
            )
        )
    for upstream, sites in cross_boundary.items():
        kind = _dominant_kind(sites)
        if kind is None:
            # All-OTHER group: no template family applies, so nothing
            # activates — same rule as in-tenant OTHER sites.
            continue
        localizing = _bounded_join(
            [_site_segment(s) for s in sites], LOCALIZING_EVIDENCE_MAX_LENGTH
        )
        templates.append(
            DormantTemplate(
                kind=kind,
                suspect_path_id=path.path_id,
                site_node_ids=[s.node_id for s in sites],
                handoff_candidate=HandoffCandidate(
                    upstream_identity=upstream,
                    localizing_evidence=localizing,
                    reason=(
                        "template resolves beyond tenant repos; terminates at "
                        "template time per ADR-BOUNDARY-001, never fetched"
                    ),
                ),
            )
        )
    return templates


def _site_segment(site: SuspectSite) -> str:
    """Render one site as ``file:line (detail)``."""
    segment = f"{site.file_path}:{site.line}"
    if site.detail:
        segment += f" ({site.detail})"
    return segment


def _bounded_join(segments: list[str], limit: int) -> str:
    """Join site segments deterministically within ``limit`` characters.

    Whole leading segments are kept in input order; dropped trailing
    segments collapse into an explicit ``… (+K more sites)`` suffix so
    omitted sites are counted, never silently lost. The result always fits.
    """
    full = "; ".join(segments)
    if len(full) <= limit:
        return full
    for keep in range(len(segments) - 1, -1, -1):
        omitted = len(segments) - keep
        suffix = f"… (+{omitted} more site{'s' if omitted != 1 else ''})"
        prefix = "; ".join(segments[:keep])
        candidate = f"{prefix}; {suffix}" if prefix else suffix
        if len(candidate) <= limit:
            return candidate
    # Unreachable: the bare suffix is far shorter than any real limit, but
    # truncate defensively so the field constraint always holds.
    return f"… (+{len(segments)} more sites)"[:limit]


def _assumptions_for(kind: TemplateKind, sites: list[SuspectSite]) -> list[TemplateAssumption]:
    """Render every branch/guard as an explicit checkable assumption,
    cheapest check first. Costs are relative spread-budget units."""
    if kind == TemplateKind.FLAG_GATED:
        raw = [
            ("flag gate controls the suspect path (expects gate readable at T)", 1),
            ("gate state at T differs from gate state at last-good G", 2),
            ("no second gate downstream re-decides the path", 3),
        ]
    elif kind == TemplateKind.DATA_GATED:
        raw = [
            ("branch/match site enumerates all live payload shapes", 1),
            ("first-seen payload shape per branch is recorded", 2),
            ("unseen shape at T explains the path taken", 3),
        ]
    elif kind == TemplateKind.CONTRACT_DRIFT:
        raw = [
            ("upstream response shape still matches the code's assumption", 2),
            ("assumed shape is pinned to a recorded upstream version", 1),
            ("drift began between last-good G and onset T", 3),
        ]
    else:  # REGIME_CROSSING
        raw = [
            ("loop/pool/retry site is bounded under normal regime", 1),
            ("metric trend breaks aligned with onset T", 2),
            ("no concurrent regime change explains the break", 4),
        ]
    ordered = sorted(raw, key=lambda item: item[1])
    return [
        TemplateAssumption(expects=text, check_order=i, cost_estimate=cost)
        for i, (text, cost) in enumerate(ordered)
    ]


def _dominant_kind(sites: list[SuspectSite]) -> TemplateKind | None:
    """Family label for a cross-boundary group: first classifiable site
    wins; None when no site carries template semantics. The label is
    descriptive only — the outcome is always a handoff."""
    for site in sites:
        kind = SITE_KIND_TO_TEMPLATE.get(site.kind)
        if kind is not None:
            return kind
    return None


def _dominant_kind(sites: list[SuspectSite]) -> TemplateKind | None:
    """Family label for a cross-boundary group: first classifiable site
    wins; None when no site carries template semantics. The label is
    descriptive only — the outcome is always a handoff."""
    for site in sites:
        kind = SITE_KIND_TO_TEMPLATE.get(site.kind)
        if kind is not None:
            return kind
    return None


#: Revisions that never identify immutable source. Mirrors the SuspectPath
#: fail-closed set; the classifier additionally excludes the empty string.
UNPINNED_REVISIONS = frozenset({"", "HEAD", "UNPINNED"})

#: Per-row site cap: one site per matching line per kind keeps large
#: windows bounded; overflow sheds deterministically (line, kind order).
_MAX_SITES_PER_ROW = 16

#: Explicit range-read syntax table: (site kind, pattern). Checked in
#: order against de-numbered snippet lines; a row may match several kinds
#: and yields one site per match. Anything unmatched stays OTHER.
_SITE_SYNTAX: tuple[tuple[SuspectSiteKind, "re.Pattern[str]"], ...] = (
    (
        SuspectSiteKind.FLAG_GATE,
        re.compile(
            r"\bif\b[^\n]*(flag|feature_flag|is_enabled|toggle|experiment|"
            r"kill[\s_-]?switch|rollout|\bff_)|\b(isEnabled|is_feature_enabled|"
            r"getFlag|isFlagOn)\s*\(",
            re.IGNORECASE,
        ),
    ),
    (
        SuspectSiteKind.BRANCH_MATCH,
        re.compile(
            r"\bswitch\s*\(|\bmatch\s+\w+|^\s*case\s+|\belse\s+if\b|\belsif\b|"
            r"\bwhen\b[^\n]*->",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        SuspectSiteKind.EXTERNAL_CALL,
        re.compile(
            r"https?://[^\s\"']+|\b(requests\s*\.\s*(get|post|put|delete)|"
            r"fetch\s*\(|axios\s*\.|httpClient|restTemplate|webClient|urllib|"
            r"\bgrpc\b|soapClient)\b",
            re.IGNORECASE,
        ),
    ),
    (
        SuspectSiteKind.LOOP_POOL_RETRY,
        re.compile(
            r"\bfor\s*\(|\bwhile\s*\(|\bfor\s+\w+\s+in\b|retry|backoff|"
            r"\bpool\b|executor|ThreadPool|Semaphore|throttle|rate[\s_-]?limit",
            re.IGNORECASE,
        ),
    ),
)

_NUMBERED_LINE = re.compile(r"^\s*(\d+)\|\s?(.*)$")
_URL_HOST = re.compile(r"https?://([^/\s\"']+)")
_GIT_SOURCE = re.compile(r"^git://([^/]+)/")


def _row_attrs(row: object) -> dict[str, object]:
    attrs = getattr(row, "attributes", None)
    return attrs if isinstance(attrs, dict) else {}


def _row_evidence_type(row: object) -> str:
    etype = getattr(row, "evidence_type", "")
    return str(getattr(etype, "value", etype) or "")


def _row_location(row: object) -> tuple[str, int | None, int | None, str]:
    """Return (file_path, line_start, line_end, revision) from provenance."""
    prov = getattr(row, "provenance", None)
    location = getattr(prov, "source_location", None)
    file_path = str(getattr(location, "file_path", "") or "")
    line_start = getattr(location, "line_start", None)
    line_end = getattr(location, "line_end", None)
    try:
        line = int(line_start) if line_start is not None else None
    except (TypeError, ValueError):
        line = None
    try:
        end = int(line_end) if line_end is not None else None
    except (TypeError, ValueError):
        end = None
    revision = str(getattr(location, "revision", "") or "")
    return (
        file_path,
        line if line is not None and line >= 1 else None,
        end if end is not None and end >= 1 else None,
        revision,
    )


def _is_range_row(attrs: dict[str, object], snippet: str) -> bool:
    """True for pinned range reads; path-only/discovery rows are excluded.

    Mirrors the read-mode rule in the context compiler: an explicit
    ``range`` declaration wins; ``path_only``/``discovery_only`` markers
    fail the row; otherwise the legacy numbered-lines shape decides.
    """
    declared = str(attrs.get("read_mode", "") or "")
    if declared == "range":
        return True
    if declared == "path_only" or bool(attrs.get("discovery_only")):
        return False
    first = snippet.splitlines()[0] if snippet else ""
    head = first.split("|", 1)[0].strip()
    return bool(head) and head.isdigit()


def _code_lines(snippet: str) -> tuple[list[tuple[int | None, str]], int | None]:
    """De-numbered snippet lines with their numbers, plus the first
    numbered line, if any."""
    lines: list[tuple[int | None, str]] = []
    first_numbered: int | None = None
    for raw in snippet.splitlines():
        match = _NUMBERED_LINE.match(raw)
        if match is not None:
            try:
                number: int | None = int(match.group(1))
            except ValueError:
                number = None
            if number is not None and first_numbered is None:
                first_numbered = number
            lines.append((number, match.group(2)))
        else:
            lines.append((None, raw))
    return lines, first_numbered


def _matchable_code(body: str) -> str:
    """Remove obvious comments and string literals before syntax matching.

    This is deliberately conservative and language-neutral: uncertain text is
    left alone, while common comment/string decoys cannot create a site.
    """
    text = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "", body)
    text = re.sub(r"(^|\s)(?:#|//).*$", r"\1", text)
    if text.strip().startswith(("/*", "*", "*/")):
        return ""
    return text


def _row_repository(row: object) -> str:
    """Repository identity for path grouping: the ``git://repo/`` source
    prefix when present, else the provenance system, else explicit unknown."""
    source = str(getattr(row, "source", "") or "")
    match = _GIT_SOURCE.match(source)
    if match is not None:
        return match.group(1)[:256]
    system = getattr(getattr(row, "provenance", None), "source_system", "")
    text = str(system or "").strip()
    return (text[:256] if text else "unknown-repo")


def _upstream_identity(snippet: str, file_path: str) -> str:
    """Deterministic upstream name for cross-boundary sites: the first URL
    host literal in the snippet when one is present, else the top-level
    directory of the path. Coarse by design; the handoff carries the
    localizing evidence, not a verified endpoint."""
    match = _URL_HOST.search(snippet)
    if match is not None:
        return match.group(1)[:512]
    top = file_path.strip().lstrip("/").split("/", 1)[0]
    return (top[:512] if top else file_path[:512])


def classify_banked_sites(
    rows: list[object],
    *,
    is_external: Callable[[str], bool] | None = None,
) -> list[SuspectPath]:
    """Classify banked rows into suspect paths (conservative, never raises).

    Admits only SOURCE_CODE rows that are pinned range reads with a
    determinable line: path-only/discovery rows, unpinned revisions, and
    rows without snippet syntax matches are skipped, never guessed.
    Sites group by (repository, revision, file), ordered by line; one
    SuspectPath per file. ``is_external`` marks files outside tenant
    repos as cross-boundary; when omitted everything stays in-tenant.
    """
    groups: dict[tuple[str, str, str], list[SuspectSite]] = {}
    for row in rows or []:
        if _row_evidence_type(row) != "SOURCE_CODE":
            continue
        attrs = _row_attrs(row)
        snippet = str(getattr(row, "content_snippet", "") or "")
        if not snippet.strip() or not _is_range_row(attrs, snippet):
            continue
        file_path, line_start, line_end, revision = _row_location(row)
        if (
            not file_path
            or len(file_path) > 1024
            or ".." in file_path
            or "\x00" in file_path
            or revision in UNPINNED_REVISIONS
            or line_start is None
            or line_end is None
            or line_end < line_start
        ):
            continue
        lines, first_numbered = _code_lines(snippet)
        line = line_start if line_start is not None else first_numbered
        if line is None:
            continue
        text = "\n".join(_matchable_code(body) for _, body in lines)
        if not text.strip():
            continue
        external = bool(is_external(file_path)) if is_external is not None else False
        repository = _row_repository(row)
        candidates: list[tuple[int, str, SuspectSiteKind, str]] = []
        for kind, pattern in _SITE_SYNTAX:
            for lineno, body in lines:
                matchable = _matchable_code(body)
                if not pattern.search(matchable):
                    continue
                hit_text = matchable.strip()
                site_line = lineno if lineno is not None else line
                if site_line is None:
                    site_line = first_numbered
                if site_line is None:
                    continue
                detail = f"matched {kind.value.lower()} syntax: {hit_text[:200]}"
                candidates.append((site_line, kind.value, kind, detail))
        # Deterministic and bounded: line order, then kind order.
        candidates.sort(key=lambda c: (c[0], c[1]))
        for site_line, _, kind, detail in candidates[:_MAX_SITES_PER_ROW]:
            try:
                site = SuspectSite(
                    node_id=uuid4(),
                    kind=kind,
                    file_path=file_path,
                    line=site_line,
                    detail=detail,
                    cross_boundary=external,
                    upstream_identity=(
                        _upstream_identity(snippet, file_path) if external else None
                    ),
                )
            except ValueError:
                continue
            groups.setdefault((repository, revision, file_path), []).append(site)
    paths: list[SuspectPath] = []
    for (repository, revision, _file), sites in sorted(groups.items()):
        ordered = sorted(sites, key=lambda s: (s.line, s.kind.value))
        try:
            paths.append(
                SuspectPath(repository=repository, revision=revision, sites=ordered)
            )
        except ValueError:
            continue
    return paths
