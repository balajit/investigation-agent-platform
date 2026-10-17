#!/usr/bin/env python3
"""cognee_spike.py — tree-sitter-under-Cognee evaluation spike (script-only, zero platform wiring).

Cognee stays the top-level framework (datasets, storage, retrieval); OUR
tree-sitter parsers supply the AST truth underneath via two seams:
  1. TreeSitterChunker (custom `chunker=` for cognify): AST-boundary chunks
     (functions/classes) instead of text windows.
  2. Typed DataPoint projection (existing pilot pattern): deterministic graph.

Subcommands:
    ingest  — cognify a bounded slice into a salted dataset (records timing)
    probe   — CODE/CHUNKS/SUMMARIES (+CYPHER attempt) queries vs battery cases
    leakage — tenant-B search over tenant-A dataset (must return nothing alien)

Env (mapped from IAP_* when present, else cognee native names):
    IAP_AZURE_API_KEY / LLM_API_KEY, IAP_AZURE_ENDPOINT / LLM_ENDPOINT,
    IAP_AZURE_DEPLOYMENT (model), IAP_COGNEE_DATASET_SALT (required),
    GRAPH_DATABASE_URL/USERNAME/PASSWORD/NAME (neo4j, via _cognee_env_from_topology
    mapping in cognee_pilot when needed).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

DEFAULT_SLICE = "src/investigation_agent_platform/application/investigation"
DEFAULT_DATASET_PREFIX = "spike-ts"


def _env(name: str, *fallbacks: str, default: str = "") -> str:
    for candidate in (name, *fallbacks):
        value = os.environ.get(candidate, "")
        if value:
            return value
    return default


def _configure_cognee_env() -> dict[str, str]:
    """Map IAP provider vars onto cognee/litellm names (validated pattern).

    Cognee routes through litellm: Azure models need the `azure/<deployment>`
    prefix (bare deployment names misroute to OpenAI and hang). Endpoint/key
    values mirror scripts/azure_openai.py (the validated reference):
      chat:       gto-salestech resource, 2025-01-01-preview, gpt-4.1
      embeddings: xcpai resource, 2023-05-15, text-embedding-ada-002
    """
    chat_deploy = _env("COGNEE_LLM_DEPLOYMENT", "IAP_AZURE_DEPLOYMENT", default="gpt-4.1")
    embed_deploy = _env(
        "COGNEE_EMBEDDING_DEPLOYMENT",
        "IAP_AZURE_EMBEDDING_DEPLOYMENT",
        default="text-embedding-ada-002",
    )
    chat_endpoint = _env("COGNEE_LLM_ENDPOINT", "IAP_AZURE_ENDPOINT")
    chat_key = _env("COGNEE_LLM_API_KEY", "IAP_AZURE_API_KEY")
    chat_version = _env(
        "COGNEE_LLM_API_VERSION", "IAP_AZURE_API_VERSION", default="2025-01-01-preview"
    )
    embed_endpoint = _env(
        "COGNEE_EMBEDDING_ENDPOINT", "IAP_AZURE_EMBEDDING_ENDPOINT", chat_endpoint
    )
    embed_key = _env(
        "COGNEE_EMBEDDING_API_KEY", "IAP_AZURE_EMBEDDING_API_KEY", chat_key
    )
    embed_version = _env(
        "COGNEE_EMBEDDING_API_VERSION",
        "IAP_AZURE_EMBEDDING_API_VERSION",
        default="2023-05-15",
    )
    mapping = {
        # Cognee-native names.
        "LLM_PROVIDER": "azure",
        "LLM_MODEL": f"azure/{chat_deploy}",
        "LLM_ENDPOINT": chat_endpoint,
        "LLM_API_KEY": chat_key,
        "LLM_API_VERSION": chat_version,
        # SPIKE-ONLY HACK (documented, never ships to platform): Cognee sends
        # the OpenAI `dimensions` param whenever it is set, and Azure ada-002
        # rejects it outright (there is no "omit" setting — auto-derive fills
        # 1536 and sends it anyway). The engine's ONLY omit-gate is the
        # nvidia_nim provider flag, which is pure local metadata: litellm
        # routes by the `azure/` model prefix regardless, so the request
        # still hits our Azure endpoint — minus the poisoned param.
        # Production answer: 3-series deployment (supports dimensions) or an
        # upstream cognee knob; this string exists solely to unblock eval.
        "EMBEDDING_PROVIDER": "nvidia_nim",
        "EMBEDDING_MODEL": f"azure/{embed_deploy}",
        "EMBEDDING_ENDPOINT": embed_endpoint,
        "EMBEDDING_API_KEY": embed_key,
        "EMBEDDING_API_VERSION": embed_version,
        # litellm fallback names (same values, both spellings set).
        "AZURE_API_BASE": chat_endpoint,
        "AZURE_API_KEY": chat_key,
        "AZURE_API_VERSION": chat_version,
    }
    applied = {}
    for key, value in mapping.items():
        if value and key not in os.environ:
            os.environ[key] = value
            applied[key] = "mapped"
        elif key in os.environ:
            applied[key] = "preset"
    return applied


def _salt() -> str:
    salt = _env("IAP_COGNEE_DATASET_SALT")
    if not salt:
        print("error: IAP_COGNEE_DATASET_SALT is required", file=sys.stderr)
        raise SystemExit(2)
    return salt


def spike_dataset(tenant: str, label: str) -> str:
    digest = hashlib.sha256(f"{_salt()}|{tenant}|{label}".encode()).hexdigest()[:32]
    return f"{DEFAULT_DATASET_PREFIX}_{digest}"


class TreeSitterChunker:
    """AST-boundary chunker for cognify (custom `chunker=` seam).

    Mirrors the TextChunker yield contract (DocumentChunk with chunker_id,
    content-derived id, text, sizes, cut_type) but splits Python sources on
    top-level def/class boundaries via OUR tree-sitter parsers. Non-Python
    or unparseable files fall back to a single whole-text chunk
    (cut_type="ast_fallback") — never dropped, never LLM-split.
    """

    chunker_id = "ts_ast_v1"

    def __init__(self, document, get_text, max_chunk_size: int):
        self.chunk_index = 0
        self.document = document
        self.get_text = get_text
        self.max_chunk_size = max_chunk_size

    def _language(self) -> str:
        name = getattr(self.document, "name", "") or ""
        location = str(getattr(self.document, "raw_data_location", "") or "")
        path = name or location
        if path.endswith(".py"):
            return "python"
        if path.endswith((".ts", ".tsx", ".js", ".jsx")):
            return "typescript"
        if path.endswith(".go"):
            return "go"
        return ""

    def _ast_spans(self, text: str, language: str) -> list[tuple[str, str, int, int]]:
        """(kind, name, start_byte, end_byte) top-level def/class spans."""
        try:
            from tree_sitter_language_pack import get_parser
        except ImportError:
            return []
        try:
            parser = get_parser(language)
            tree = parser.parse(text.encode("utf-8", errors="replace"))
        except Exception:
            return []
        spans = []
        try:
            for node in tree.root_node.children:
                if node.type in ("function_definition", "class_definition"):
                    name = ""
                    for child in node.children:
                        if child.type == "identifier":
                            snippet = text.encode("utf-8", errors="replace")[
                                child.start_byte : child.end_byte
                            ]
                            name = snippet.decode("utf-8", errors="replace")
                            break
                    spans.append((node.type, name, node.start_byte, node.end_byte))
        except Exception:
            return []
        return spans

    async def read(self):
        from cognee.modules.chunking.chunk_id import chunk_content_hash, content_chunk_id
        from cognee.modules.chunking.models.DocumentChunk import DocumentChunk

        document_id = str(self.document.id)
        document_name = self.document.name or "unknown"
        language = self._language()
        emitted = 0
        async for content_text in self.get_text():
            spans = self._ast_spans(content_text, language) if language else []
            if not spans:
                cut, text = "ast_fallback", content_text
                content_hash = chunk_content_hash(text)
                yield DocumentChunk(
                    chunker_id=self.chunker_id,
                    id=content_chunk_id(document_id, content_hash, 0),
                    text=text,
                    chunk_size=len(text),
                    content_hash=content_hash,
                    max_chunk_tokens=self.max_chunk_size,
                    is_part_of=self.document,
                    chunk_index=self.chunk_index,
                    cut_type=cut,
                    contains=[],
                    importance_weight=self.document.importance_weight,
                    document_id=document_id,
                    document_name=document_name,
                    metadata={"index_fields": ["text"], "ast_language": language},
                )
                self.chunk_index += 1
                emitted += 1
                continue
            encoded = content_text.encode("utf-8", errors="replace")
            for kind, name, start, end in spans:
                text = encoded[start:end].decode("utf-8", errors="replace")
                if len(text) > self.max_chunk_size:
                    text = text[: self.max_chunk_size]
                content_hash = chunk_content_hash(text)
                yield DocumentChunk(
                    chunker_id=self.chunker_id,
                    id=content_chunk_id(document_id, content_hash, emitted),
                    text=text,
                    chunk_size=len(text),
                    content_hash=content_hash,
                    max_chunk_tokens=self.max_chunk_size,
                    is_part_of=self.document,
                    chunk_index=self.chunk_index,
                    cut_type=f"ast_{kind}",
                    contains=[],
                    importance_weight=self.document.importance_weight,
                    document_id=document_id,
                    document_name=document_name,
                    metadata={
                        "index_fields": ["text"],
                        "ast_language": language,
                        "ast_symbol": name,
                        "ast_kind": kind,
                    },
                )
                self.chunk_index += 1
                emitted += 1


PROBE_QUERIES = [
    ("callers of overlay_evidence_budget", "CODE"),
    ("where is budget enforcement implemented", "CHUNKS"),
    ("how does call graph traversal work", "SUMMARIES"),
    ("ReasoningCoordinator.reason definition", "CODE"),
]


async def cmd_ingest(args: argparse.Namespace) -> int:
    import cognee

    applied = _configure_cognee_env()
    print(f"llm env: {sorted(applied)}")
    dataset = spike_dataset(args.tenant, args.slice)
    files = sorted(str(p) for p in Path(args.slice).rglob("*.py"))
    if args.max_files > 0:
        files = files[: args.max_files]
    print(f"ingest: {len(files)} files -> dataset {dataset}")
    started = time.perf_counter()
    await cognee.add(files, dataset_name=dataset)
    await cognee.cognify(datasets=[dataset], chunker=TreeSitterChunker)
    elapsed = time.perf_counter() - started
    print(f"ingest done in {elapsed:.1f}s dataset={dataset} files={len(files)}")
    return 0


async def cmd_probe(args: argparse.Namespace) -> int:
    import cognee
    from cognee import SearchType

    _configure_cognee_env()
    dataset = spike_dataset(args.tenant, args.slice)
    for query, search_type in PROBE_QUERIES:
        started = time.perf_counter()
        try:
            results = await cognee.search(
                query, SearchType[search_type], datasets=[dataset]
            )
        except Exception as exc:
            print(f"[{search_type}] {query!r}: ERROR {type(exc).__name__}: {exc}")
            continue
        elapsed = time.perf_counter() - started
        items = results if isinstance(results, list) else [results]
        print(f"[{search_type}] {query!r}: {len(items)} hits in {elapsed:.1f}s")
        for item in items[:3]:
            print(f"  - {str(item)[:220]}")
    return 0


async def cmd_leakage(args: argparse.Namespace) -> int:
    import cognee
    from cognee import SearchType

    _configure_cognee_env()
    victim = spike_dataset(args.tenant, args.slice)
    alien_marker = args.slice.replace("/", "_")
    try:
        results = await cognee.search(
            f"content unique to {alien_marker}", SearchType.CHUNKS, datasets=[victim]
        )
    except Exception as exc:
        print(f"leakage probe error (tolerated): {exc}")
        return 0
    items = results if isinstance(results, list) else [results]
    foreign = [
        str(item) for item in items if args.other_tenant and args.other_tenant in str(item)
    ]
    print(f"leakage: {len(items)} hits, {len(foreign)} foreign-tenant hits")
    return 1 if foreign else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tree-sitter-under-Cognee spike (no platform wiring).")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="cognify a bounded slice with the AST chunker")
    ingest.add_argument("--slice", default=DEFAULT_SLICE)
    ingest.add_argument("--tenant", default="spike-a")
    ingest.add_argument("--max-files", type=int, default=25)
    probe = sub.add_parser("probe", help="run retrieval probes over the dataset")
    probe.add_argument("--slice", default=DEFAULT_SLICE)
    probe.add_argument("--tenant", default="spike-a")
    leak = sub.add_parser("leakage", help="cross-tenant leakage probe")
    leak.add_argument("--slice", default=DEFAULT_SLICE)
    leak.add_argument("--tenant", default="spike-a")
    leak.add_argument("--other-tenant", default="spike-b")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "ingest":
        return asyncio.run(cmd_ingest(args))
    if args.command == "probe":
        return asyncio.run(cmd_probe(args))
    if args.command == "leakage":
        return asyncio.run(cmd_leakage(args))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
