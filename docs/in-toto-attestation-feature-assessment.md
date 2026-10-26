# in-toto Attestation Feature Assessment

**Research date:** 2026-10-25  
**Scope:** Attestation Framework, DSSE, classic in-toto workflow tooling, SLSA provenance, and related signer/verification mechanisms as candidates for investigation evidence.  
**Status:** Research assessment; not an implementation authorization.

## 1. Executive summary

The in-toto Attestation Framework defines a portable format for authenticated claims: an envelope carries a typed payload; an in-toto Statement binds that claim to digest-identified subjects; a predicate supplies domain-specific meaning. DSSE is an envelope protocol that signs both the payload type and exact payload bytes. The classic in-toto reference implementation is a separate software supply-chain workflow model based on owner-signed layouts and functionary-signed link metadata. SLSA provenance is a specific predicate for software build claims, not a general-purpose investigation schema.

These technologies can provide record integrity and interoperable claim packaging. They do not, by themselves, provide trusted identity, signer authorization, truth, completeness, lawful collection, evidence custody, freshness, durable retention, or the reasoning that supports an investigation conclusion. Those guarantees require IAP-specific identities, policy, collection controls, domain schemas, storage semantics, and independent verification.

Initial applicability: **use the formats as candidates for signed IAP-specific records, but do not substitute the classic supply-chain workflow model or SLSA predicate for IAP's investigation lifecycle.** The present repository already has evidence provenance, fingerprints, evidence-bound verification, tenant controls, and a proof-kernel design/workstream; the missing layer is a clearly specified and independently verifiable record contract, not necessarily a new service or an external transparency system. The Python Attestation Framework bindings are a notable compatibility/maintenance question: upstream describes them as pre-1.0 generated bindings, and the public PyPI release observed here is older than current repository activity. Do not add that dependency until the exact Python 3.13 artifact/API is verified. DSSE could be implemented through a separately selected, maintained library or adapter, but no library recommendation is made without a pinned package/version review.

### Facts, decisions, and limits

- **Verified upstream capability:** Statement v1 has typed predicates and digest-bearing subjects; DSSE signs a payload type plus exact serialized bytes; classic in-toto verifies supply-chain layouts, links, artifact rules, and inspections.
- **Verified IAP capability:** IAP stores evidence provenance/fingerprints and has separate claim verification, tenant-scoped persistence, append-only timeline/fact patterns, and cryptographic code for other purposes.
- **Repository gap:** Current inspection did not establish signed ordinary evidence records, persistence-boundary digest recomputation, or a generally available independent attestation verifier.
- **Design proposal:** Represent collection, transformation, claim, verification, finding, and conclusion assertions with an IAP-defined predicate vocabulary and explicit identity/policy checks; keep evidence bytes outside the signed statement and bind them by digest/reference.
- **Unverified assumption:** Whether every IAP evidence payload is retrievable as stable immutable bytes, and whether the current proof-kernel work will become the authoritative production verification path, must be resolved before designing a durable attestation lifecycle.

## 2. Ecosystem and component inventory

