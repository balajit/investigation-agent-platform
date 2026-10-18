# src/investigation_agent_platform/application/investigation/disposition.py
"""Similar-past-investigation disposition (TO_DO_LLM_List §2).

A new request may restate an earlier investigation. Instead of always
starting a full run, the new request is dispositioned against similar past
runs *deterministically, pre-run*:

- §2.1 matching: exact (tenant + application + ``code_issue_fingerprint``,
  reused from error intake — never a second identity) then near (keyword
  overlap on entities/endpoints/symbols, not embeddings) within a retention
  window. No match → fresh investigation, no links.
- §2.2 case matrix A–G → action + Jira-style links. Links are written by
  this step only, never by the LLM.
- §2.3 re-verification for case B: re-resolve each supporting citation
  rather than re-reasoning.
- §2.5 budget/safety: indexed fingerprint lookup, then a bounded scan of
  recent rows plus at most one verification pass. Never merges scopes;
  never declares duplicate on text similarity alone (case G).

Fail-open: any disposition error degrades to a fresh investigation so the
create path can never break because disposition did.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID

from investigation_agent_platform.infrastructure.observability.tracing import traced

logger = logging.getLogger("iap.application")

# §2.4 link kinds (mirrors EvidenceRelationshipType members).
DUPLICATE_OF = "DUPLICATE_OF"
FOLLOW_UP_OF = "FOLLOW_UP_OF"
RETRY_OF = "RETRY_OF"
POSSIBLY_RELATED = "POSSIBLY_RELATED"
RELATES_TO = "RELATES_TO"

# Node-ID shape for investigation-level links in `evidence_relationships`.
def investigation_node(investigation_id: UUID | str) -> str:
    return f"investigation:{investigation_id}"


def _conclusion_tenant(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any | None:
    """Tenant lives on the conclusion object for re-verification."""
    try:
        conclusion = kwargs.get("conclusion")
        if conclusion is None and args:
            conclusion = args[0] if not isinstance(args[0], UUID) else None
        return getattr(conclusion, "tenant_id", None)
    except Exception:
        return None


def _reverify_extra(verdict: Any) -> dict[str, Any]:
    """Verdict + check counts (never citation text)."""
    try:
        if not isinstance(verdict, dict):
            return {}
        extra: dict[str, Any] = {}
        if verdict.get("verdict"):
            extra["iap.verdict"] = str(verdict["verdict"])
        for key in ("checked", "decayed", "skipped"):
            value = verdict.get(key)
            if isinstance(value, list):
                extra[f"iap.reverify_{key}_count"] = len(value)
        return extra
    except Exception:
        return {}


def _disposition_extra(disposition: Any) -> dict[str, Any]:
    """Disposition action + match kind (never reasons — may quote problems)."""
    try:
        extra: dict[str, Any] = {}
        action = getattr(disposition, "action", None)
        if action is not None:
            extra["iap.disposition"] = str(getattr(action, "value", action))
        if getattr(disposition, "matched_by", None):
            extra["iap.matched_by"] = str(disposition.matched_by)
        if getattr(disposition, "start_run", None) is not None:
            extra["iap.start_run"] = bool(disposition.start_run)
        return extra
    except Exception:
        return {}


class DispositionAction(str, Enum):
    DUPLICATE = "duplicate"  # A: attached to the running original; no new run
    SERVE_CACHED = "serve_cached"  # B valid: cached conclusion served; no new run
    FOLLOW_UP = "follow_up"  # B stale: new run carrying the delta
    SEEDED_FRESH = "seeded_fresh"  # C / E-fresh: new run seeded with prior limits
    RETRY = "retry"  # D: new run; prior partial evidence reusable by ID
    DECLINED = "declined"  # E explicit-not-needed: closed, no link
    FRESH = "fresh"  # no similar past run (or G weak match): full new run
    POSSIBLY_RELATED = "possibly_related"  # G: fresh run + POSSIBLY_RELATED link


# Statuses where the question is still being worked (case A).
ACTIVE_STATUSES = frozenset(
    {
        "CREATED",
        "RUNNING",
        "CONTEXTUALIZING",
        "INVESTIGATING",
        "CORRELATING",
        "HYPOTHESIZING",
        "VERIFYING",
        "CONCLUDING",
        "PAUSED",
        "AWAITING_INPUT",
    }
)

# Conclusion statuses that count as terminal success (case B) vs abstention (case C).
# ANSWERED joins success: a re-asked answered question serves cached.
SUCCESS_CONCLUSIONS = frozenset({"ROOT_CAUSE_ESTABLISHED", "ROOT_CAUSE_LIKELY", "ANSWERED"})
ABSTENTION_CONCLUSIONS = frozenset({"INSUFFICIENT_EVIDENCE", "NO_ROOT_CAUSE_FOUND"})

# Evidence types re-resolved by re-verification step (§2.3).
_CODE_EVIDENCE_TYPES = frozenset({"SOURCE_CODE", "COMMIT_HISTORY", "CALL_GRAPH", "EXCEPTION_PATH"})
_LOG_EVIDENCE_TYPES = frozenset({"RUNTIME_LOG", "LOG", "TRACE", "RUNTIME_TRACE"})
_PROBE_EVIDENCE_TYPES = frozenset({"API_PROBE"})


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def retention_days() -> int:
    """§2.1 near-match retention window (env `IAP_DISPOSITION_RETENTION_DAYS`, default 30)."""
    return _env_int("IAP_DISPOSITION_RETENTION_DAYS", 30)


def similarity_threshold() -> float:
    """§2.1 near-match overlap threshold (env `IAP_DISPOSITION_SIMILARITY_THRESHOLD`, default 0.4)."""
    return _env_float("IAP_DISPOSITION_SIMILARITY_THRESHOLD", 0.4)


def max_candidates() -> int:
    """Bounded scan width for near matching (env `IAP_DISPOSITION_MAX_CANDIDATES`, default 25)."""
    return _env_int("IAP_DISPOSITION_MAX_CANDIDATES", 25)


def log_retention_days() -> int:
    """§2.3 step 3: log-citation retention horizon (env `IAP_DISPOSITION_LOG_RETENTION_DAYS`, default 30)."""
    return _env_int("IAP_DISPOSITION_LOG_RETENTION_DAYS", 30)


_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "for", "with", "from", "that", "this", "these", "those", "are", "was", "were", "has", "have", "had", "not", "but", "also", "when", "what", "which", "there", "their", "they", "them", "then", "than", "into", "over", "under", "again", "once", "here", "how", "why", "can", "will", "just", "about", "after", "before", "during", "while", "where", "error", "failed", "failure", "issue", "problem", "check", "please"]
)

_PATH_RE = re.compile(r"(?<![\w])/(?:[\w.\-]+/)*[\w.\-]+")
_SYMBOL_RE = re.compile(r"(?<![\w])(?:[A-Za-z_][\w]*\.)+[A-Za-z_][\w]*")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")


def keyword_signature(text: str) -> frozenset[str]:
    """Typed keyword set for §2.1 near matching.

    `path:` tokens (endpoint paths), `sym:` tokens (qualified symbols), and
    `word:` tokens (length ≥ 4, stopwords removed). Entity tokens (path/sym)
    carry identity weight in the case-G guard.
    """
    text = text or ""
    sig: set[str] = set()
    for m in _PATH_RE.finditer(text):
        token = m.group(0).rstrip("/").lower()
        if len(token) >= 2:
            sig.add(f"path:{token}")
    for m in _SYMBOL_RE.finditer(text):
        sig.add(f"sym:{m.group(0).lower()}")
    for m in _WORD_RE.finditer(text.lower()):
        word = m.group(0)
        if word not in _STOPWORDS:
            sig.add(f"word:{word}")
    return frozenset(sig)


def entity_tokens(signature: frozenset[str]) -> frozenset[str]:
    """Identity-bearing tokens (paths + symbols) for the case-G guard."""
    return frozenset(t for t in signature if t.startswith(("path:", "sym:")))


def overlap_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Overlap coefficient: |A∩B| / min(|A|,|B|). Tolerant of length skew."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass(frozen=True)
class SimilarMatch:
    investigation_id: UUID
    status: str
    conclusion_status: str | None
    created_at: datetime
    matched_by: str  # "fingerprint" | "entity" | "text"
    score: float


@dataclass(frozen=True)
class Disposition:
    action: DispositionAction
    start_run: bool  # False → create CANCELLED; the start gate then blocks any run
    primary_target_id: UUID | None
    primary_link: str | None
    cluster_link_ids: tuple[UUID, ...] = ()
    matched_by: str | None = None
    reason: str = ""
    seed: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "start_run": self.start_run,
            "primary_target_id": str(self.primary_target_id) if self.primary_target_id else None,
            "primary_link": self.primary_link,
            "cluster_link_ids": [str(i) for i in self.cluster_link_ids],
            "matched_by": self.matched_by,
            "reason": self.reason,
            "seed": self.seed,
            "verdict": self.verdict,
        }


@traced("reason", require_scope=False)
def decide(candidates: list[SimilarMatch]) -> Disposition:
    """§2.2 case matrix over similarity candidates (pure; no I/O).

    Strong identity (fingerprint or shared entity token) runs the full
    matrix A–F. Text-similarity alone falls to case G: a fresh run linked
    POSSIBLY_RELATED — a wrong duplicate is worse than a redundant run.
    """
    strong = [c for c in candidates if c.matched_by in ("fingerprint", "entity")]
    weak = [c for c in candidates if c.matched_by == "text"]
    if not strong:
        if not weak:
            return Disposition(
                action=DispositionAction.FRESH,
                start_run=True,
                primary_target_id=None,
                primary_link=None,
                reason="no similar past investigation within retention",
            )
        # Case G: proceed with the run, link POSSIBLY_RELATED to the best weak match.
        best = max(weak, key=lambda c: (c.score, c.created_at.isoformat()))
        others = tuple(c.investigation_id for c in weak if c.investigation_id != best.investigation_id)[:4]
        return Disposition(
            action=DispositionAction.POSSIBLY_RELATED,
            start_run=True,
            primary_target_id=best.investigation_id,
            primary_link=POSSIBLY_RELATED,
            cluster_link_ids=others,
            matched_by="text",
            reason=(
                f"text similarity alone (score {best.score:.2f}) — proceeding with a fresh run "
                f"per case G; linked {POSSIBLY_RELATED} to {best.investigation_id}"
            ),
        )

    # Case F ordering: prefer the active one (case A); else the most recent
    # terminal success (case B); cluster the rest with RELATES_TO.
    actives = sorted(
        [c for c in strong if c.status in ACTIVE_STATUSES],
        key=lambda c: (0 if c.matched_by == "fingerprint" else 1, c.created_at),
    )
    if actives:
        # Case A: mark duplicate of the running one; never start a second run.
        primary = actives[-1]
        others = tuple(
            c.investigation_id
            for c in strong
            if c.investigation_id != primary.investigation_id
        )[:4]
        return Disposition(
            action=DispositionAction.DUPLICATE,
            start_run=False,
            primary_target_id=primary.investigation_id,
            primary_link=DUPLICATE_OF,
            cluster_link_ids=others,
            matched_by=primary.matched_by,
            reason=(
                f"active investigation {primary.investigation_id} ({primary.status}) already "
                f"covers this request (matched by {primary.matched_by}); duplicate, no new run"
            ),
        )

    terminal = sorted(strong, key=lambda c: c.created_at)
    if not terminal:
        return Disposition(
            action=DispositionAction.FRESH,
            start_run=True,
            primary_target_id=None,
            primary_link=None,
            reason="no usable similar past investigation",
        )
    # Most recent terminal outcome decides; older strong matches cluster.
    latest = terminal[-1]
    others = tuple(c.investigation_id for c in terminal[:-1])[:4]

    if latest.status == "COMPLETED" and (latest.conclusion_status in SUCCESS_CONCLUSIONS):
        # Case B: needs the §2.3 re-verification pass — decided by the caller,
        # which replaces this placeholder via `with_verdict`.
        return Disposition(
            action=DispositionAction.FOLLOW_UP,  # provisional; caller upgrades/downgrades on verdict
            start_run=True,
            primary_target_id=latest.investigation_id,
            primary_link=FOLLOW_UP_OF,
            cluster_link_ids=others,
            matched_by=latest.matched_by,
            reason=(
                f"prior terminal success {latest.investigation_id} ({latest.conclusion_status}); "
                "awaiting re-verification verdict"
            ),
        )
    if latest.status == "COMPLETED" and (latest.conclusion_status in ABSTENTION_CONCLUSIONS):
        # Case C: question still open — new seeded run, never a duplicate.
        return Disposition(
            action=DispositionAction.SEEDED_FRESH,
            start_run=True,
            primary_target_id=latest.investigation_id,
            primary_link=RELATES_TO,
            cluster_link_ids=others,
            matched_by=latest.matched_by,
            reason=(
                f"prior run {latest.investigation_id} abstained ({latest.conclusion_status}); "
                "question open — new seeded run"
            ),
        )
    if latest.status == "FAILED":
        # Case D: retry; prior partial evidence reusable by ID (carried in seed).
        return Disposition(
            action=DispositionAction.RETRY,
            start_run=True,
            primary_target_id=latest.investigation_id,
            primary_link=RETRY_OF,
            cluster_link_ids=others,
            matched_by=latest.matched_by,
            reason=f"prior run {latest.investigation_id} FAILED (platform error); retry",
        )
    if latest.status == "CANCELLED":
        # Case E: like C unless the cancellation was an explicit decline.
        return Disposition(
            action=DispositionAction.SEEDED_FRESH,
            start_run=True,
            primary_target_id=latest.investigation_id,
            primary_link=RELATES_TO,
            cluster_link_ids=others,
            matched_by=latest.matched_by,
            reason=(
                f"prior run {latest.investigation_id} CANCELLED; question open — new seeded run"
            ),
        )
    # Any other terminal shape (e.g. legacy statuses): fresh but linked.
    return Disposition(
        action=DispositionAction.POSSIBLY_RELATED,
        start_run=True,
        primary_target_id=latest.investigation_id,
        primary_link=POSSIBLY_RELATED,
        cluster_link_ids=others,
        matched_by=latest.matched_by,
        reason=f"prior run {latest.investigation_id} in unexpected state {latest.status}; fresh run linked",
    )


def apply_declined(disposition: Disposition) -> Disposition:
    """Case E explicit-decline variant: cancellation meant 'not needed'."""
    return Disposition(
        action=DispositionAction.DECLINED,
        start_run=False,
        primary_target_id=None,
        primary_link=None,
        cluster_link_ids=(),
        matched_by=disposition.matched_by,
        reason="prior cancellation was an explicit decline ('not needed'); closing without a link",
        seed={},
        verdict=None,
    )


def with_verdict(disposition: Disposition, verdict: dict[str, Any], seed: dict[str, Any]) -> Disposition:
    """§2.2 case B: fold the §2.3 re-verification verdict into the disposition."""
    if verdict.get("verdict") == "CONFIRMED":
        return Disposition(
            action=DispositionAction.SERVE_CACHED,
            start_run=False,
            primary_target_id=disposition.primary_target_id,
            primary_link=DUPLICATE_OF,
            cluster_link_ids=disposition.cluster_link_ids,
            matched_by=disposition.matched_by,
            reason=(
                f"prior conclusion re-verified CONFIRMED; serving cached conclusion, "
                f"duplicate of {disposition.primary_target_id}"
            ),
            seed={},
            verdict=verdict,
        )
    merged_seed = dict(seed)
    merged_seed["decayed_citations"] = verdict.get("decayed", [])
    return Disposition(
        action=DispositionAction.FOLLOW_UP,
        start_run=True,
        primary_target_id=disposition.primary_target_id,
        primary_link=FOLLOW_UP_OF,
        cluster_link_ids=disposition.cluster_link_ids,
        matched_by=disposition.matched_by,
        reason=(
            f"prior conclusion STALE ({len(verdict.get('decayed', []))} decayed citations); "
            "follow-up run starts from the delta"
        ),
        seed=merged_seed,
        verdict=verdict,
    )


def excerpt_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@traced("reason", tenant_from=_conclusion_tenant, extra_from=_reverify_extra)
async def reverify_conclusion(
    *,
    prior_investigation_id: UUID,
    conclusion: Any,
    evidence_repo: Any,
    contradiction_repo: Any,
    code_reader: Any | None = None,
    probe_runner: Any | None = None,
) -> dict[str, Any]:
    """§2.3 re-verification: re-resolve each supporting citation, don't re-reason.

    Checks: (1) evidence rows still exist; (2) code citations re-read + hash
    compared; (3) log citations within retention; (4) probes re-issued and
    compared; (5) no new HIGH/CRITICAL contradictions since the conclusion.
    Reads live state only — no mutating actions, no evidence rows except the
    verdict record itself (persisted by the caller in disposition metadata).
    """
    checked: list[str] = []
    decayed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    tenant_id = getattr(conclusion, "tenant_id", "")
    supporting_ids: list[UUID] = list(getattr(conclusion, "supporting_evidence_ids", []) or [])

    rows: dict[UUID, Any] = {}
    if supporting_ids:
        try:
            found = await evidence_repo.get_by_ids(tenant_id, supporting_ids)
            rows = {e.evidence_id: e for e in (found or [])}
        except Exception as exc:
            logger.warning("re-verify: evidence fetch failed: %s", exc)
    for eid in supporting_ids:
        if eid not in rows:
            decayed.append({"evidence_id": str(eid), "reason": "evidence row no longer present"})

    for eid, ev in rows.items():
        etype = str(getattr(getattr(ev, "evidence_type", ""), "value", getattr(ev, "evidence_type", "")) or "").upper()
        snippet = str(getattr(ev, "content_snippet", "") or "")
        if etype in _CODE_EVIDENCE_TYPES:
            checked.append(f"code:{eid}")
            if code_reader is None:
                skipped.append({"evidence_id": str(eid), "reason": "no code reader wired"})
                continue
            try:
                live = await code_reader(ev)
            except Exception as exc:
                decayed.append({"evidence_id": str(eid), "reason": f"code citation unresolvable: {exc}"})
                continue
            if live is None:
                skipped.append({"evidence_id": str(eid), "reason": "code reader returned no content"})
            elif excerpt_hash(live) != excerpt_hash(snippet):
                decayed.append({"evidence_id": str(eid), "reason": "cited range content changed"})
        elif etype in _LOG_EVIDENCE_TYPES:
            checked.append(f"log:{eid}")
            observed = getattr(ev, "observed_at", None)
            if observed is None:
                skipped.append({"evidence_id": str(eid), "reason": "observation time unknown"})
            else:
                horizon = datetime.now(UTC) - timedelta(days=log_retention_days())
                obs = observed if observed.tzinfo else observed.replace(tzinfo=UTC)
                if obs < horizon:
                    decayed.append({"evidence_id": str(eid), "reason": "log window aged out of retention"})
        elif etype in _PROBE_EVIDENCE_TYPES:
            checked.append(f"probe:{eid}")
            if probe_runner is None:
                skipped.append({"evidence_id": str(eid), "reason": "no probe runner wired"})
                continue
            try:
                outcome = await probe_runner(ev)
            except Exception as exc:
                decayed.append({"evidence_id": str(eid), "reason": f"probe re-issue failed: {exc}"})
                continue
            if outcome is False:
                decayed.append({"evidence_id": str(eid), "reason": "probe status/shape differs from recorded excerpt"})
        else:
            checked.append(f"present:{eid}")

    # Step 5: new HIGH/CRITICAL contradictions since the conclusion.
    try:
        generated_at = getattr(conclusion, "generated_at", None)
        contradictions = await contradiction_repo.find_by_investigation_id(tenant_id, prior_investigation_id)
        for c in contradictions or []:
            severity = str(getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", "")) or "").upper()
            if severity not in ("HIGH", "CRITICAL"):
                continue
            if getattr(c, "resolution_status", "UNRESOLVED") != "UNRESOLVED":
                continue
            created = getattr(c, "created_at", None)
            if generated_at is not None and created is not None:
                gen = generated_at if generated_at.tzinfo else generated_at.replace(tzinfo=UTC)
                cre = created if created.tzinfo else created.replace(tzinfo=UTC)
                if cre <= gen:
                    continue
            decayed.append(
                {
                    "evidence_id": "",
                    "reason": f"new {severity} contradiction since conclusion: "
                    f"{str(getattr(c, 'statement', ''))[:200]}",
                }
            )
    except Exception as exc:
        logger.warning("re-verify: contradiction check failed: %s", exc)
        skipped.append({"evidence_id": "", "reason": f"contradiction check unavailable: {exc}"})

    verdict = "CONFIRMED" if not decayed else "STALE"
    return {
        "verdict": verdict,
        "checked": checked,
        "decayed": decayed,
        "skipped": skipped,
        "at": datetime.now(UTC).isoformat(),
    }


@traced("reason")
async def find_similar(
    *,
    tenant_id: str,
    application_id: str,
    problem_description: str,
    fingerprint: str | None,
    investigation_repo: Any,
    finding_repo: Any,
    code_issue_index: Any | None = None,
) -> list[SimilarMatch]:
    """§2.1 matching: exact fingerprint first, then bounded near-text scan."""
    matches: list[SimilarMatch] = []
    seen: set[UUID] = set()

    async def _hydrate(inv_id: UUID) -> Any | None:
        try:
            return await investigation_repo.get_by_id(tenant_id, inv_id)
        except Exception as exc:
            logger.warning("disposition: hydrate %s failed: %s", inv_id, exc)
            return None

    async def _conclusion_status_async(inv: Any) -> str | None:
        conc = getattr(inv, "conclusion", None)
        if conc is None and finding_repo is not None and hasattr(finding_repo, "get_conclusion"):
            try:
                conc = await finding_repo.get_conclusion(tenant_id, inv.id)
            except Exception as exc:
                logger.warning("disposition: conclusion fetch %s failed: %s", inv.id, exc)
                return None
        if conc is None:
            return None
        status = getattr(conc, "status", None)
        return str(getattr(status, "value", status) or "") or None

    # 1. Exact: fingerprint via the shared code-issue index (reuse, §2.1).
    exact_ids: list[UUID] = []
    if fingerprint and code_issue_index is not None:
        try:
            latest = await code_issue_index.latest_investigation(fingerprint)
            if latest is not None:
                exact_ids.append(latest if isinstance(latest, UUID) else UUID(str(latest)))
            rows = await code_issue_index.sessions_for_fingerprint(fingerprint)
            for row in rows or []:
                try:
                    inv_id = row[1] if isinstance(row, (tuple, list)) else row.get("investigation_id")
                    inv_id = inv_id if isinstance(inv_id, UUID) else UUID(str(inv_id))
                    if inv_id not in exact_ids:
                        exact_ids.append(inv_id)
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("disposition: fingerprint lookup failed: %s", exc)
    for inv_id in exact_ids:
        inv = await _hydrate(inv_id)
        if inv is None or getattr(inv, "application_id", None) != application_id:
            continue
        seen.add(inv.id)
        matches.append(
            SimilarMatch(
                investigation_id=inv.id,
                status=str(getattr(getattr(inv, "status", ""), "value", getattr(inv, "status", ""))),
                conclusion_status=await _conclusion_status_async(inv),
                created_at=getattr(inv, "created_at", datetime.now(UTC)),
                matched_by="fingerprint",
                score=1.0,
            )
        )

    # 2. Near: bounded scan of recent rows for this tenant+application.
    try:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days())
        recent_ids = await investigation_repo.list_filtered(
            tenant_id, None, application_id, max_candidates() * 2
        )
    except Exception as exc:
        logger.warning("disposition: recent scan failed: %s", exc)
        recent_ids = []
    own_sig = keyword_signature(problem_description)
    own_entities = entity_tokens(own_sig)
    threshold = similarity_threshold()
    for inv_id in (recent_ids or [])[: max_candidates()]:
        try:
            uid = inv_id if isinstance(inv_id, UUID) else UUID(str(inv_id))
        except Exception:
            continue
        if uid in seen:
            continue
        inv = await _hydrate(uid)
        if inv is None:
            continue
        created = getattr(inv, "created_at", None)
        if created is not None:
            created_cmp = created if created.tzinfo else created.replace(tzinfo=UTC)
            if created_cmp < cutoff:
                continue
        # Fingerprint equality on the row itself also counts as exact.
        row_fp = getattr(inv, "code_issue_fingerprint", None)
        if fingerprint and row_fp == fingerprint:
            seen.add(uid)
            matches.append(
                SimilarMatch(
                    investigation_id=uid,
                    status=str(getattr(getattr(inv, "status", ""), "value", getattr(inv, "status", ""))),
                    conclusion_status=await _conclusion_status_async(inv),
                    created_at=created or datetime.now(UTC),
                    matched_by="fingerprint",
                    score=1.0,
                )
            )
            continue
        other_text = str(getattr(getattr(inv, "request", None), "problem_description", "") or "")
        other_sig = keyword_signature(other_text)
        score = overlap_similarity(own_sig, other_sig)
        if score < threshold:
            continue
        seen.add(uid)
        shared_entities = own_entities & entity_tokens(other_sig)
        matches.append(
            SimilarMatch(
                investigation_id=uid,
                status=str(getattr(getattr(inv, "status", ""), "value", getattr(inv, "status", ""))),
                conclusion_status=await _conclusion_status_async(inv),
                created_at=created or datetime.now(UTC),
                matched_by="entity" if shared_entities else "text",
                score=round(score, 3),
            )
        )
    return matches


async def build_seed(
    *,
    tenant_id: str,
    prior_id: UUID,
    investigation_repo: Any,
    evidence_repo: Any | None = None,
    hypothesis_repo: Any | None = None,
    include_evidence_ids: bool = False,
) -> dict[str, Any]:
    """Carried context so a new run starts from the delta, not from zero (§2.2 C/D/E, §2.3)."""
    seed: dict[str, Any] = {"prior_investigation_id": str(prior_id)}
    try:
        prior = await investigation_repo.get_by_id(tenant_id, prior_id)
    except Exception as exc:
        logger.warning("disposition: seed hydrate %s failed: %s", prior_id, exc)
        return seed
    if prior is None:
        return seed
    conc = getattr(prior, "conclusion", None)
    if conc is not None:
        for key in ("root_cause", "limitations", "recommended_next_steps"):
            value = getattr(conc, key, None)
            if value:
                seed[key] = value if isinstance(value, list) else str(value)[:2000]
        supporting = list(getattr(conc, "supporting_evidence_ids", []) or [])
        if supporting and include_evidence_ids:
            seed["reusable_evidence_ids"] = [str(e) for e in supporting[:50]]
    if hypothesis_repo is not None and hasattr(hypothesis_repo, "find_by_investigation_id"):
        try:
            hyps = await hypothesis_repo.find_by_investigation_id(tenant_id, prior_id)
            statements = [
                str(getattr(h, "statement", ""))[:300]
                for h in (hyps or [])
                if getattr(h, "statement", "")
            ][:10]
            if statements:
                seed["prior_hypothesis_statements"] = statements
        except Exception as exc:
            logger.warning("disposition: seed hypotheses %s failed: %s", prior_id, exc)
    return seed


@traced("reason", extra_from=_disposition_extra)
async def disposition_for_request(
    *,
    tenant_id: str,
    application_id: str,
    problem_description: str,
    fingerprint: str | None,
    investigation_repo: Any,
    finding_repo: Any = None,
    code_issue_index: Any | None = None,
    evidence_repo: Any = None,
    contradiction_repo: Any = None,
    hypothesis_repo: Any = None,
    code_reader: Any | None = None,
    probe_runner: Any | None = None,
    declined: bool = False,
) -> Disposition:
    """Full §2 disposition: match → case matrix → re-verify when case B. Fail-open to FRESH."""
    try:
        matches = await find_similar(
            tenant_id=tenant_id,
            application_id=application_id,
            problem_description=problem_description,
            fingerprint=fingerprint,
            investigation_repo=investigation_repo,
            finding_repo=finding_repo,
            code_issue_index=code_issue_index,
        )
        disposition = decide(matches)
        if disposition.action == DispositionAction.FOLLOW_UP and disposition.primary_target_id is not None:
            # Provisional case B: run the §2.3 verification pass.
            verdict: dict[str, Any] = {"verdict": "STALE", "checked": [], "decayed": [], "skipped": []}
            seed: dict[str, Any] = {}
            try:
                prior = await investigation_repo.get_by_id(tenant_id, disposition.primary_target_id)
                conc = getattr(prior, "conclusion", None) if prior is not None else None
                if conc is None and finding_repo is not None and hasattr(finding_repo, "get_conclusion"):
                    conc = await finding_repo.get_conclusion(tenant_id, disposition.primary_target_id)
                if conc is None:
                    verdict = {
                        "verdict": "STALE",
                        "checked": [],
                        "decayed": [{"evidence_id": "", "reason": "prior conclusion not found"}],
                        "skipped": [],
                        "at": datetime.now(UTC).isoformat(),
                    }
                else:
                    verdict = await reverify_conclusion(
                        prior_investigation_id=disposition.primary_target_id,
                        conclusion=conc,
                        evidence_repo=evidence_repo,
                        contradiction_repo=contradiction_repo,
                        code_reader=code_reader,
                        probe_runner=probe_runner,
                    )
                seed = await build_seed(
                    tenant_id=tenant_id,
                    prior_id=disposition.primary_target_id,
                    investigation_repo=investigation_repo,
                    evidence_repo=evidence_repo,
                    hypothesis_repo=hypothesis_repo,
                    include_evidence_ids=True,
                )
                if conc is not None:
                    root = getattr(conc, "root_cause", None)
                    if root:
                        seed["prior_root_cause"] = str(root)[:2000]
            except Exception as exc:
                logger.warning("disposition: re-verify failed, treating as STALE: %s", exc)
                verdict = {
                    "verdict": "STALE",
                    "checked": [],
                    "decayed": [{"evidence_id": "", "reason": f"re-verification error: {exc}"}],
                    "skipped": [],
                    "at": datetime.now(UTC).isoformat(),
                }
            disposition = with_verdict(disposition, verdict, seed)
        elif disposition.action in (DispositionAction.SEEDED_FRESH, DispositionAction.RETRY) and (
            disposition.primary_target_id is not None
        ):
            disposition = Disposition(
                action=disposition.action,
                start_run=disposition.start_run,
                primary_target_id=disposition.primary_target_id,
                primary_link=disposition.primary_link,
                cluster_link_ids=disposition.cluster_link_ids,
                matched_by=disposition.matched_by,
                reason=disposition.reason,
                seed=await build_seed(
                    tenant_id=tenant_id,
                    prior_id=disposition.primary_target_id,
                    investigation_repo=investigation_repo,
                    evidence_repo=evidence_repo,
                    hypothesis_repo=hypothesis_repo,
                    include_evidence_ids=(disposition.action == DispositionAction.RETRY),
                ),
                verdict=None,
            )
        elif disposition.action == DispositionAction.SEEDED_FRESH and declined:
            disposition = apply_declined(disposition)
        return disposition
    except Exception as exc:
        logger.warning("disposition failed open to FRESH: %s", exc)
        return Disposition(
            action=DispositionAction.FRESH,
            start_run=True,
            primary_target_id=None,
            primary_link=None,
            reason=f"disposition error, proceeding fresh: {exc}",
        )
