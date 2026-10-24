# src/investigation_agent_platform/application/knowledge/intake.py
"""Error intake with code-issue merge-or-fork (Part 6 D4/D8, Slice 0).

Same code issue (tenant-agnostic fingerprint) -> annotate a new session on
the canonical investigation instead of forking a duplicate. New code issue
-> create investigation (session 1). Prior conclusions are preserved
untouched; closed targets reopen via the standard lifecycle transition.
Cancelled targets are never resurrected: a new investigation is forked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from investigation_agent_platform.domain.common.exceptions import (
    DomainValidationException,
)
from investigation_agent_platform.domain.common.utils import Clock, SystemClock
from investigation_agent_platform.domain.investigation.models import (
    ActorType,
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationStatus,
)
from investigation_agent_platform.domain.knowledge.fingerprint import build_code_issue_fingerprint
from investigation_agent_platform.domain.knowledge.models import InvestigationSession
from investigation_agent_platform.infrastructure.observability.tracing import traced

logger = logging.getLogger("iap.application")

def _intake_extra(result: Any) -> dict[str, Any]:
    """Merge flag + session number (never the fingerprint value)."""
    try:
        extra: dict[str, Any] = {}
        if getattr(result, "merged", None) is not None:
            extra["iap.merged"] = bool(result.merged)
        session = getattr(result, "session_number", None)
        if isinstance(session, int):
            extra["iap.session_number"] = session
        return extra
    except Exception:
        return {}


def build_intake_seed(
    *,
    error_class: str,
    failing_symbol: str,
    caller_symbol: str | None = None,
    top_frame_file: str | None = None,
    frames: list[Any] | None = None,
    log_refs: list[str] | None = None,
    trace_refs: list[str] | None = None,
    severity: Any = None,
    expected: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
    repository: str | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """Intake seed for investigation orientation (D1, pure).

    Packs caller-provided failure context into metadata["intake_seed"]:
    frame list (for WALK_STACK auto-dispatch), log/trace refs (for first-
    turn resolution into banked rows), severity (for the triage
    classifier), and golden expected/actual pairs (for silent-failure
    comparison). Bounded and JSON-scalar only. Never invents.

    Phase 0.5: the resolved repository and revision are persisted so the
    investigation carries its target identity. An unresolved repository or
    revision defaults to ``UNPINNED`` — never a silent ``HEAD``.
    """
    try:
        seed: dict[str, Any] = {
            "error_class": str(error_class or "")[:256],
            "failing_symbol": str(failing_symbol or "")[:256],
            "repository": str(repository or "").strip()[:512] or "UNPINNED",
            "revision": str(revision or "").strip()[:128] or "UNPINNED",
        }
        if caller_symbol:
            seed["caller_symbol"] = str(caller_symbol)[:256]
        if top_frame_file:
            seed["top_frame_file"] = str(top_frame_file)[:1024]
        parsed_frames: list[dict[str, Any]] = []
        for frame in list(frames or [])[:20]:
            try:
                if isinstance(frame, dict):
                    entry = {
                        key: frame[key]
                        for key in ("file", "line", "function")
                        if frame.get(key) is not None
                    }
                else:
                    entry = {
                        key: getattr(frame, key, None)
                        for key in ("file", "line", "function")
                    }
                    entry = {k: v for k, v in entry.items() if v is not None}
                if entry:
                    parsed_frames.append(
                        {k: (v if not isinstance(v, str) else v[:1024]) for k, v in entry.items()}
                    )
            except Exception:
                continue
        if parsed_frames:
            seed["frames"] = parsed_frames
        if log_refs:
            seed["log_refs"] = [str(r)[:512] for r in list(log_refs)[:50]]
        if trace_refs:
            seed["trace_refs"] = [str(r)[:512] for r in list(trace_refs)[:50]]
        try:
            severity_value = getattr(severity, "value", severity)
            if severity_value is not None:
                seed["severity"] = str(severity_value)[:32]
        except Exception:
            pass
        if expected:
            seed["expected"] = {str(k)[:128]: v for k, v in list(expected.items())[:50]}
        if actual:
            seed["actual"] = {str(k)[:128]: v for k, v in list(actual.items())[:50]}
        return seed
    except Exception:
        return {"error_class": str(error_class or "")[:256]}


def _silent_symbol(error_class: str, problem_description: str) -> str:
    """Deterministic fallback identity for silent failures (D1, pure).

    Silent cases carry no exception identity; hash the complaint so
    distinct complaints stay distinct and identical ones merge.
    """
    try:
        import hashlib as _hashlib

        digest = _hashlib.sha256(
            (problem_description or error_class or "").encode()
        ).hexdigest()[:16]
        return f"silent:{digest}"
    except Exception:
        return "silent:unknown"


@dataclass
class IntakeResult:
    investigation_id: UUID
    session_number: int
    merged: bool
    code_issue_fingerprint: str


class RepositoryScopeError(DomainValidationException):
    """The application profile declares more than one repository and the
    caller did not choose one.

    Phase 0.5: picking the first repo would silently investigate the wrong
    codebase. Ambiguity fails closed; the caller must name the repository.
    """

    def __init__(self, application_id: str, candidates: list[str]) -> None:
        super().__init__(
            message=(
                f"Application '{application_id}' declares multiple repositories; "
                "repository must be specified explicitly"
            ),
            details={
                "application_id": application_id,
                "candidates": list(candidates),
            },
        )
        self.error_code = "AMBIGUOUS_REPOSITORY_SCOPE"
        self.candidates = list(candidates)


def resolve_repository_scope(profile: Any) -> tuple[str, list[str]]:
    """Resolve a single canonical repository from an application profile (pure).

    Returns ``(repository_id, candidates)``. ``Service.repository_id`` is
    canonical; ``ServiceCode.repos`` locators are the fallback. One distinct
    value resolves; zero yields ``("", [])`` (unpinned); more than one yields
    ``("", candidates)`` so the caller can fail closed.
    """
    try:
        services = list(getattr(profile, "services", None) or [])
        repo_ids = {
            str(getattr(service, "repository_id", "") or "").strip()
            for service in services
        }
        repo_ids.discard("")
        if len(repo_ids) == 1:
            return next(iter(repo_ids)), sorted(repo_ids)
        if len(repo_ids) > 1:
            return "", sorted(repo_ids)
        locators: set[str] = set()
        for service in services:
            code = getattr(service, "code", None)
            for repo in list(getattr(code, "repos", None) or []):
                name = str(repo or "").strip()
                if name:
                    locators.add(name)
        if len(locators) == 1:
            return next(iter(locators)), sorted(locators)
        return "", sorted(locators)
    except Exception:
        return "", []


class ErrorIntakeService:
    """Merge-or-fork intake for auto-forwarded (and analyst-reported) errors."""

    def __init__(
        self,
        investigation_repo: Any,
        session_repo: Any,
        code_issue_index: Any,
        clock: Clock | None = None,
        profile_repo: Any | None = None,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.session_repo = session_repo
        self.code_issue_index = code_issue_index
        self.clock = clock or SystemClock()
        # Phase 0.5: optional; when present, resolves the canonical
        # repository for the application so intake carries target identity.
        self.profile_repo = profile_repo

    @traced("knowledge", component="intake", extra_from=_intake_extra)
    async def intake(
        self,
        *,
        tenant_id: str,
        application_id: str,
        error_class: str,
        repository: str,
        revision: str,
        failing_symbol: str,
        caller_symbol: str | None = None,
        top_frame_file: str | None = None,
        problem_description: str = "",
        session_id: str | None = None,
        requested_by: str = "intake",
        log_refs: list[str] | None = None,
        trace_refs: list[str] | None = None,
        occurred_at: datetime | None = None,
        frames: list[Any] | None = None,
        severity: Any = None,
        expected: dict[str, Any] | None = None,
        actual: dict[str, Any] | None = None,
    ) -> IntakeResult:
        now = occurred_at or datetime.now(UTC)
        # Phase 0.5: resolve the canonical repository from the application
        # profile when the caller did not pin one. Ambiguous multi-repo
        # scope fails closed rather than guessing; an absent scope stays
        # UNPINNED (visible), never a silent HEAD.
        repository = await self._resolve_repository(
            tenant_id=tenant_id,
            application_id=application_id,
            repository=repository,
        )
        revision_value = (revision or "").strip()
        intake_seed = build_intake_seed(
            error_class=error_class,
            failing_symbol=failing_symbol,
            caller_symbol=caller_symbol,
            top_frame_file=top_frame_file,
            frames=frames,
            log_refs=log_refs,
            trace_refs=trace_refs,
            severity=severity,
            expected=expected,
            actual=actual,
            repository=repository,
            revision=revision_value,
        )
        fingerprint = build_code_issue_fingerprint(
            error_class=error_class,
            repository=(repository or "").strip() or "UNPINNED",
            revision=(revision_value or "").strip() or "UNPINNED",
            failing_symbol=(failing_symbol or "").strip() or _silent_symbol(error_class, problem_description),
            caller_symbol=caller_symbol,
            top_frame_file=top_frame_file,
        )

        candidate_id = await self.code_issue_index.latest_investigation(fingerprint)
        # Merge = link to the shared lineage, NOT load another tenant's row.
        # Cross-tenant rows are invisible under RLS by design, so the merge
        # target is always this tenant's own most-recent investigation for
        # the fingerprint (or a fresh row). Shared knowledge flows through
        # SHARED artifacts; session numbering stays global via the index.
        prior_rows = await self.code_issue_index.sessions_for_fingerprint(fingerprint)
        merged = len(prior_rows) > 0
        target: Investigation | None = None
        if candidate_id is not None:
            existing = await self.investigation_repo.get_by_id(tenant_id, candidate_id)
            if existing is not None and existing.status != InvestigationStatus.CANCELLED:
                target = existing
        if target is None and merged:
            # Another tenant holds the lineage; resolve this tenant's own
            # most-recent row for it (if any) via the index.
            for _, inv_id, _ in reversed(prior_rows):
                own = await self.investigation_repo.get_by_id(tenant_id, inv_id)
                if own is not None and own.status != InvestigationStatus.CANCELLED:
                    target = own
                    break

        if target is None:
            # No reusable own row (first session ever, or this tenant's rows
            # are all cancelled): fork a fresh row linked by fingerprint.
            # `merged` keeps its lineage value — joining an existing lineage
            # with a fresh row is still a merge, not a fork.
            investigation = await self._create_investigation(
                tenant_id=tenant_id,
                application_id=application_id,
                problem_description=problem_description or error_class,
                session_id=session_id or f"sess_{uuid4().hex[:12]}",
                requested_by=requested_by,
                fingerprint=fingerprint,
                intake_seed=intake_seed,
            )
        else:
            investigation = await self._maybe_reopen(tenant_id, target)

        session_number = await self.code_issue_index.next_session_number(fingerprint)
        session = InvestigationSession(
            investigation_id=investigation.id,
            session_number=session_number,
            tenant_id=tenant_id,
            occurred_at=now,
            log_refs=list(log_refs or []),
            trace_refs=list(trace_refs or []),
        )
        await self.session_repo.append(tenant_id, session)
        await self.code_issue_index.record_session(
            fingerprint, session_number, investigation.id, now
        )
        logger.info(
            "Intake session recorded",
            extra={
                "context": {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation.id),
                    "session_number": session_number,
                    "merged": merged,
                }
            },
        )
        return IntakeResult(
            investigation_id=investigation.id,
            session_number=session_number,
            merged=merged,
            code_issue_fingerprint=fingerprint,
        )

    async def _resolve_repository(
        self, *, tenant_id: str, application_id: str, repository: str
    ) -> str:
        """Return the caller's repository, or resolve it from the profile."""
        explicit = (repository or "").strip()
        if explicit and explicit.upper() not in ("UNPINNED", "UNKNOWN", "HEAD"):
            return explicit
        if self.profile_repo is None:
            return explicit
        try:
            profile = await self.profile_repo.get_by_application_id(
                tenant_id, application_id
            )
        except Exception:
            return explicit
        if profile is None:
            return explicit
        resolved, candidates = resolve_repository_scope(profile)
        if resolved:
            return resolved
        if len(candidates) > 1:
            raise RepositoryScopeError(application_id, candidates)
        return explicit

    async def _create_investigation(
        self,
        *,
        tenant_id: str,
        application_id: str,
        problem_description: str,
        session_id: str,
        requested_by: str,
        fingerprint: str,
        intake_seed: dict[str, Any] | None = None,
    ) -> Investigation:
        now = self.clock.utcnow()
        request = InvestigationRequest(
            application_id=application_id,
            problem_description=problem_description,
            session_id=session_id,
            requested_by=requested_by,
        )
        context = InvestigationContext(
            environment="production",
            time_window=(now, now),
        )
        seed = dict(intake_seed or {})
        metadata: dict[str, Any] = {}
        # Version pin (remediation plan ARCH-HIGH-006): every persisted
        # investigation carries the verifier version it started with, so
        # rows keep their semantics when the engine evolves. (Function-level
        # import keeps the knowledge import graph light, same as services.py.)
        from investigation_agent_platform.application.investigation.verification import (
            VERIFIER_VERSION_CURRENT,
        )

        metadata["verifier_version"] = VERIFIER_VERSION_CURRENT
        if seed:
            metadata["intake_seed"] = seed
        # Phase 0.5: target identity lives on the investigation, not only
        # inside the seed, so downstream stages can read it directly.
        if seed.get("repository"):
            metadata["repository"] = seed["repository"]
        if seed.get("revision"):
            metadata["revision"] = seed["revision"]
        investigation = Investigation(
            session_id=session_id,
            application_id=application_id,
            tenant_id=tenant_id,
            request=request,
            context=context,
            status=InvestigationStatus.CREATED,
            created_at=now,
            updated_at=now,
            code_issue_fingerprint=fingerprint,
            metadata=metadata,
        )
        await self.investigation_repo.create(tenant_id, investigation)
        return investigation

    async def _maybe_reopen(self, tenant_id: str, target: Investigation) -> Investigation:
        if target.status in (InvestigationStatus.COMPLETED, InvestigationStatus.FAILED):
            reopened, _ = target.transition_to(
                InvestigationStatus.INVESTIGATING,
                ActorType.SYSTEM,
                "recurring code issue",
                clock=self.clock,
            )
            await self.investigation_repo.save(tenant_id, reopened, target.version)
            return reopened
        return target
