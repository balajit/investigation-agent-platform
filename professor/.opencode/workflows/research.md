
# Workflow: Research

## Runtime Capability Gate

Before delegating external research, establish which source-retrieval tools are actually available to the active OpenCode runtime. `webfetch` retrieves a known URL; `websearch` availability depends on the active provider/runtime and may require an explicitly supported environment setting. A permission entry is not proof of availability. If search is unavailable, use another actually available and authorized source discovery mechanism only when one is present; otherwise return PARTIAL or BLOCKED and explain what capability is missing. Never imply that discovery or retrieval occurred when it did not.

## 1. Purpose

Answer a defined question through structured investigation and traceable evidence.

Research may concern technical behavior, architecture, frameworks, APIs, standards, security, business processes, or other factual topics.

The output must distinguish established facts from interpretation and recommendation. It must make clear what is known, what remains uncertain, and how the evidence supports the answer.

## 2. Activation Conditions

Professor selects this workflow when the user requests:
- Research into a technical or factual question.
- Comparison of technologies, approaches, or documented capabilities.
- Investigation of a system behavior not requiring a full design.
- Evidence gathering for a decision or report.
- Current information that depends on authoritative external sources.
- A concise or comprehensive research deliverable.

Use `research-and-design.md` if the primary deliverable requires a proposed architecture or implementation design.

Use `code-review.md` if the primary task is to identify defects in existing source code.

## 3. Inputs

Capture:

- The question or questions to answer.
- The purpose of the research.
- Relevant context and constraints.
- Scope and exclusions.
- Required depth.
- Whether current information is necessary.
- Preferred source types, if specified.
- Required output format and destination.
- Acceptance criteria.

Proceed with explicit assumptions when optional context is missing.

Ask a concise clarifying question if ambiguity would materially change the research objective or risk producing a misleading result.

## 4. Roles

### Professor
Defines the research scope, evaluates evidence, resolves conflicting interpretations, and synthesizes the answer.

### Prompt-gen
Structures complex or multi-part research into a bounded task contract.

### Research Assistant
Collects and analyzes evidence, prioritizing authoritative sources and explicit limitations.

### Code Analyst
Examines repository artifacts when the answer depends on implementation details rather than external documentation alone.

### Adversarial Reviewer
Challenges consequential conclusions, contested claims, and recommendations that depend on uncertain evidence.

### Validation Agent
Checks substantial research deliverables against the requested questions, evidence standards, and acceptance criteria.

## 5. Execution Procedure

### Stage 1 — Define the Research Questions

Convert the user's request into a finite set of answerable questions.

For each question, identify:
- Why it matters.
- What evidence would answer it.
- Relevant time period or version.
- Required source quality.
- Scope boundaries.
- Acceptance criteria.

Separate factual questions from evaluative questions and recommendations.

### Stage 2 — Plan Evidence Collection

Create a bounded research plan.

Prioritize:
1. Primary sources and official documentation.
2. Standards, specifications, and authoritative records.
3. Official release notes and security advisories.
4. Reputable secondary sources for context.
5. Community discussions when practical experience or community sentiment is relevant.

For time-sensitive claims, check publication or update dates and whether the source applies to the requested period.

Do not assume a source is authoritative merely because it appears prominently in search results.

### Stage 3 — Gather Evidence

Use Research Assistant for external research.

Confirm available tools before assigning search/retrieval work. The Research Assistant may return PARTIAL or BLOCKED when necessary source discovery or access is unavailable; absence of evidence must not be hidden by speculative synthesis.

Use Code Analyst when repository inspection is needed to establish what the implementation actually does.

For each material source, record:
- Source identifier.
- Title or artifact name.
- Publisher or origin.
- URL, file path, line range, or other locator.
- Publication/revision date and retrieval date when available.
- Source quality (official specification/documentation, standard, original research, primary technical source, or secondary analysis).
- Claim supported.
- Limitations and version applicability.

