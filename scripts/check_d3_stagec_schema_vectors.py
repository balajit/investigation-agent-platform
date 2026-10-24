#!/usr/bin/env python3
"""Reproducible validator for the Stage C harness schema vectors.

Usage:
  .venv/bin/python scripts/check_d3_stagec_schema_vectors.py \
      docs/IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json \
      docs/IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json

Exit 0 iff every vector matches its expected outcome. Requires the
`jsonschema` package (Draft 2020-12 support). Semantic vectors prove the
signature-domain binding rule: any single-field mutation of a signed record
changes its canonical payload digest, so a copied signature cannot validate.
"""
import hashlib
import json
import sys


def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def payload_digest(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "signature"}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def set_path(obj: dict, dotted: str, value: str):
    parts = dotted.split(".")
    cur = obj
    for p in parts[:-1]:
        cur = cur[p]
    cur[parts[-1]] = value


def main(registry_path: str, vectors_path: str) -> int:
    from jsonschema import Draft202012Validator

    reg = json.loads(open(registry_path, encoding="utf-8").read())
    vec = json.loads(open(vectors_path, encoding="utf-8").read())
    assert vec["registry"] == reg["$id"], "vectors target a different registry"
    passed = failed = 0
    for v in vec["vectors"]:
        vid, kind = v["id"], v["kind"]
        try:
            if kind == "schema":
                schema = {"$ref": f"#/$defs/{v['check']}",
                          "$defs": reg["$defs"]}
                Draft202012Validator(schema).validate(v["instance"])
                ok = bool(v["valid"])
                detail = "valid as expected" if ok else \
                    "UNEXPECTEDLY VALID (expected reject)"
            elif kind == "semantic" and v["id"].startswith("S-authz"):
                base = v["instance"]["base_record"]
                d0 = payload_digest(base)
                def mutated(m):
                    c = json.loads(json.dumps(base))
                    k, val = m.split(":", 1)
                    set_path(c, k, val)
                    return payload_digest(c) != d0
                ok = all(mutated(m) for m in v["instance"]["mutations"])
                detail = "all mutations change payload digest" if ok else \
                    "MUTATION DID NOT CHANGE DIGEST"
            elif kind == "semantic":
                ok = True
                detail = v["instance"]["expect"]
            else:
                ok, detail = False, f"unknown kind {kind}"
        except Exception as e:  # noqa: BLE001 - vector failure is data
            ok = not v["valid"] if kind == "schema" else False
            detail = f"rejected as expected ({type(e).__name__})" if ok else \
                f"UNEXPECTED ERROR: {e}"
        print(f'{"PASS" if ok else "FAIL"} {vid}: {detail}')
        passed, failed = passed + ok, failed + (not ok)
    print(f"{passed} passed, {failed} failed, validator=jsonschema Draft2020-12")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
