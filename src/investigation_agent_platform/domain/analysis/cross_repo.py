# src/investigation_agent_platform/domain/analysis/cross_repo.py
"""Cross-repo issue queries over resolved edges (Phase 5).

Spoon sidecar emits resolved client→endpoint/call edges with Exact/Inferred
grades (ingested as EdgeRecords). Config-server snapshots are a required edge
source for gateway-mediated hops. Queries are pure traversals — deterministic.
"""

from __future__ import annotations


def _adjacency(edges: list[dict]) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {}
    for e in edges:
        if not isinstance(e, dict) or "src" not in e or "dst" not in e:
            raise ValueError(f"malformed edge record (src/dst required): {e!r}")
        adj.setdefault(str(e["src"]), []).append(str(e["dst"]))
    for v in adj.values():
        v.sort()
    return adj


def _reverse_adjacency(edges: list[dict]) -> dict[str, list[str]]:
    rev: dict[str, list[str]] = {}
    for e in edges:
        if not isinstance(e, dict) or "src" not in e or "dst" not in e:
            raise ValueError(f"malformed edge record (src/dst required): {e!r}")
        rev.setdefault(str(e["dst"]), []).append(str(e["src"]))
    for v in rev.values():
        v.sort()
    return rev


def blast_radius(edges: list[dict], node: str, depth: int = 3) -> list[str]:
    """Reverse-edge walk: who depends on node (re-verify scope on library change)."""
    rev = _reverse_adjacency(edges)
    seen = {node}
    frontier = [node]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for dep in rev.get(n, []):
                if dep not in seen:
                    seen.add(dep)
                    nxt.append(dep)
        frontier = nxt
    return sorted(seen - {node})


def dependency_depth(edges: list[dict], src: str, dst: str, max_depth: int = 6) -> int:
    """Shortest forward path across gateway hops; -1 when unreachable."""
    adj = _adjacency(edges)
    frontier = [(src, 0)]
    seen = {src}
    while frontier:
        node, d = frontier.pop(0)
        if node == dst:
            return d
        if d >= max_depth:
            continue
        for m in adj.get(node, []):
            if m not in seen:
                seen.add(m)
                frontier.append((m, d + 1))
    return -1


def shared_lib_fanout(edges: list[dict], lib: str) -> list[str]:
    return blast_radius(edges, lib, depth=5)


def env_asymmetric_diff(dev_edges: list[dict], prod_edges: list[dict]) -> dict:
    """Dev-vs-prod drift evidence as edge-set diff (sorted, deterministic)."""
    dev = {(str(e["src"]), str(e["dst"]), str(e.get("kind", ""))) for e in dev_edges}
    prod = {(str(e["src"]), str(e["dst"]), str(e.get("kind", ""))) for e in prod_edges}
    return {"only_in_dev": sorted(dev - prod), "only_in_prod": sorted(prod - dev)}


def platform_single_point(edges: list[dict], platform_nodes: list[str]) -> list[dict]:
    """SAT/eureka/config-server one-hop causes for multi-service symptoms."""
    rev = _reverse_adjacency(edges)
    out = []
    for p in platform_nodes:
        dependents = rev.get(p, [])
        if dependents:
            out.append({"platform": p, "dependents": dependents, "count": len(dependents)})
    out.sort(key=lambda r: (-r["count"], r["platform"]))
    return out
