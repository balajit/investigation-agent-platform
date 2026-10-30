# src/investigation_agent_platform/application/onboarding_agent/agent/questions.py
"""Report Q&A invocation contract (agent design §2, contract 3; §5.2).

A validated report plus resolvable evidence pointers in, cited answer out.
Answers are assembled only from report fields and fixed templates — the
function performs lookups, never inference. Unknowns are answered with the
probe that would resolve them, never with a guess presented as fact.

Evidence scope rule: citations are the evidence pointers of the claims in
the matched service verdict. Pointers live on claims, not dispositions, so
a disposition-level answer cites its sibling claims' evidence — documented
here so callers are never surprised by the scope.
"""

from __future__ import annotations

from investigation_agent_platform.application.onboarding_agent.contracts.coverage import (
    Claim,
    Confidence,
    CoverageReport,
    EvidencePointer,
    ServiceVerdict,
)
from investigation_agent_platform.application.onboarding_agent.contracts.guidance import (
    Audience,
    CitedAnswer,
    CoverageQuestion,
    QuestionKind,
)


def _normalize(text: str) -> str:
    return text.strip().casefold()


def _dedupe_pointers(
    pointers: list[EvidencePointer],
) -> list[EvidencePointer]:
    seen: set[tuple[str, str]] = set()
    unique: list[EvidencePointer] = []
    for pointer in pointers:
        key = (pointer.output_hash, pointer.field_path)
        if key not in seen:
            seen.add(key)
            unique.append(pointer)
    return unique


def _scope_citations(verdict: ServiceVerdict) -> list[EvidencePointer]:
    pointers: list[EvidencePointer] = []
    for claim in verdict.claims:
        pointers.extend(claim.evidence)
    return _dedupe_pointers(pointers)


def _outstanding_probes(verdict: ServiceVerdict) -> list[str]:
    probes: list[str] = []
    for claim in verdict.claims:
        if claim.confidence in (Confidence.PROJECTED, Confidence.SPECULATIVE):
            if claim.confirming_probe and claim.confirming_probe not in probes:
                probes.append(claim.confirming_probe)
    return probes


def _versions_suffix(report: CoverageReport, question: CoverageQuestion) -> str:
    if question.audience == Audience.CUSTOMER:
        return ""
    model = f", model {report.model_version}" if report.model_version else ""
    return f" (analyzer {report.analyzer_version}{model})"


def _unknown_answer(report: CoverageReport, question: CoverageQuestion) -> CitedAnswer:
    return CitedAnswer(
        question=question,
        status="unknown",
        summary=(
            f"Unknown: {question.subject!r} matches no capability or service "
            f"in report {report.report_id} (revision {report.revision}, "
            f"generation {report.generation})."
        ),
        resolving_probe=(
            "Name the capability or service exactly as it appears in the "
            f"report; to cover {question.subject!r}, run a coverage analysis "
            "that includes it."
        ),
    )


def _limitations_answer(report: CoverageReport, question: CoverageQuestion) -> CitedAnswer:
    return CitedAnswer(
        question=question,
        status="answered",
        summary=("Coverage limitations: " + " | ".join(report.limitations)),
    )


def _match(report: CoverageReport, subject: str) -> tuple[ServiceVerdict | None, int | None]:
    """Return (verdict, disposition index or None for a service-level match)."""
    want = _normalize(subject)
    for verdict in report.verdicts:
        for index, disposition in enumerate(verdict.dispositions):
            if _normalize(disposition.capability) == want:
                return verdict, index
    for verdict in report.verdicts:
        if _normalize(verdict.service) == want:
            return verdict, None
    return None, None


def answer_coverage_question(report: CoverageReport, question: CoverageQuestion) -> CitedAnswer:
    """Answer one structured question strictly from the supplied report."""
    if question.kind == QuestionKind.LIMITATIONS:
        return _limitations_answer(report, question)

    verdict, disposition_index = _match(report, question.subject)
    if verdict is None:
        return _unknown_answer(report, question)

    citations = _scope_citations(verdict)
    suffix = _versions_suffix(report, question)

    if disposition_index is not None:
        disposition = verdict.dispositions[disposition_index]
        reasons = "; ".join(disposition.reasons) if disposition.reasons else "no reasons recorded"
        if question.kind == QuestionKind.WHY:
            return CitedAnswer(
                question=question,
                status="answered",
                summary=(
                    f"{disposition.capability} is {disposition.disposition} "
                    f"for service {verdict.service}. Reasons: {reasons}. "
                    f"{len(citations)} evidence pointer(s) cited.{suffix}"
                ),
                matched_service=verdict.service,
                matched_disposition=disposition,
                citations=citations,
                resolving_probe=_first_probe_or_none(verdict),
            )
        if question.kind == QuestionKind.EVIDENCE:
            return CitedAnswer(
                question=question,
                status="answered",
                summary=_pointers_summary(disposition.capability, verdict.service, citations),
                matched_service=verdict.service,
                matched_disposition=disposition,
                citations=citations,
            )
        return _change_answer(report, question, verdict, disposition.capability, citations, suffix)

    pairs = (
        ", ".join(f"{item.capability}={item.disposition}" for item in verdict.dispositions)
        or "no dispositions recorded"
    )
    if question.kind == QuestionKind.WHY:
        return CitedAnswer(
            question=question,
            status="answered",
            summary=(
                f"Service {verdict.service} carries tier "
                f"{verdict.tier.value} with dispositions: {pairs}. "
                f"{len(citations)} evidence pointer(s) cited.{suffix}"
            ),
            matched_service=verdict.service,
            citations=citations,
            resolving_probe=_first_probe_or_none(verdict),
        )
    if question.kind == QuestionKind.EVIDENCE:
        return CitedAnswer(
            question=question,
            status="answered",
            summary=_pointers_summary(verdict.service, verdict.service, citations),
            matched_service=verdict.service,
            citations=citations,
        )
    return _change_answer(report, question, verdict, verdict.service, citations, suffix)


def _first_probe_or_none(verdict: ServiceVerdict) -> str | None:
    probes = _outstanding_probes(verdict)
    return probes[0] if probes else None


def _pointers_summary(subject: str, service: str, citations: list[EvidencePointer]) -> str:
    if not citations:
        return f"Evidence for {subject} in service {service}: no evidence pointers recorded."
    paths = "; ".join(f"{pointer.output_hash[:12]}:{pointer.field_path}" for pointer in citations)
    return f"Evidence for {subject} in service {service} ({len(citations)} pointer(s)): {paths}."


def _change_answer(
    report: CoverageReport,
    question: CoverageQuestion,
    verdict: ServiceVerdict,
    subject: str,
    citations: list[EvidencePointer],
    suffix: str,
) -> CitedAnswer:
    probes = _outstanding_probes(verdict)
    if probes:
        summary = (
            f"What would change {subject} for service {verdict.service}: "
            + "; ".join(probes)
            + f".{suffix}"
        )
    else:
        summary = (
            f"No confirming probes outstanding for {subject} in service "
            f"{verdict.service}: the verdict rests on VERIFIED evidence "
            f"cited above.{suffix}"
        )
    return CitedAnswer(
        question=question,
        status="answered",
        summary=summary,
        matched_service=verdict.service,
        citations=citations,
        resolving_probe=probes[0] if probes else None,
    )


def answerable_claims(report: CoverageReport) -> list[Claim]:
    """All claims in the report, in verdict order. Helper for callers that
    need to enumerate what the report can answer from."""
    claims: list[Claim] = []
    for verdict in report.verdicts:
        claims.extend(verdict.claims)
    return claims