Inspect the source sufficiently to establish that it supports the associated claim.

Search-result snippets alone should not be treated as conclusive evidence for material claims when the underlying source can be inspected.

### Stage 4 — Analyze and Reconcile Evidence

Classify conclusions as:

- OBSERVED FACT — directly established by inspected evidence.
- SUPPORTED CONCLUSION — inference supported by multiple facts or a strong evidentiary basis.
- HYPOTHESIS — plausible but unverified.
- RECOMMENDATION — proposed course of action.
- UNKNOWN — insufficient evidence to determine the answer.

For conflicting evidence:
1. Identify the precise disagreement.
2. Compare source authority, relevance, freshness, and version.
3. Examine differences in scope, assumptions, and definitions.
4. Seek additional evidence when the disagreement could change the conclusion.
5. Preserve unresolved conflicts explicitly.

Do not manufacture consensus or conceal contradictory evidence.

### Stage 5 — Synthesize the Answer

Answer the original questions directly.

For each important conclusion:
- State the conclusion.
- Provide supporting evidence.
- Explain material reasoning.
- Identify uncertainty and limitations.
- Distinguish evidence from recommendation.

Avoid excessive detail unrelated to the user's objective.

For comparisons, use explicit criteria and explain where evidence is incomplete or not directly comparable.

For current or changing topics, state the applicable time frame and version assumptions.

### Stage 6 — Adversarial Review

Request Adversarial Reviewer when:
- Conclusions have significant consequences.
- Sources conflict.
- The answer contains material uncertainty.
- Recommendations depend on contested assumptions.
- The user requests a rigorous or adversarial investigation.

Ask the reviewer to identify unsupported claims, missing evidence, alternative interpretations, stale sources, and conclusions stronger than the evidence permits.

Professor must resolve material challenges or disclose them.

### Stage 7 — Validate the Deliverable

Use Validation Agent for substantial reports or research with explicit acceptance criteria.

Check:
- Every requested question is answered or marked unresolved.
- Important claims have traceable support.
- Sources are appropriate and applicable.
- Facts and inferences are distinguished.
- Contradictions and limitations are disclosed.
- The requested format and destination are satisfied.

Do not claim validation occurred unless it actually did.

## 6. Required Deliverable

Produce a Markdown report containing:

1. Executive Answer.
2. Research Questions and Scope.
3. Key Findings.
4. Evidence Table.
5. Analysis and Reasoning.
6. Conflicting Evidence, if any.
7. Unknowns and Limitations.
8. Recommendations, if requested or warranted.
9. Acceptance Criteria Self-Check.
10. Validation Results, when performed.
11. Sources.

Use citations close to the claims they support.

When research artifacts are requested, save them under `workspace/research/` or `workspace/deliverables/` according to the project's artifact conventions and the user's specified destination.

## 7. Acceptance Criteria

- The user's core questions are answered directly.
- Scope and exclusions are explicit.
- Material claims have traceable evidence.
- Source authority, relevance, and freshness are considered.
- Facts, conclusions, hypotheses, recommendations, and unknowns are distinguished.
- Conflicting evidence is reconciled or disclosed.
- Important assumptions and limitations are explicit.
- The report follows the requested format and depth.
- Validation is completed when required, or its absence is disclosed.

## 8. Stop Conditions

Stop when the research questions are adequately answered and additional evidence is unlikely to materially change the conclusion.

If evidence is insufficient, explain what is missing and what would resolve the uncertainty.

Do not continue gathering sources merely to increase their count. Prefer a small number of authoritative, directly relevant sources over a large collection of weakly relevant sources.

Ask before expanding the research into a materially different subject or performing consequential actions.

## 9. Final Response

Professor presents:
- The direct answer.
- The most important supporting evidence.
- Material qualifications and unknowns.
- Recommendations, if applicable.
- The deliverable location.
- Validation status and remaining limitations.