| Component | Purpose and core abstractions | Status/version observed | Guarantees and limitations | IAP applicability and evidence |
|---|---|---|---|---|
| in-toto Attestation Framework | Portable Envelope, Statement, Predicate, and Bundle model; bindings and predicate schemas. | Current repository v1 specification identifies itself as v1.2; `Statement/v1` remains the type URI. Repository reports active source changes in October 2026; README says framework remains under development. | Standardizes a claim container and subject/predicate relationship; does not define universal predicate semantics, trust policy, or truth. | **Adapter required / candidate format.** [Spec overview](https://in-toto.io/docs/specs/), [v1 spec](https://github.com/in-toto/attestation/tree/main/spec/v1), [repo](https://github.com/in-toto/attestation). |
| in-toto Statement | `_type`, `subject[]`, `predicateType`, optional `predicate`; subjects have digest maps. | Statement v1; framework v1 specification revision 1.2 observed. | Binds claims to digest identities. Subject matching is by digest alone regardless of content type; consumers must enforce context/type expectations. Predicate is domain-specific. | **Direct format reuse** if IAP defines and versions its own predicates and artifact namespace. [Statement spec](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md). |
| in-toto Predicate / ResourceDescriptor | Typed domain claims and reusable resource references. | Current `main` specification; independent predicate versions. | Predicate schema and semantics are owned by its specification. Consumers must reject or safely handle unknown types; digest algorithm requirements need policy. | **Adapter required.** Define investigation-specific predicate URIs and strict supported-version behavior. [Predicate](https://github.com/in-toto/attestation/blob/main/spec/v1/predicate.md), [ResourceDescriptor](https://github.com/in-toto/attestation/blob/main/spec/v1/resource_descriptor.md). |
| DSSE | Envelope with payload, payload type, signatures; PAE binds type and bytes. | DSSE protocol/envelope v1.0.2, dated 2024-05-10; GitHub page does not list a GitHub release artifact. | Verifies signature over exact bytes and type; does not prescribe key management, identity, authorization, or algorithm. `keyid` is unauthenticated and is only a lookup hint. | **Complementary format candidate.** Makes signed records portable; trust/policy remain IAP concerns. [Protocol](https://github.com/secure-systems-lab/dsse/blob/v1.0.2/protocol.md), [Envelope](https://github.com/secure-systems-lab/dsse/blob/v1.0.2/envelope.md), [repo](https://github.com/secure-systems-lab/dsse). |
| Classic in-toto reference implementation | Supply-chain layouts, steps, authorized functionaries, link metadata, artifact rules, inspections. | Python project releases page showed v3.0.0 as latest non-RC and v3.1.0-rc; exact release year was not exposed in the retrieved view. | Verifies an execution against a trusted supply-chain layout; signed links do not establish honest capture if functionary/platform is compromised. | **Unsuitable as IAP's investigation orchestrator.** Could be relevant to IAP's own software supply chain. It is distinct from the attestation bindings. [Repo](https://github.com/in-toto/in-toto), [docs](https://in-toto.readthedocs.io/en/latest/), [releases](https://github.com/in-toto/in-toto/releases). |
| `in-toto-attestation` Python bindings | Generated Python protobuf bindings for the Attestation Framework. | Upstream docs describe pre-1.0; PyPI showed 0.9.3 released 2023-12-13, while source repo has newer activity. Exact compatibility of that distribution with IAP Python 3.13 is not verified. | Binding/model support is not a complete signing, trust, policy, persistence, or investigation service. Package/source skew is a maintenance risk. | **Do not select yet.** Verify a pinned package/source revision and Python 3.13 test matrix. [Python binding source](https://github.com/in-toto/attestation/tree/main/python), [PyPI](https://pypi.org/project/in-toto-attestation/). |
| SLSA provenance | Build predicate for software artifacts: subjects, build definition, resolved dependencies, run details/builder. | Official site identifies SLSA v1.2 as current; provenance and verification pages marked Approved. | Describes software build claims; accuracy depends on builder trust/security model. Dependencies may be best effort; L3 does not protect against compromise of the build platform. | **Not suitable for incident-evidence claims.** Strong fit for attestations about IAP's own built/released artifacts. [Build provenance](https://slsa.dev/spec/v1.2/build-provenance), [verification](https://slsa.dev/spec/v1.2/verifying-artifacts). |
| Sigstore / Cosign / Bundle | Keyless OIDC identity signatures, short-lived Fulcio certificates, Rekor transparency evidence, TUF-distributed trust material, bundle transport and verification. | Rolling documentation; bundle docs describe v0.3.2 updated 2025-01-14. Pin deployed tooling and protobuf schema. | Can provide signer identity evidence and transparency/timestamp material under configured roots and identity policy. Does not establish assertion truth, completeness, or authorization for a particular claim. | **Optional complementary signer/archival mechanism.** Operationally more complex than local keys; not required for an initial internal IAP design. [Cosign signing](https://docs.sigstore.dev/cosign/signing/overview/), [verify](https://docs.sigstore.dev/cosign/verifying/verify/), [security](https://docs.sigstore.dev/about/security/), [bundle](https://docs.sigstore.dev/about/bundle/). |
| NIST SP 800-86 | Forensic techniques within incident response. | Published 2006. | Background guidance only; not an all-inclusive investigation guide or legal advice. | **Context only**, not a product schema or current legal policy. [NIST publication](https://csrc.nist.gov/pubs/sp/800/86/final). |

### Version/status notes

Status statements above are snapshots taken 2026-10-25, not support guarantees. GitHub release pages for in-toto v1.2.0 and classic in-toto v3.0.0 did not expose a release year in the retrieved view; no year is inferred. Current framework `main` activity is not proof a new Python distribution was published. The exact source commit and artifact must be pinned at implementation time.

## 3. Detailed feature catalog

### 3.1 Attestation data model

The Statement has `_type: https://in-toto.io/Statement/v1`, a required `subject` array of digest-bearing descriptors, a `predicateType` URI, and an optional predicate payload. Each subject requires a digest map. The subject digest is the cryptographic content identifier; name is contextual and must not be treated as a substitute for digest validation. Predicate semantics and schema are separate from the outer Statement version. Predicate versions evolve independently. [Statement fields](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md#fields)

The in-toto v1 parsing guidance tells consumers to ignore unrecognized fields unless the predicate specification says otherwise and aims for monotonic policy: removing information should not turn a deny into an allow. For IAP, the domain predicate contract should define required fields, closed enums, supported predicate versions, size limits, timestamp meaning, and unknown-field behavior. Unknown predicate types should be preserved as opaque bytes for archival if required, but never interpreted as accepted evidence by a verifier lacking that predicate's policy.

The DSSE payload signature binds a payload-type string and exact serialized body bytes using PAE. It does not canonicalize JSON. IAP must choose whether its payload is exact generated JSON bytes or a separately specified canonical encoding, reject malformed/ambiguous input, and pass the exact verified bytes into parsing. Do not verify one representation and consume another. [DSSE protocol](https://github.com/secure-systems-lab/dsse/blob/v1.0.2/protocol.md#protocol).

### 3.2 Evidence integrity and provenance

Digest generation and comparison establish that presented bytes match a recorded digest, assuming a collision-resistant approved algorithm and trustworthy digest computation. A digest does not establish source identity, collection correctness, time, authorization, or completeness. Provenance requires authenticated assertions about source, collector, method, transformation, and parent artifacts; authenticity depends on a trusted signer and process.

For derived material, use a new subject digest plus references to parent subject digests and a transformation predicate (operation/tool/version/parameters, actor, execution ID, time semantics, and output). Do not embed large logs, traces, database extracts, or secrets in an attestation. Persist content separately under existing authorization/retention controls and use stable opaque evidence IDs plus digests. Define behavior when the referenced payload is deleted, redacted, inaccessible, or corrected: verification must report unavailable/changed, never silently substitute.

The framework supports multiple subjects, but the Statement is not itself a lineage graph. IAP must define relationships among collected evidence, transformations, agent claims, verification records, findings, and conclusions. A relational append-only record set with explicit parent/reference IDs is proportionate to the current PostgreSQL architecture; a graph database or transparency log is not required absent a concrete query, tamper-evidence, or external-verifier requirement.

### 3.3 Signing, identity, and trust

DSSE verifies signatures over PAE(payloadType, bytes), can carry multiple signatures, and leaves keys, algorithms, trust roots, and ownership out of scope. `keyid` is unauthenticated. Verification must select keys from trusted configuration and evaluate signature threshold/purpose separately. Sigstore can supply identity certificates and transparency evidence, but the verifier still needs exact expected issuer/identity and authorization policy; trust roots and root-update/recovery are operational dependencies.

IAP must supply: identity-to-principal mapping; predicate/action-specific signer authorization; trust-root distribution and freshness; key rotation/revocation procedure; time/freshness semantics; policy version recording; verifier-result audit; separation of producer and verifier roles where required; and access controls for signed payloads. Hardware-backed keys or an external key service may be integrated by a signer adapter, but are not generic in-toto/DSSE guarantees; evaluate only if the trust model requires them. Signing a model-generated claim only attributes the claim to the signing service; it does not upgrade that claim's evidentiary support.

### 3.4 Verification and policy

Verification layers are distinct and should return distinct results:

1. **Envelope/structural validity:** decode envelope, validate supported payload type, parse schema and limits.
2. **Cryptographic validity:** signature(s) validate over the exact bytes.
3. **Identity/trust acceptance:** signer chains/identity satisfy the selected trust root and policy version.
4. **Authorization:** signer is permitted to assert this predicate for this tenant, investigation, subject, and action.
5. **Subject binding:** supplied evidence bytes match the expected digest and subject context.
6. **Semantic validation:** predicate fields and relationships meet domain rules.
7. **Evidence sufficiency:** cited evidence satisfies the existing claim-specific verification obligations.
8. **Conclusion decision:** existing investigation policy decides whether to accept, reject, or abstain.

Only layers 1–6 are candidates for a dedicated attestation verifier; 7–8 remain in the current verification/conclusion boundary. Report exact failure codes and preserve the evaluated trust-policy/version so a verifier result can be reconstructed. Missing, unknown, malformed, mismatched, unauthorized, stale, and conflicting records must not collapse into a successful state.

### 3.5 Lifecycle and workflow

Classic in-toto layouts model an owner-approved software supply-chain process: required steps, functionaries, artifact rules, and inspections. Link metadata records step evidence. This does not map directly to open-ended incident investigations with evolving evidence and disputable conclusions. Reuse Statement/DSSE concepts, not layout/step semantics, unless a future requirement defines a repeatable, controlled evidence-collection procedure that benefits from workflow verification.

An IAP record lifecycle should avoid an unnecessarily large persisted state machine. Proposed minimal semantics: `CREATED` means immutable record bytes persisted; signature envelope may be present or absent per policy; verifier returns an evaluation (`VALID/INVALID/INDETERMINATE` plus typed reasons) against a named policy version. Trust acceptance is an evaluation, not an intrinsic mutable status. Superseding/correction is a new record referencing the prior record; never rewrite signed history. Expiration/revocation are trust-policy inputs, not edits to signed payloads.

### 3.6 Operations and security boundaries

IAP is Python 3.13 (`pyproject.toml`), uses Pydantic, SQLAlchemy async/PostgreSQL, `cryptography`, Temporal, FastAPI, MCP, and RLS. It already has cryptographic dependencies and signing-related code, but that does not demonstrate suitability of a new binding package or a complete trust lifecycle. Candidate library and specification licenses/terms have not been individually analyzed; assess licensing, Python 3.13 wheels/source, transitive dependency/security review, parser behavior, signature APIs, maintenance/release cadence, and interoperability vectors before adoption.

Attestations should hold bounded metadata and digests, not raw sensitive evidence. Store them tenant-scoped, enforce authorization before lookup and before disclosure, avoid global digest-existence oracles, and keep signatures/payloads out of logs. Large evidence belongs in existing payload/object storage. Offline verification is feasible if all required statement bytes, signatures, evidence bytes or trusted digest records, policy version, and trust material are archived; a Sigstore bundle alone does not eliminate trust-root requirements.

No throughput/latency measurements exist for attestation creation or verification in IAP. A single PostgreSQL store and synchronous signing/verifying boundary should be the starting point. Add asynchronous processing only if measured payload/verification cost blocks investigation latency. No separate queue, microservice, cache, transparency deployment, or multi-region design is justified now.

### 3.7 Threat model and residual risk

| Threat | Upstream mechanism | Assumptions / residual risk | IAP control required |
|---|---|---|---|
| Forged/unauthorized envelope | DSSE signature verification | Verification under an arbitrary key is not trust; compromised authorized signer can still sign. | Trusted key/identity selection, purpose/tenant authorization, rotation/revocation. |
| Signed payload modification | Signature over type + exact bytes; Statement subject digest | Does not protect referenced bytes unless retrieved and rehashed; does not prevent deletion. | Recompute digest at collection/read boundary; immutable record bytes; report missing artifact. |
| Evidence substitution | Digest-bearing subject | Digest alone lacks tenant/case/source semantics and content type policy. | Bind stable evidence ID, tenant, investigation/run context, media/schema, digest algorithm, and expected subject role. |
| Replay of old attestation | No general replay prevention in DSSE/Statement | Old valid claim can be replayed into another case or current policy. | Bind investigation/run/event IDs, policy/version, issued-at semantics; enforce uniqueness/state and freshness where required. |
| Authorized but compromised signer | Signature/identity evidence | Cryptography cannot distinguish legitimate operator from compromised identity. | Least privilege, separation of duties, incident response, revocation and re-evaluation. |
| False signed claim / malicious predicate | Typed predicate format | Signatures do not establish truth. | Evidence sufficiency and deterministic verification independent of signer authenticity. |
| Fabricated or incomplete source evidence | No generic prevention | Omitted/false observations can be signed faithfully. | Collector controls, source authentication, query scope/counts, explicit partial/failure indicators, independent corroboration. |
| Compromised tool/collector | in-toto link model can record execution; no protection if trusted functionary lies | Trust in execution and capture remains necessary. | Identify tool and version; constrain collection permissions; preserve raw source response where lawful; audit execution. |
| Cross-tenant substitution/disclosure | No tenant semantics in Statement/DSSE | Valid signature may still expose another tenant's record. | RLS + application authorization; tenant-bound predicate; adversarial cross-tenant tests; no global digest lookup. |
| Unauthorized disclosure | None; signatures are not encryption | Signed payload can be copied by anyone who can read it. | Existing classification/redaction/access policy; encrypt at rest/in transit; minimize payload. |
| Rollback/deletion | Transparency logs may reveal some logged-event inconsistency | Not all events are logged; DB/object deletion or rollback remains possible without external log/backup controls. | Retention, backups, append-only permissions/audit; evaluate external anchoring only if threat warrants. |
| Compromised roots/policy | Sigstore TUF and verifier trust configuration | Root compromise/staleness or policy bug can bless bad identities. | Controlled root updates, policy versioning, review, reproducibility, recovery test. |
| Conflicting conclusions | Multiple signed records possible | No automatic conflict resolution or truth arbitration. | Preserve conflicting records; explicit supersession/review and current conclusion policy. |

## 4. Capability-to-IAP mapping baseline

Repository evidence is summarized here; exact paths and findings are cross-referenced in `iap-attestation-architecture-decision.md`. “Unknown” means inspection did not establish the behavior, not proof it is absent throughout all code paths.

| ID | Capability | Upstream support | Existing IAP implementation / gap | Adaptation and recommendation | Verification required |
|---|---|---|---|---|---|
| ATT-001 | Evidence integrity | Digest-bearing Statement subjects; DSSE bytes | `Evidence.fingerprint` persists; inspected save path does not recompute payload digest. | Define immutable-byte digest lifecycle; integrate format only after contract. | Modify payload after bank and assert retrieval/verification detects mismatch. |
| ATT-002 | Producer provenance | Signed predicate can carry asserted producer/source | `EvidenceProvenance` and `ProvenanceRecord` are structured but not shown signed. | IAP predicate for collection source, collector, method, time semantics, query scope. | Verify unauthorized/missing source identity and collector identity fail. |
| ATT-003 | Agent/tool attribution | DSSE signer and predicate fields | `action_executions` captures principal/policy/status; other action tables differ. No unified signed event established. | Separate asserted actor from cryptographic signer; bind tool/version/run/action ID. | Tamper actor; replay a valid action in another investigation. |
| ATT-004 | Claim/conclusion attribution | Typed predicate and signatures | Hypothesis/finding/conclusion models exist; signed assertion not established. | Separate agent assertion, verifier result, human approval; do not infer truth from signer. | Valid signature on unsupported claim must not pass evidence verifier. |
| ATT-005 | Evidence-to-finding/conclusion linkage | Subject/related references can identify digests | Verification binds evidence to claims; conclusions require evidence in key statuses. No durable signed chain established. | Stable refs and predicate lineage across evidence→claim→verification→finding→conclusion. | Independently reconstruct references; missing/foreign IDs fail closed. |
| ATT-006 | Transformation provenance | Multiple subjects/typed predicate | `ProvenanceRecord` includes input digests and process metadata. No signed transformation record established. | Preserve parent digest and exact transform version/output digest. | Change input/output or tool version and require mismatch/new record. |
| ATT-007 | Signature verification | DSSE and signature libraries; Sigstore optional | `cryptography` dependency and Ed25519 harness verification exist, but for different authorization artifacts. | Reuse vetted library after selection; dedicated policy adapter, no custom crypto. | Real signatures, invalid signatures, algorithm/key rejection, rotation vectors. |
| ATT-008 | Trust-policy enforcement | Not supplied by DSSE; Sigstore supports identity material | JWT identity, tenant scoping; no attestation signer trust policy found. | Pin policy version; map identity to predicate/tenant/action permissions. | Valid signature from unauthorized identity rejected; policy replay test. |
| ATT-009 | Tenant isolation | No tenant model in formats | JWT/API tenant checks, `rls_session`, RLS migrations exist; general RLS proof is uneven. | Keep tenant scope outside untrusted record lookup; tenant bind in predicate + database policy. | Actual-role PostgreSQL cross-tenant get/list/create tests. |
| ATT-010 | Replay / freshness | No generic freshness enforcement | IDs/idempotency, retrieval/freshness metadata and verifier versions exist; case-bound attestation replay policy not established. | Bind run/case/event/policy; define expiry and dedup semantics only where needed. | Copy old statement across run/tenant, expired record, duplicate races. |
| ATT-011 | Tamper-evident history | Signatures protect bytes; transparency logs optional | Timeline, facts, transitions, proof records vary; signed append-only attestation history not established. | Append-only correction/supersession; external anchoring deferred. | Attempt mutation/deletion under runtime role; restore and verify historical record. |
| ATT-012 | Independent verification | Standard formats permit independent parsing/verification | Existing `VerificationEngine` is domain verifier; separate portable attestation verifier not established. | Read-only verifier API/service over persisted bytes + pinned policy; independent implementation vectors. | Restart/offline verify and cross-implementation golden vectors. |
| ATT-013 | Persistence/retrieval | Format does not provide storage | PostgreSQL evidence/investigation repositories and object references exist; no attestation repository. | Prefer tenant-scoped relational records and object refs; no new store initially. | Persist/retrieve exact bytes; verify across process restart. |
| ATT-014 | Schema evolution | Statement type and predicate versioning | IAP has versioned verifier behavior and typed models; attestation schema/version routing absent. | Version predicates and policy; unsupported version is indeterminate/rejected, no env legacy branches. | Golden old/new predicate vectors; unknown version fail-closed. |
| ATT-015 | Error/result semantics | DSSE reports cryptographic verification; no domain outcome vocabulary | Verification outcomes/limitations exist; no attestation-specific stage distinctions. | Distinct invalid/unauthorized/unsupported/unavailable/stale/semantic-fail outcomes. | Ensure no error path becomes “verified” or conclusion accepted. |
| ATT-016 | Privacy, redaction, access | No confidentiality in DSSE | Classification/redaction and tenant-bound endpoints exist. | Sign digests/metadata; control retrieval separately; redaction creates a new artifact/record relationship. | No secret/raw payload in envelope or logs; authorization before disclosure. |
| ATT-017 | Independent historical reproducibility | In-toto and DSSE formats portable | Verifier versions, policy data, and raw evidence retention are separate concerns. | Record policy/verifier identity, exact bytes, trust-material reference, and retrieval status. | Offline historical verification with recorded verifier/policy; deleted evidence is not “pass.” |
| ATT-018 | Incomplete/conflicting evidence | Predicate can record status/links, no generic arbitration | IAP has abstention, contradictions, and finding states. | Preserve partial/failed/conflicting signals; never equate no rows with verified absence. | Missing source, provider failure, contradictory evidence tests. |

## 5. IAP-specific illustrative predicates

The following are **proposed examples only**, not official in-toto predicate schemas. They use synthetic identifiers and omit payload contents. Every `subject.digest` must be calculated from the exact immutable artifact bytes under an approved algorithm; `example.org` is a placeholder namespace.

### 5.1 Collection event for a log record

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [{"name": "evidence:ev-01", "digest": {"sha256": "<64-hex-digest>"}}],
  "predicateType": "https://example.org/iap/predicate/collection/v1",
  "predicate": {
    "tenant_ref": "tenant-demo",
    "investigation_ref": "inv-demo",
    "event_id": "collect-01",
    "source": {"system": "synthetic-log-source", "record_ref": "record-01"},
    "collector": {"principal_ref": "collector-service", "adapter": "log-adapter", "version": "1"},
    "retrieved_at": "2026-10-25T12:00:00Z",
    "source_time": {"value": "2026-10-25T11:59:58Z", "basis": "source-reported"},
    "completeness": "single-record"
  }
}
```

Establishes: the accepted signer asserted that this collector event refers to a particular digest and declared source/time basis. Does not establish source truth, completeness beyond declared scope, or that source time is trustworthy. Verify signer authorization, payload digest, tenant/run binding, source scope, and time-policy before accepting it.

### 5.2 Correlated trace set and transformation

The subject is the serialized, bounded span-set artifact; predicate references source span evidence digests and the correlation algorithm/version. Verify every parent reference, tenant/run scope, transformation version, and output digest. A correlation record says which inputs and procedure produced a set; it does not prove the correlation is causally correct.

### 5.3 Hypothesis, verification, finding, and conclusion

Represent the model's hypothesis as an **assertion** referencing evidence IDs/digests and typed claim; represent verifier evaluation as a separate record referencing the exact claim and evidence snapshot with verifier/policy version and typed outcome; represent finding and conclusion as later records linked to those records. A valid signature on a hypothesis does not satisfy a proof obligation. Findings may be accepted, rejected, or inconclusive. A reopened investigation creates new events and a superseding conclusion; it must not overwrite the original signed conclusion.

### 5.4 Scenario obligations matrix

All rows below describe proposed IAP-specific predicates, not standard in-toto claims. “Digest” means digest of the exact immutable serialized artifact bytes, not a hash of a mutable URI or a short summary.

| Scenario | Subject / asserted producer | Predicate references and fields | Time semantics | Verification obligations and trust | Establishes / does not establish |
|---|---|---|---|---|---|
| Collector retrieves an application log record | One evidence record subject; collector service asserts retrieval; signer is separately identified. | Evidence ID + digest; source system/record reference; query/scope and result count; adapter/version; tenant/investigation/run; redaction/completeness status. | Source event time (source-reported), retrieval time (collector clock), signing time separate. | Verify exact bytes/digest, collector identity authorization, source scope, tenant/run, query outcome and whether result is partial. | Establishes an authenticated collection assertion for bytes; not that the source generated a true record or collector saw all records. |
| Trace investigation produces correlated spans | Correlated span-set artifact; correlation tool/service asserts transformation. | Parent span/evidence digests, input selection criteria, correlation method/tool version, output digest, dropped/missing-span indicators, execution ID. | Span event times retain source basis; correlation/retrieval times separate. | Verify every parent exists/is accessible and belongs to scope; recompute output digest; validate declared completeness and tool policy. | Establishes declared inputs/procedure/output relationship; not causal correctness of the correlation. |
| Agent generates hypothesis from multiple artifacts | Hypothesis/claim serialization; agent runtime asserts it, signing service is distinct if used. | Claim ID/type/target, exact statement digest, evidence IDs/digests and each binding role, model/prompt/tool versions, run ID, limitation/uncertainty markers. | Generation time is agent/service time; referenced observations preserve their own times. | Verify claim bytes and evidence references; authenticate agent execution identity; never treat model identity/signature as evidentiary sufficiency. | Establishes that an identified agent asserted a claim using listed references; not that the claim follows from evidence. |
| Verifier evaluates evidence support | Immutable verifier-result record; verifier service asserts evaluation. | Claim digest, evidence-set snapshot digest, verifier/rule version, policy version, per-obligation result/reason, missing/conflicting IDs. | Evaluation time is verifier time; evidence times unchanged. | Verify verifier identity/version/policy; independently reconstruct input snapshot; rerun deterministic checks where available; compare exact result. | Establishes a verifier produced a specific evaluation under identified rules; not universal truth or a conclusion if policy does not authorize it. |
| Finding accepted, rejected, or inconclusive | Finding record; authorized investigation service or reviewer asserts disposition. | Finding ID/type/status, claim/verifier references, supporting/refuting evidence digests, rationale/limitations, actor and policy. | Decision time separate from evidence and verifier times. | Verify decision-maker authorization and that cited verifier/evidence records remain valid and in-scope; preserve abstention and conflict states. | Establishes a recorded disposition under policy; not objective correctness. |
| Final conclusion references supporting findings | Conclusion subject/record; authorized conclusion service or reviewer asserts it. | Conclusion ID/status, finding IDs/digests, supporting/refuting evidence snapshot, verifier/policy versions, unresolved limitations, investigation/run identity. | Conclusion time distinct; source time remains sourced from evidence. | Verify all referenced records, tenant/run binding, evidence sufficiency via existing conclusion gate, authorization, policy version. | Establishes a conclusion record and declared basis; signature alone cannot make it correct. |
| Investigation reopened on conflicting new evidence | New conflict/reopen event subject; authorized workflow/reviewer asserts the change. | Prior conclusion digest, new evidence digests, conflict relationship, reopen reason, new run ID, supersedes/disputes link; retain old record. | New evidence collection and reopen times separate from old conclusion time. | Verify new evidence and actor, detect replay, preserve old conclusion and mark current status via explicit relation, re-evaluate under applicable policy. | Establishes a later conflicting event and lifecycle transition; does not erase the prior record or automatically decide which claim is true. |

### 5.5 Temporal semantics

Keep collection time, source-reported event time, signing time, and verification time distinct. Signing time is not automatically trusted event time. Bind a claim to investigation ID, execution/run ID, and relevant evidence-set digest to reduce replay across contexts. A replay guard needs persisted uniqueness and a policy-defined time/state rule; DSSE does not supply it.

## 6. Findings and research conclusion

### Reuse first

1. Reuse the Statement subject/predicate pattern if a portable, signed claim format is needed.
2. Reuse DSSE's exact-byte and payload-type signing protocol through a vetted implementation, subject to Python 3.13 and dependency review.
3. Use Sigstore only if IAP has a concrete need for external identity/transparency/offline bundles; it is optional, not foundational.
4. Apply SLSA provenance to IAP build/release artifacts, not investigation evidence.

### IAP must own

- Investigation-specific predicate schemas and versioning.
- Evidence-byte identity, digest recomputation, stable IDs, retention and unavailability semantics.
- Source/collector/agent/tool identity, authorization and trust policy.
- Replay/freshness and case/run binding.
- Semantic verification, evidence sufficiency, finding/conclusion policy, and contradiction handling.
- Tenant isolation, privacy/redaction, audit persistence, policy versioning, and historical reconstruction.

### Do not adopt without a concrete need

- Classic in-toto layouts/link workflow as a replacement for Temporal or investigation state.
- SLSA provenance predicates for logs, traces, hypotheses, or conclusions.
- A dedicated transparency service, blockchain, distributed event store, or new attestation microservice for the current scale.
- Keyless Sigstore for every internal event if simpler managed signing keys satisfy the threat model.

### Preliminary recommendation and confidence

**Recommendation:** Hybrid at the data-format boundary, with a deliberate defer on dependency selection and production integration. Define IAP-owned predicates and policy; keep evidence, claim verification, proof obligations, and conclusions in their current domain boundaries. Reuse Statement/DSSE concepts and a proven library only after a pinned-runtime/package review. Keep classic workflow tooling and SLSA scoped to software supply chain. Do not add a trust service, queue, store, migration, or public API yet.

**Confidence:** High on upstream format mechanics and limits (official specifications); medium on IAP gap classification (repository inspection, no full runtime/database test performed); low on the final Python binding choice until artifact compatibility, API coverage, and release mapping are verified. The final adoption decision is in `iap-attestation-architecture-decision.md` and remains design-only.

### Unresolved questions

1. Which evidence classes have immutable retrievable bytes, and which only have summaries or references?
2. Must independent verifiers operate outside IAP, and what interoperability format must they accept?
3. Which principals are authorized to assert collection, agent, verification, and human-review events?
4. Is durable tamper-evidence against a database administrator in scope, or are ordinary append-only controls/backups sufficient?
5. What historical retention and offline-verification requirements apply to raw evidence and trust roots?
6. Does the current proof-kernel work become the production authority for some claim types, and how will that result be bound without introducing a second verdict authority?
7. Which maintained Python library/version supports Python 3.13 and the required DSSE/Statement behavior with acceptable dependency and licensing risk?

## 7. Authoritative source index

- [in-toto specifications overview](https://in-toto.io/docs/specs/)
- [in-toto Attestation Framework v1](https://github.com/in-toto/attestation/tree/main/spec/v1), [Statement](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md), [Envelope](https://github.com/in-toto/attestation/blob/main/spec/v1/envelope.md), [Predicate](https://github.com/in-toto/attestation/blob/main/spec/v1/predicate.md), [versioning](https://github.com/in-toto/attestation/blob/main/spec/versioning.md)
- [Framework repository/releases](https://github.com/in-toto/attestation/releases), [Python bindings](https://github.com/in-toto/attestation/tree/main/python), [PyPI](https://pypi.org/project/in-toto-attestation/)
- [DSSE protocol v1.0.2](https://github.com/secure-systems-lab/dsse/blob/v1.0.2/protocol.md), [envelope v1.0.2](https://github.com/secure-systems-lab/dsse/blob/v1.0.2/envelope.md)
- [Classic in-toto reference implementation](https://github.com/in-toto/in-toto), [Python docs](https://in-toto.readthedocs.io/en/latest/), [releases](https://github.com/in-toto/in-toto/releases)
- [SLSA v1.2 provenance](https://slsa.dev/spec/v1.2/build-provenance), [verification](https://slsa.dev/spec/v1.2/verifying-artifacts/)
- [Sigstore signing](https://docs.sigstore.dev/cosign/signing/overview/), [verification](https://docs.sigstore.dev/cosign/verifying/verify/), [security model](https://docs.sigstore.dev/about/security/), [bundle format](https://docs.sigstore.dev/about/bundle/)
- [NIST SP 800-86](https://csrc.nist.gov/pubs/sp/800/86/final)
