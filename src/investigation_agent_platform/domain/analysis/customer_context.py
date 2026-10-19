# src/investigation_agent_platform/domain/analysis/customer_context.py
"""Customer context assembly (Phase 4, per-investigation, never persisted as a profile).

Standing profile (HAS) + session trail (DID) + hub mapping (MEANS) with the
mismatch detector: behavior contradicting entitlements is the top signal.
"""

from __future__ import annotations


def assemble_standing_profile(account: dict, entitlements: list[dict]) -> dict:
    """Layer 1: what the customer HAS. Reuses datasource + tenant-gate patterns (caller-enforced)."""
    return {
        "account": {k: account.get(k) for k in ("account_id", "segment", "lifecycle")},
        "entitlements": [
            {"offer": e.get("offer"), "catalog": e.get("catalog"), "hub": e.get("hub", "")}
            for e in entitlements
        ],
    }


def fuse_trail(spine: list[dict], session_picks: list[dict]) -> list[dict]:
    """Layer 2: journey spine + picked items, sorted deterministically."""
    fused = [{"kind": "spine", **s} for s in spine] + [{"kind": "pick", **p} for p in session_picks]
    fused.sort(key=lambda e: (str(e.get("ts", "")), str(e.get("id", ""))))
    return fused


def map_to_hubs(fused: list[dict], hub_aliases: dict[str, str]) -> list[dict]:
    """Layer 3: resolve items/account/entitlements to hubs via alias map."""
    out = []
    for event in fused:
        label = str(event.get("item", event.get("offer", event.get("hub", ""))))
        hub = hub_aliases.get(label, hub_aliases.get(label.lower(), ""))
        out.append({**event, "hub": hub})
    return out


def mismatch_detector(standing: dict, mapped: list[dict]) -> list[dict]:
    """Sharpest signal first: session behavior contradicting the standing profile."""
    entitled = {e.get("offer") for e in standing.get("entitlements", []) if e.get("offer")}
    entitled_hubs = {e.get("hub") for e in standing.get("entitlements", []) if e.get("hub")}
    findings = []
    for event in mapped:
        offer = event.get("offer", event.get("item", ""))
        hub = event.get("hub", "")
        if offer and offer not in entitled:
            findings.append({"signal": "unentitled_item", "offer": offer, "hub": hub, "rank": 1})
        elif hub and hub not in entitled_hubs:
            findings.append({"signal": "ineligible_hub", "offer": offer, "hub": hub, "rank": 2})
    findings.sort(key=lambda f: (f["rank"], str(f.get("offer", ""))))
    return findings
