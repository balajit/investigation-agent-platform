# rea-agents Evaluation — PARKED v1.0

Status: PARKED by owner decision. Not needed for this project. No installation
performed, no configuration changed.

## What was evaluated

`rea-agents` (MIT; MCP server + agent skill + CLI for reverse engineering:
native binaries via caller-supplied Hopper/Ghidra/IDA, JS/Electron apps, .NET,
APKs, firmware, WASM/EVM, model files, packet captures). Install routes:
`npx rea-agents setup` (auto-registers MCP + workflow instructions),
`npm install --global rea-agents` (CLI), or skill-only.

## Findings

- Environment has Node v26.10.0 / npm 11.19.1, below the tool's stated Node
  floor (22.23.4+ / 24.22.1+ / 26.12.1+). A supported Node range would be
  required before any install attempt.
- opencode is not in the tool's named supported-agents list, so auto-setup
  may not know this config layout; manual MCP registration would have been
  the safe route (exact server argv unverified — never guessed).
- Security posture noted: remote-code execution via npx (pin versions,
  verify package identity), and a by-design privileged local MCP server
  (binary/app/device inspection under user permissions) that warrants
  deliberate scoping and a toggle.

## Why parked

The tool's Java surface is APK/manifest/decompiled-method inspection, not
source-model querying. It contributes nothing to the Java QL-style
query-layer investigation (Q0–Q4) or to Stage E prototypes. Its native and
binary strengths address target classes outside this project's scope.

## What would unpark this

A committed need to inspect built artifacts, packaged applications, or
binary/bytecode targets (wheels, APKs, EVM, firmware) during investigations,
plus a supported Node range and an explicit install approval with scope
(global vs project) and flavor (manual MCP registration recommended).
