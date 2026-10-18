# tests/unit/test_enhance_logs.py
"""IAP-side enhance-logs flow: predicates, discovery, exemplar enrich (no MCP, no DB)."""

import importlib.util
from pathlib import Path


def _mod():
    path = Path(__file__).resolve().parents[2] / "scripts" / "enhance-logs.py"
    spec = importlib.util.spec_from_file_location("enhance_logs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parsed(raw, record=None):
    from investigation_agent_platform.application.compression.logparse import ParsedLine

    return ParsedLine(
        seq=0,
        source_file="f.log",
        timestamp=None,
        record=record,
        raw=raw,
        ids=frozenset(),
    )


def test_is_signal_levels_and_tokens():
    mod = _mod()
    assert mod.is_signal(_parsed("x", {"level": "ERROR"})) is True
    assert mod.is_signal(_parsed("x", {"level": "WARNING"})) is True
    assert mod.is_signal(_parsed("conclusion saved", {"level": "INFO"})) is True
    assert mod.is_signal(_parsed("GET /x HTTP/1.1 200", {"level": "INFO"})) is False
    assert mod.is_signal(_parsed("plain line")) is False


def test_contains_id_parsed_and_raw():
    mod = _mod()
    uid = "34df140f-1bf3-4193-8f52-5e2fcbd8c3fc"
    assert mod.contains_id(_parsed(f"tail {uid}"), uid) is True
    assert mod.contains_id(_parsed("nothing here"), uid) is False


def test_enrich_exemplar_meaningful_vs_ref():
    mod = _mod()
    from investigation_agent_platform.application.compression.logparse import Exemplar

    uid = "34df140f-1bf3-4193-8f52-5e2fcbd8c3fc"
    first = _parsed("first", {"message": "start", "level": "INFO"})
    last = _parsed("last", {"message": "end", "level": "INFO"})
    exemplar = Exemplar(signature=("a", "b", "INFO"), count=7, first=first, last=last)
    resolutions = {uid: {"id": uid, "status": "COMPLETED"}}
    full = mod.enrich_exemplar(exemplar, resolutions, {uid}, True)
    assert full["resolved"][uid]["status"] == "COMPLETED"
    assert full["refs"] == []
    assert full["dedup"]["count"] == 7
    compact = mod.enrich_exemplar(exemplar, resolutions, {uid}, False)
    assert compact["resolved"] == {}
    assert compact["refs"] == [uid]


def test_discover_files_mtime_order(tmp_path):
    mod = _mod()
    import time

    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.write_text("x\n", encoding="utf-8")
    time.sleep(0.02)
    b.write_text("y\n", encoding="utf-8")
    found = mod.discover_files([str(tmp_path)])
    assert [Path(p).name for p in found] == ["a.log", "b.log"]
    assert mod.discover_files([str(tmp_path / "missing")]) == []


def test_parse_result_text_json_and_python_literal():
    mod = _mod()
    assert mod._parse_result_text('[{"a": 1}]') == [{"a": 1}]
    assert mod._parse_result_text("[{'a': 1}]") == [{"a": 1}]
    assert mod._parse_result_text("not a payload") is None


def test_investigation_node_shape():
    mod = _mod()
    assert mod.investigation_node("abc") == "investigation:abc"
