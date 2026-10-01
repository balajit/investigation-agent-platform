# src/investigation_agent_platform/application/intake/csv_adapter.py
"""CSV batch adapter (Part 11.9).

Parses CSV text into validated `BatchIntakeRecord` rows using an explicit
field mapping. The mapping names every consumed column; unmapped columns
are ignored (never smuggled into parameters). Workflow inputs stay
structured JSON — CSV never travels past this adapter.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from investigation_agent_platform.domain.intake.batch import (
    MAX_BATCH_RECORDS,
    BatchIntakeRecord,
)

#: Maximum CSV text size (bytes); larger uploads are rejected pre-parse.
MAX_CSV_BYTES = 1_000_000

_REQUIRED_MAPPINGS = ("external_key", "application_id", "problem_description")
_OPTIONAL_MAPPINGS = ("session_id", "priority")


def parse_csv_batch(text: str, field_mapping: dict[str, Any]) -> list[BatchIntakeRecord]:
    """Parse CSV rows into intake records via an explicit field mapping.

    `field_mapping` example::

        {
            "external_key": "ticket_id",
            "application_id": "app",
            "problem_description": "summary",
            "session_id": "session",          # optional
            "priority": "prio",               # optional
            "parameters": {"region": "region_col", "tier": "tier_col"},
        }
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("CSV text must not be empty")
    if len(text.encode()) > MAX_CSV_BYTES:
        raise ValueError("CSV text exceeds the 1MB limit")
    if not isinstance(field_mapping, dict):
        raise TypeError("field_mapping must be an object")
    for required in _REQUIRED_MAPPINGS:
        mapped = field_mapping.get(required)
        if not isinstance(mapped, str) or not mapped:
            raise ValueError(f"field_mapping.{required} must name a column")
    parameters_map = field_mapping.get("parameters", {})
    if not isinstance(parameters_map, dict):
        raise TypeError("field_mapping.parameters must be an object")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header row")
    columns = set(reader.fieldnames)
    for name in (
        [str(field_mapping[key]) for key in _REQUIRED_MAPPINGS]
        + [str(field_mapping[key]) for key in _OPTIONAL_MAPPINGS if field_mapping.get(key)]
        + [str(column) for column in parameters_map.values()]
    ):
        if name not in columns:
            raise ValueError(f"Mapped column {name!r} is not in the CSV header")
    records: list[BatchIntakeRecord] = []
    for line_number, row in enumerate(reader, start=2):
        if len(records) >= MAX_BATCH_RECORDS:
            raise ValueError(f"CSV exceeds the {MAX_BATCH_RECORDS}-record cap")
        if all((value or "").strip() == "" for value in row.values()):
            continue  # skip blank lines
        parameters = {
            name: (row[column] or "") for name, column in parameters_map.items() if row.get(column)
        }
        try:
            records.append(
                BatchIntakeRecord(
                    external_key=(row[str(field_mapping["external_key"])] or "").strip(),
                    application_id=(row[str(field_mapping["application_id"])] or "").strip(),
                    problem_description=(row[str(field_mapping["problem_description"])] or ""),
                    session_id=(row.get(str(field_mapping.get("session_id", ""))) or None),
                    priority=(row.get(str(field_mapping.get("priority", ""))) or "NORMAL"),
                    parameters=parameters,
                )
            )
        except Exception as exc:
            raise ValueError(f"CSV line {line_number}: {exc}") from exc
    if not records:
        raise ValueError("CSV contains no data rows")
    return records
