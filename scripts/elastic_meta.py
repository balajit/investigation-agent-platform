#!/usr/bin/env python3
"""Elastic index/field metadata explorer (operator tooling).

Auth comes exclusively from the environment — never from source:
  ES_URL       Full base URL, e.g. https://host/es/ (required)
  ES_USER      Basic-auth username (required with ES_PASSWORD)
  ES_PASSWORD  Basic-auth password (required with ES_USER)
  ES_API_KEY   Alternative to basic auth (ARENTES_API_KEY style)
  ES_TIMEOUT   Request timeout seconds (default 30)
  ES_PATTERN   Default index pattern (default "logs-*")

Usage:
    ES_URL=... ES_USER=... ES_PASSWORD=... uv run python scripts/elastic_meta.py --indices
    ES_URL=... ES_API_KEY=... uv run python scripts/elastic_meta.py --columns my-index-0001
    ES_URL=... ES_API_KEY=... uv run python scripts/elastic_meta.py --field-caps "logs-*-*"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from elasticsearch import AsyncElasticsearch


def _client_from_env() -> AsyncElasticsearch:
    url = os.environ.get("ES_URL", "")
    if not url:
        raise SystemExit("error: ES_URL is required")
    timeout = float(os.environ.get("ES_TIMEOUT", "30"))
    api_key = os.environ.get("ES_API_KEY", "")
    user = os.environ.get("ES_USER", "")
    password = os.environ.get("ES_PASSWORD", "")
    if api_key:
        return AsyncElasticsearch(url, api_key=api_key, request_timeout=timeout)
    if user and password:
        return AsyncElasticsearch(url, basic_auth=(user, password), request_timeout=timeout)
    raise SystemExit("error: ES_API_KEY or ES_USER+ES_PASSWORD is required")


class ElasticMetadataService:
    def __init__(self, client: AsyncElasticsearch) -> None:
        self._client = client

    async def aclose(self) -> None:
        await self._client.close()

    async def get_available_indices(self, pattern: str = "logs-*") -> list[dict[str, Any]]:
        """Indices/data streams matching a pattern with doc counts and sizes."""
        try:
            cat_response: Any = await self._client.cat.indices(
                index=pattern, format="json", h=["index", "docs.count", "store.size"]
            )
        except Exception as exc:
            raise RuntimeError(f"cat.indices failed for {pattern!r}: {exc}") from exc
        entries = list(cat_response) if cat_response else []
        return [
            {"index": e.get("index", ""), "docs": e.get("docs.count"), "size": e.get("store.size")}
            for e in entries
            if e.get("index")
        ]

    async def get_index_columns(self, index_name: str) -> dict[str, str]:
        """Flattened field names to types for one concrete index."""
        try:
            mapping_response: Any = await self._client.indices.get_mapping(index=index_name)
        except Exception as exc:
            raise RuntimeError(f"get_mapping failed for {index_name!r}: {exc}") from exc
        fields: dict[str, str] = {}

        def _flatten(properties: dict[str, Any], prefix: str = "") -> None:
            for field, meta in properties.items():
                if not isinstance(meta, dict):
                    continue
                full_key = f"{prefix}.{field}" if prefix else field
                sub = meta.get("properties")
                if isinstance(sub, dict) and sub:
                    _flatten(sub, prefix=full_key)
                else:
                    fields[full_key] = str(meta.get("type", "object"))

        body = dict(mapping_response)
        indices = [index_name] if index_name in body else list(body)
        for name in indices:
            props = body.get(name, {}).get("mappings", {}).get("properties", {})
            if isinstance(props, dict):
                _flatten(props)
        return fields

    async def get_field_caps(self, pattern: str = "logs-*") -> dict[str, str]:
        """Field names to types across all indices matching a pattern."""
        try:
            response: Any = await self._client.field_caps(index=pattern, fields=["*"])
        except Exception as exc:
            raise RuntimeError(f"field_caps failed for {pattern!r}: {exc}") from exc
        out: dict[str, str] = {}
        for field, types in (dict(response).get("fields", {}) or {}).items():
            if isinstance(types, dict) and types:
                out[field] = min(types)
        return out


async def _run(args: argparse.Namespace) -> int:
    service = ElasticMetadataService(_client_from_env())
    try:
        if args.indices is not None:
            rows = await service.get_available_indices(args.indices or os.environ.get("ES_PATTERN", "logs-*"))
            print(json.dumps(rows, indent=2)[:20000])
        elif args.columns:
            print(json.dumps(await service.get_index_columns(args.columns), indent=2)[:20000])
        elif args.field_caps is not None:
            rows = await service.get_field_caps(args.field_caps or "logs-*")
            print(json.dumps(rows, indent=2)[:20000])
        else:
            print("nothing requested (see --help)")
            return 2
    finally:
        await service.aclose()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Explore Elastic indices and fields.")
    p.add_argument("--indices", nargs="?", const="", help="List indices for a pattern")
    p.add_argument("--columns", help="Flattened field map for one index")
    p.add_argument("--field-caps", nargs="?", const="", help="Field caps across a pattern")
    return p


def main() -> None:
    sys.exit(asyncio.run(_run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
