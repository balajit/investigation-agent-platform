# src/investigation_agent_platform/application/investigation/memory.py
import logging
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from investigation_agent_platform.domain.evidence.models import EvidenceType
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.investigation.models import (
    EvidenceManifest,
    Fact,
)

logger = logging.getLogger(__name__)


class OpenInvestigationQuestion(BaseModel):
    question_id: UUID = Field(default_factory=uuid4)
    question: str
    priority: int = Field(..., ge=1, le=5)
    related_hypothesis_ids: list[UUID] = Field(default_factory=list)
    required_evidence_types: list[EvidenceType] = Field(default_factory=list)
    status: str = Field(default="OPEN")


class InvestigationMemory(BaseModel):
    investigation_id: UUID
    stable_facts: list[Fact] = Field(default_factory=list)
    active_hypotheses: list[Hypothesis] = Field(default_factory=list)
    rejected_hypotheses: list[Hypothesis] = Field(default_factory=list)
    key_evidence_snippets: list[EvidenceManifest] = Field(default_factory=list)
    open_questions: list[OpenInvestigationQuestion] = Field(default_factory=list)
    unresolved_contradictions: list[dict[str, str]] = Field(default_factory=list)

    def estimate_tokens(self, text: str) -> int:
        """Approximates token count using standard 4 characters per token heuristic."""
        return max(1, len(text) // 4)

    def get_sliding_context(self, max_token_budget: int = 4000) -> dict[str, list[dict[str, str]]]:
        """Constructs a deterministic, token-aware context window prioritizing facts and contradictions."""
        used_tokens = 0
        context: dict[str, list[dict[str, str]]] = {
            "facts": [],
            "hypotheses": [],
            "contradictions": [],
            "evidence": [],
        }

        # Priority 1: Stable facts
        for fact in self.stable_facts:
            fact_str = f"Fact: {fact.statement}"
            tokens = self.estimate_tokens(fact_str)
            if used_tokens + tokens > max_token_budget:
                break
            context["facts"].append({"fact_id": str(fact.id), "statement": fact.statement})
            used_tokens += tokens

        # Priority 2: Active Hypotheses
        for hyp in self.active_hypotheses:
            hyp_str = f"Hypothesis: {hyp.statement}"
            tokens = self.estimate_tokens(hyp_str)
            if used_tokens + tokens > max_token_budget:
                break
            context["hypotheses"].append({"id": str(hyp.id), "statement": hyp.statement})
            used_tokens += tokens

        # Priority 3: Unresolved Contradictions
        for contradiction in self.unresolved_contradictions:
            c_str = f"Contradiction: {contradiction.get('statement', '')}"
            tokens = self.estimate_tokens(c_str)
            if used_tokens + tokens > max_token_budget:
                break
            context["contradictions"].append(contradiction)
            used_tokens += tokens

        # Priority 4: Highest Relevance Evidence Snippets (Deduplicated)
        seen_evidence_ids: set[UUID] = set()
        ranked_evidence = sorted(
            self.key_evidence_snippets,
            key=lambda manifest: manifest.relevance,
            reverse=True,
        )

        for manifest in ranked_evidence:
            if manifest.evidence_id in seen_evidence_ids:
                continue
            e_str = f"Evidence {manifest.evidence_id}: {manifest.summary}"
            tokens = self.estimate_tokens(e_str)
            if used_tokens + tokens > max_token_budget:
                break
            seen_evidence_ids.add(manifest.evidence_id)
            context["evidence"].append(
                {
                    "evidence_id": str(manifest.evidence_id),
                    "snippet": manifest.summary,
                    "relevance": str(manifest.relevance),
                }
            )
            used_tokens += tokens

        return context