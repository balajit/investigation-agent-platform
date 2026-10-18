# ADR-001: Reasoning and Evidence Separation

**Status**: Accepted

**Context**: Investigation reasoning prompts must remain stable when the set of underlying evidence providers changes.

**Decision**: Investigation reasoning and evidence acquisition are strictly separated. Reasoning operates only on the normalized `Evidence` domain contract and never touches provider SDKs directly.

**Rationale**: Reasoning models remain unchanged when replacing infrastructure providers. Evidence providers can be added, removed, or swapped without retraining or re-prompting the reasoning layer.