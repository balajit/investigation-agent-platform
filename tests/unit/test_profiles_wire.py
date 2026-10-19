# tests/unit/test_profiles_wire.py
"""Golden wire tests for the profiles router (OpenAPI spec 1.0).

Each test pins the exact response body: field-for-field equality against a
literal. Any serialization change (aliases, nulls, key presence) fails here
first. Redaction tests prove secrets never cross the wire.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context

HEADERS = {"X-Tenant-ID": "tenant-a"}


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _client():
    ctx = AppContext()
    set_app_context(ctx)
    return ctx, TestClient(create_app())


def _body():
    return {
        "id": "svc-a",
        "name": "Service A",
        "description": "contract test profile",
        "environment": "production",
        "owner": "team-a",
        "mapping_source_id": "generic-ecs",
        "services": [
            {
                "name": "svc-a",
                "code": {"org": "acme", "repos": ["svc-a"], "source_roots": ["src"]},
                "telemetry": {"log_indices": ["logs-a-*"], "service_names": ["svc-a"]},
            }
        ],
        "datasources": [
            {
                "id": "primary",
                "name": "db",
                "kind": "postgres",
                "role": "state",
                "allowed_tables": ["t"],
                "query_templates": {
                    "find": {
                        "description": "find",
                        "sql": "SELECT * FROM t WHERE id = :id",
                        "allowed_tables": ["t"],
                    }
                },
            }
        ],
    }


def _expected_sanitized():
    return {
        "allowed_capabilities": [],
        "correlation": {"fields": [], "priority": []},
        "data_classification": None,
        "datasources": [
            {
                "allowed_tables": ["t"],
                "connection_ref": None,
                "description": None,
                "dynamodb": None,
                "file_log": None,
                "id": "primary",
                "kafka": None,
                "kind": "postgres",
                "mcp_server": None,
                "name": "db",
                "query_templates": {"find": {"description": "find"}},
                "read_only": True,
                "redis": None,
                "role": "state",
                "s3": None,
                "schema_name": None,
                "tenants": [],
            "relationships": [],
            "session_entity": None,
            "session_key": None,
            }
        ],
        "derivation": None,
        "description": "contract test profile",
        "domain_hubs": [],
        "expectations": {"endpoints": []},
        "domain": None,
        "environment": "production",
        "environments": [],
        "id": "svc-a",
        "investigation": {
            "correlationDepth": 2,
            "defaultTimeWindow": 3600,
            "enabledEvidenceTypes": ["LOG", "TRACE", "DATABASE_STATE", "COMMIT_HISTORY"],
            "evidence_budget": None,
            "maximumEvidencePerQuery": 50,
            "maximumHypotheses": 5,
            "maximumInvestigationDuration": 7200,
            "parametersSchema": None,
        },
        "knowledge": {
            "enable_cross_tenant_fingerprints": False,
            "min_confidence": 0.7,
            "retention_days": 30,
            "reuse_scope": "tenant_app",
        },
        "mapping_source_id": "generic-ecs",
        "name": "Service A",
        "oncall": None,
        "owner": "team-a",
        "owners": [],
        "planes": [],
        "referenceDocs": None,
        "runbooks": [],
        "services": [
            {
                "code": {
                    "build_system": None,
                    "default_branch": "main",
                    "language": None,
                    "module_structure": None,
                    "org": "acme",
                    "paths": [],
                    "repos": ["svc-a"],
                    "source_roots": ["src"],
                },
                "dependencies": [],
                "description": None,
                "endpoints": [],
                "name": "svc-a",
                "probeable_endpoints": [],
                "repository_id": None,
                "role": None,
                "telemetry": {
                    "default_lookback_s": None,
                    "log_indices": ["logs-a-*"],
                    "metrics_backend": None,
                    "service_names": ["svc-a"],
                    "session_key": None,
                    "tracking_key": None,
                    "trace_backend": None,
                },
            }
        ],
        "slos": [],
        "status": "DRAFT",
        "tags": [],
        "tenant_id": "tenant-a",
        "version": 1,
        "source": None,
    }


class TestProfilesGolden:
    def test_create_body_exact(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        assert resp.status_code == 201
        assert resp.json() == {"profile": _expected_sanitized(), "version": 1}

    def test_create_replay_adds_only_flag(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        resp = client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {
            "profile": _expected_sanitized(),
            "version": 1,
            "idempotent_replay": True,
        }

    def test_detail_and_list_shapes(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        detail = client.get("/api/v1/profiles/svc-a?version=1", headers=HEADERS)
        assert detail.json() == {"profile": _expected_sanitized(), "version": 1}
        listed = client.get("/api/v1/profiles", headers=HEADERS).json()
        svc = next(i for i in listed["items"] if i["id"] == "svc-a")
        assert svc == _expected_sanitized()
        assert listed["total"] == len(listed["items"])

    def test_revisions_shape(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        resp = client.get("/api/v1/profiles/svc-a/revisions", headers=HEADERS)
        assert resp.json() == {
            "items": [
                {
                    "id": "svc-a",
                    "version": 1,
                    "status": "DRAFT",
                    "name": "Service A",
                    "environment": "production",
                }
            ],
            "total": 1,
        }

    def test_update_bumps_version_same_body(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        body = _body()
        body["status"] = "ACTIVE"
        resp = client.put("/api/v1/profiles/svc-a", json=body, headers=HEADERS)
        assert resp.status_code == 200
        payload = resp.json()
        assert set(payload) == {"profile", "version"}
        assert payload["version"] == 2
        assert payload["profile"]["status"] == "ACTIVE"
        assert payload["profile"]["datasources"][0]["query_templates"] == {
            "find": {"description": "find"}
        }


class TestProfilesRedaction:
    def test_view_never_emits_secrets(self) -> None:
        from investigation_agent_platform.api.v1.schemas.profiles import ProfileView
        from investigation_agent_platform.domain.profile.models import ApplicationProfile

        body = _body()
        body["tenant_id"] = "tenant-a"
        body["datasources"][0]["connection_ref"] = "secret://db/main"
        profile = ApplicationProfile.model_validate(body)
        dumped = ProfileView.from_domain(profile).model_dump(mode="json", by_alias=True)
        text = __import__("json").dumps(dumped)
        assert "secret://db/main" not in text
        assert "SELECT * FROM t" not in text
        ds = dumped["datasources"][0]
        assert ds["connection_ref"] == {"present": True, "scheme": "secret"}
        assert ds["query_templates"] == {"find": {"description": "find"}}

    def test_view_rejects_widening(self) -> None:
        from pydantic import ValidationError

        from investigation_agent_platform.api.v1.schemas.profiles import ProfileView
        from investigation_agent_platform.domain.profile.models import ApplicationProfile

        profile = ApplicationProfile.model_validate({**_body(), "tenant_id": "tenant-a"})
        raw = profile.sanitized()
        raw["services"][0]["smuggled"] = "x"
        with pytest.raises(ValidationError):
            ProfileView.model_validate(raw)


class TestProfilesErrorEnvelope:
    def test_404_body_and_correlation(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get("/api/v1/profiles/nope", headers=HEADERS)
        assert resp.status_code == 404
        body = resp.json()["error"]
        assert body["code"] == "PROFILE_NOT_FOUND"
        assert body["message"]
        assert body["correlation_id"]
        assert resp.headers.get("x-correlation-id") == body["correlation_id"]
        assert "details" not in body

    def test_400_schema_error_shape(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post("/api/v1/profiles", json={"id": "x"}, headers=HEADERS)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "PROFILE_SCHEMA_INVALID"


class TestTemplateDropGuard:
    def test_drop_without_confirm_409(self, _restore_context: None) -> None:
        _, client = _client()
        assert client.post("/api/v1/profiles", json=_body(), headers=HEADERS).status_code == 201
        dropped = _body()
        dropped["datasources"][0].pop("query_templates")
        resp = client.put("/api/v1/profiles/svc-a", json=dropped, headers=HEADERS)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PROFILE_TEMPLATES_DROPPED"

    def test_drop_with_confirm_200(self, _restore_context: None) -> None:
        _, client = _client()
        assert client.post("/api/v1/profiles", json=_body(), headers=HEADERS).status_code == 201
        dropped = _body()
        dropped["datasources"][0].pop("query_templates")
        dropped["confirm_template_removal"] = True
        resp = client.put("/api/v1/profiles/svc-a", json=dropped, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["version"] == 2


class TestGenerateMinimalContract:
    def test_minimal_contract_validation(self, _restore_context: None) -> None:
        _, client = _client()
        # Missing app_name.
        resp = client.post(
            "/api/v1/profiles:generate",
            json={"repo": "github.com/acme/svc"},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        # Bad repo ref.
        resp = client.post(
            "/api/v1/profiles:generate",
            json={"app_name": "Svc", "repo": "git@github.com:acme/svc.git"},
            headers=HEADERS,
        )
        assert resp.status_code == 400

    def test_generate_from_local_checkout(
        self, _restore_context: None, tmp_path, monkeypatch
    ) -> None:
        import investigation_agent_platform.integrations.profile_gen.generate as gen

        (tmp_path / "pom.xml").write_text("<project/>")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "A.java").write_text(
            "package a;\nimport org.springframework.web.bind.annotation.*;\n"
            "@RestController\n@RequestMapping(\"api\")\npublic class A {\n"
            '    @GetMapping("/ping")\n    public String ping() { return "ok"; }\n}\n'
        )
        monkeypatch.setattr(
            gen, "clone_repo", lambda url, branch="": tmp_path
        )
        # list_files on tmp_path directly.
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles:generate",
            json={"app_name": "Acme Svc", "repo": "github.com/acme/svc", "persist": False},
            headers=HEADERS,
        )
        assert resp.status_code == 201, resp.text[:300]
        body = resp.json()
        assert body["persisted"] is False
        assert body["derivation"]["repo_type"] == "java-spring"
        endpoints = body["profile"]["services"][0]["probeable_endpoints"]
        assert any(e["path_template"] == "/api/ping" for e in endpoints)

    def test_deprecated_explicit_fields(self, _restore_context: None, tmp_path, monkeypatch) -> None:
        import investigation_agent_platform.integrations.profile_gen.generate as gen

        (tmp_path / "pom.xml").write_text("<project/>")
        monkeypatch.setattr(gen, "clone_repo", lambda url, branch="": tmp_path)
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles:generate",
            json={"tenant_app_id": "legacy-svc", "org": "acme", "repo": "svc", "persist": False},
            headers=HEADERS,
        )
        assert resp.status_code == 201, resp.text[:300]
        assert resp.json()["profile"]["id"] == "legacy-svc"
