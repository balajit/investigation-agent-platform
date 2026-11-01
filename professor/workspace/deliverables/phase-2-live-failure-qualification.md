# G6 / WP2-R6 Live Failure Handling Qualification

**Scope:** `professor/` and all descendants only.  
**Session:** `ses_e66fd2ac9ffeGV9h9OZ2bRpiG5`  
**Manifest:** `workspace/evidence/phase2-g6-live-failure-session.manifest.json`  
**Result:** `PARTIAL`; live injection qualification `NOT_VERIFIED`.

## Runtime Capability Gate

The actual Professor session inspected the Professor workflows, schemas, scripts, and task surface. No supported result-injection, specialist-availability override, forced-failure, malformed-result, or conflicting-result control surface was exposed or demonstrated.

Ordinary task calls returned specialist results, but they were not injected failures and do not qualify the live failure boundary. No scenario is marked `PASS`.

## Scenario Matrix

| ID | Scenario | Status | Reason |
|---|---|---|---|
| F1 | No supporting evidence | NOT_VERIFIED | No controlled injection boundary; ordinary task path cannot prove Professor’s failure disposition. |
| F2 | Incomplete specialist result | BLOCKED | No supported way to inject an incomplete specialist result. |
| F3 | Conflicting specialist results | BLOCKED | No supported way to inject two controlled conflicting results. |
| F4 | Adversarial Reviewer rejection | BLOCKED | No supported way to force a controlled reviewer rejection at the result boundary. |
| F5 | Validation failure | BLOCKED | No supported way to force a controlled Validation Agent FAIL/REVISE result. |
| F6 | Unavailable research capability | PARTIAL | The session documented the known unavailable `websearch` capability, but did not exercise a complete research failure workflow. |
| F7 | Blocked specialist | BLOCKED | No supported specialist availability override or controlled blocked-result injection. |
| F8 | Malformed specialist response | BLOCKED | No supported malformed-result injection boundary. |

## Agents And Evidence

- Professor session: directly observed.
- Prompt-gen: completed qualification-contract task.
- Research Assistant: completed an unavailable-research task and an insufficient-evidence task; these were ordinary task executions, not injected result failures.
- Validation Agent: completed validation-failure task and final G6 matrix validation task; no controlled failure result was injected.
- Adversarial Reviewer: completed rejection/challenge tasks; no controlled reviewer result was injected.

The final Professor result explicitly states that no supported injection/control mechanism was demonstrated and that G7 was not started.

## Decision

**`PARTIAL; LIVE FAILURE BOUNDARY NOT_VERIFIED; G7 NOT_STARTED`**

The session establishes a runtime limitation: the current supported task surface does not expose a demonstrated mechanism for controlled specialist-result injection or availability overrides. It does not establish that Professor handles all eight failure cases correctly at runtime.

## Limitations

- No shell commands, file edits, commits, external side effects, or production inputs were used.
- Provider-independent fixtures from earlier gates are not counted as live G6 evidence.
- The absence of an injection surface is an observation of this runtime/project surface, not proof that no future OpenCode integration could provide one.
- Working-tree hashes are perishable; future gates must prefer committed blobs or reverify hashes at gate close.
