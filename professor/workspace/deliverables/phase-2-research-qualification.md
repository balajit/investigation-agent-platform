# G3 / WP2-R3 Research Qualification

**Scope:** `professor/` and all descendants only.  
**Session:** `ses_e67c30ac6ffeFF4lRLbPoQrKoP`  
**Manifest:** `workspace/evidence/phase2-g3-research-session.manifest.json`  
**Result:** `PARTIAL`

**Capability follow-up evidence:** `workspace/evidence/phase2-g3-search-capability-session.json` (SHA-256 `d7806430bbdcd44bb889fa491da3f0514499d79dccd57168fdaaf65af23a3481`).

**Capability retry evidence:** `workspace/evidence/phase2-g3-search-capability-retry.json` (SHA-256 `a2ca36b4d99267fa7429b562fc14bbf3e0e992d5eb35fbdfe14c91f4683df98c`). The retry again reported no callable `websearch`; the requested query was blocked before execution. `webfetch` was reported exposed but was not called.

**Capability retry 2 evidence:** `workspace/evidence/phase2-g3-search-capability-retry-2.json` (SHA-256 `be1ee0e5c98885e87090839f5b65e02e668caa0d8a204bb33f55e0cff3091f14`). A fresh Research Assistant session again reported no callable `websearch`; the exact requested query was not attempted and returned no results.

## Research Questions And Scope

1. Where does official OpenCode v1.19.x documentation place Markdown agents?
2. Which agent modes does it define?
3. What does it establish about primary-to-subagent delegation?
4. What version caveat applies because current documentation announces OpenCode v2?

**Scope:** Current official agents documentation and the official repository source pinned to `v1.19.0`.  
**Exclusions:** Other OpenCode patch tags, internal implementation, malformed-agent behavior, OS-specific path resolution, and unrelated OpenCode v2 behavior.

## Agents And Capability Status

- **Professor:** directly observed as the orchestrating session.
- **Research Assistant:** task `RA-G3-WP2-R3` completed and returned bounded source analysis; classification `SPECIALIST_REPORTED` until independently checked.
- **Validation Agent:** task `VA-G3-WP2-R3` completed and reviewed the proposed final report; its corrections were incorporated into this final synthesis.
- **webfetch:** directly observed for both required URLs.
- **websearch:** `NOT_VERIFIED`; the runtime did not expose a websearch tool, so an executable search attempt could not be performed.
- **Capability retry:** `BLOCKED` for the requested websearch call because no callable tool was exposed in the Research Assistant runtime.
- **Alternative capability report:** The capability-only follow-up session recorded Atlassian search and Context7 discovery/query tools as exposed alternatives, but they were not called. These are `SPECIALIST_REPORTED`, not independently verified working alternatives.

## Evidence Table

| Source | Directly supported fact | Status |
|---|---|---|
| https://opencode.ai/docs/agents/ | The current page documents project Markdown agents under `.opencode/agents/`, describes primary agents and subagents, and states that primary agents can invoke specialized subagents. | `INDEPENDENTLY_VERIFIED` by Professor via observed webfetch output. |
| https://github.com/anomalyco/opencode/blob/v1.19.0/packages/web/src/content/docs/agents.mdx | The v1.19.0 page documents Markdown agents under `.opencode/agents/`, identifies modes including `primary`, `subagent`, and `all`, and describes subagents as callable by primary agents. | `INDEPENDENTLY_VERIFIED` by Professor via observed webfetch output. |
| Current-page navigation/version notice | The current page announces OpenCode v2, so current documentation must not automatically be treated as equivalent to the installed v1.19.3 behavior. | `INDEPENDENTLY_VERIFIED`; applicability to every v1.19.x patch remains `INFERRED`. |

## Analysis

- **OBSERVED:** Both required official sources were fetched in the session.
- **OBSERVED:** Professor independently inspected the fetched output before final synthesis.
- **SPECIALIST_REPORTED:** Research Assistant returned findings covering locations, modes, delegation, and version caveat.
- **INDEPENDENTLY_VERIFIED:** The source locations and the main Markdown-agent/delegation claims were visible in Professor’s fetch results.
- **INFERRED:** The v1.19.0 source-pinned documentation is the safer compatibility reference for this installed v1.19.3 project than the current page that announces v2.
- **NOT_VERIFIED:** Runtime source discovery through websearch; behavior of all v1.19.x patch releases; exact parser behavior for malformed or duplicate frontmatter.

## Conflicts And Limitations

No direct contradiction was observed between the supplied current page and the v1.19.0 page on the core Markdown-agent location and primary/subagent concepts. The material difference is version context: current documentation announces v2, while this project is configured for OpenCode 1.19.3.

The required websearch capability was not exposed. The session therefore qualified known-URL retrieval and evidence synthesis, not unknown-source discovery.

## Acceptance Status

| Criterion | Status | Evidence |
|---|---|---|
| Bounded task and explicit questions | MET | Session request and final report. |
| Research Assistant delegated | MET | Task event `RA-G3-WP2-R3` completed. |
| Two official sources retrieved | MET | Two observed `webfetch` events. |
| Professor independently inspected source evidence | MET | Professor fetched both URLs directly in the same session. |
| Claims traceable to sources | MET | Evidence table contains both exact URLs and version context. |
| Source discovery through websearch | NOT_VERIFIED | Tool was not exposed. |
| Alternative external search tools | SPECIALIST_REPORTED / NOT_VERIFIED | Atlassian search and Context7 tools were reported exposed but not exercised. |
| Validation Agent reviewed actual final report text | MET | Task `VA-G3-WP2-R3` completed before final synthesis; corrections were incorporated. |
| Complete Research qualification | PARTIAL | Known-URL research passed; source discovery remained unavailable. |

## G3 Decision

**`PARTIAL; G3 RESEARCH QUALIFICATION COMPLETE WITH LIMITATIONS; G4 NOT_STARTED`**

The Research workflow demonstrated bounded evidence-backed retrieval, Professor inspection, synthesis, and independent validation. It did not demonstrate source discovery because `websearch` was unavailable. No G4 Research and Design work was started.
