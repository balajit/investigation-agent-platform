# RS — Causal-graph RCA algorithms for incident investigation (2026-10-29)

- Research date: 2026-10-29. Claims checked against current sources via web search at that time.
- Informs: DES-composition-decision-v1.0, DES-collect-frontier-v1.0, DES-S6-binding-v1.0.

## 1. Causal DAG fundamentals

A causal DAG factorizes the joint distribution (Markov property). Three building blocks: chain `X → Z → Y` (conditioning on Z blocks), fork `X ← Z → Y` (conditioning on Z blocks the spurious association), collider `X → Z ← Y` (marginally independent; conditioning on Z *opens* the path — Berkson's paradox). d-separation: a path is blocked by set S iff every chain/fork has its middle in S and every collider has its middle (and descendants) outside S. Under Markov + faithfulness, d-separation ⇔ conditional independence in data. Constraint-based discovery turns observational telemetry into edges mechanically: start fully connected, delete edge A–B when A ⊥ B | S for conditioning sets grown from neighbors, orient unshielded triples `A–B–C` as colliders when B ∉ Sep(A,C), then Meek propagation. Output is a CPDAG (equivalence class), not a unique DAG — chains and forks are Markov-equivalent without v-structures.

## 2. Discovery algorithms and the incident-window verdict

- PC: needs hundreds–thousands of samples for tens of variables; high-order CI tests unreliable on small samples. Poor fit for 5–30 min incident windows; faithfulness violations (deploy flags, saturation) routine in ops data.
- FCI/RFCI/Anytime-FCI: drops sufficiency (latent confounders allowed) at higher cost; RFCI with small conditioning caps is the only defensible variant, output mostly ambiguous.
- Score-based (GES/GFCI/FGES): more sample-efficient in dense-metric settings but overfits tiny windows; needs a held-out pre-window.
- LiNGAM/additive-noise/NOTEARS: NOTEARS is not scale-invariant (standardization flips edges); avoid without scale analysis. Granger causality is predictive, not interventional.

## 3. Microservice RCA literature (what fits)

- MonitorRank (2013): personalized PageRank/random walk with restart from the anomalous node over the call graph; transition weights from neighbor correlation. Cheap, topology-robust, conflates propagation with causation.
- CloudRanger (2018): learns the impact graph dynamically from metrics, then second-order random walk. Handles stale topology; inherits small-sample discovery problems.
- MS-Rank (2019): multi-metric weighted walks plus operator-feedback weight learning. Cold-start cost (needs repeated incidents per type).
- AutoMAP (2020): anomaly-behavior graph for async/non-topological propagation. Depends on per-metric anomaly-detector quality.
- CIRCA (2022): reframes RCA as intervention recognition — a node is root cause iff P(X | parents) shifts while parents hold. Takes parents from architecture (no discovery), tests mechanism shift across onset. Closest native match for assumption-violation evidence; weak on short pre-windows.
- RCD (2022): learns only localized causal structure around the SLO node (Ψ-PC). Still needs enough normal-period samples.
- ExplainIt! (2019): declarative hypothesis scoring over a metric warehouse; needs hand-authored hypothesis families.
- FluxRank (2019): machine-granular KPI change quantification; cannot name code ranges without an attribution layer.
- MicroRank (2021): traces split normal/abnormal, PageRank-weighted extended spectrum analysis to rank service instances. Direct template for trace-SBFL transfer.

## 4. Spectrum-based fault localization transfer

Tarantula/Ochiai/DStar score entities by failing-vs-passing execution. Mapped to incidents: pre-onset (last-known-good) traces are passing spectra, post-onset traces failing spectra, code ranges/callee edges are entities. Default to Ochiai or DStar2. Caveats: traces are not i.i.d.; single-fault assumption; sampling bias must be normalized (MicroRank-style trace-importance weighting).

## 5. Fit ranking at survey time

1. MicroRank-style extended SBFL over traces — fits the dormant-contrast proof shape, works on short windows, needs trace→code-range attribution (absent at survey time).
2. CIRCA-style mechanism-shift on architecture-derived parent sets — matches assumption-violation evidence natively, needs metric parents and longer pre-windows.
3. Biased random walk on the call/impact graph — cheapest, topology-robust, explains propagation; needs calibrated per-node anomaly scores.
