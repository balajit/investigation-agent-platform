"""R12H: the datasource registry never disposes stale versions.

A new profile version disposes superseded engines on resolve, and an
opportunistic bounded sweep reaps expired entries. Unit tests with a
counting engine factory. Written first, red before the fix.
"""

from __future__ import annotations


class _Engine:
    def __init__(self, dsn, disposed):  # type: ignore[no-untyped-def]
        self.dsn = dsn
        self._disposed = disposed
        self.disposed = False
        self.fail_dispose = False

    async def dispose(self):  # type: ignore[no-untyped-def]
        if self.fail_dispose:
            raise RuntimeError("dispose boom")
        self.disposed = True
        self._disposed.append(self.dsn)


def _profile(version):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.profile.models import (
        Datasource,
        DatasourceKind,
        DatasourceRole,
    )

    return {
        "id": "app-1",
        "version": version,
        "datasources": [
            Datasource(
                id="primary",
                name="app db",
                kind=DatasourceKind.POSTGRES,
                role=DatasourceRole.STATE,
                connection_ref="env://IAP_R12H_TEST_DSN",
            )
        ],
    }


class _Repos:
    def __init__(self, versions):  # type: ignore[no-untyped-def]
        self._versions = versions

    async def get_by_application_id(self, tenant_id, application_id, version=None):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        v = version if version is not None else max(self._versions)
        return SimpleNamespace(
            id="app-1", version=v, datasources=_profile(v)["datasources"]
        )


def _registry(repos, disposed, **over):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
        DatasourceRegistry,
    )

    def _factory(dsn):  # type: ignore[no-untyped-def]
        return _Engine(dsn, disposed)

    return DatasourceRegistry(
        repos,
        engine_factory=_factory,
        allow_direct_engines=True,
        environment="test",
        **over,
    )


async def _resolve(registry, version):  # type: ignore[no-untyped-def]
    return await registry.resolve("t", "app-1", "primary", version=version)


def test_r12h_t01_new_version_disposes_old_engine(monkeypatch) -> None:
    import asyncio

    monkeypatch.setenv("IAP_R12H_TEST_DSN", "postgresql://u:p@localhost/db")

    disposed: list = []

    async def go():  # type: ignore[no-untyped-def]
        registry = _registry(_Repos([1, 2]), disposed)
        await _resolve(registry, 1)
        assert len(disposed) == 0
        before = list(registry._cache.keys())
        assert len(before) == 1
        await _resolve(registry, 2)
        assert len(disposed) == 1
        assert [k for k in registry._cache] == [
            k for k in registry._cache if k[3] == 2
        ]
        assert len(registry._cache) == 1

    asyncio.run(go())


def test_r12h_t02_expired_entries_are_swept(monkeypatch) -> None:
    import asyncio
    import time

    monkeypatch.setenv("IAP_R12H_TEST_DSN", "postgresql://u:p@localhost/db")

    from investigation_agent_platform.domain.profile.models import (
        Datasource,
        DatasourceKind,
        DatasourceRole,
    )

    disposed: list = []

    class _Two:
        async def get_by_application_id(self, tenant_id, application_id, version=None):  # type: ignore[no-untyped-def]
            from types import SimpleNamespace

            def _ds(ds_id):  # type: ignore[no-untyped-def]
                return Datasource(
                    id=ds_id,
                    name=ds_id,
                    kind=DatasourceKind.POSTGRES,
                    role=DatasourceRole.STATE,
                    connection_ref="env://IAP_R12H_TEST_DSN",
                )

            return SimpleNamespace(
                id="app-1", version=1, datasources=[_ds("a"), _ds("b")]
            )

    async def go():  # type: ignore[no-untyped-def]
        registry = _registry(_Two(), disposed)
        await registry.resolve("t", "app-1", "a", version=1)
        assert len(disposed) == 0
        # An entry past TTL for a key never re-resolved...
        for key in list(registry._cache.keys()):
            adapter, engine, _exp = registry._cache[key]
            registry._cache[key] = (adapter, engine, time.monotonic() - 1.0)
        # ...is reaped (bounded sweep) when another key resolves.
        await registry.resolve("t", "app-1", "b", version=1)
        assert len(disposed) == 1
        assert sorted(k[2] for k in registry._cache) == ["b"]

    asyncio.run(go())


def test_r12h_t03_disposal_fault_does_not_break_resolution(monkeypatch) -> None:
    import asyncio

    monkeypatch.setenv("IAP_R12H_TEST_DSN", "postgresql://u:p@localhost/db")

    disposed: list = []

    async def go():  # type: ignore[no-untyped-def]
        registry = _registry(_Repos([1, 2]), disposed)
        first = await _resolve(registry, 1)
        assert first is not None
        for _key, (_adapter, engine, _exp) in list(registry._cache.items()):
            engine.fail_dispose = True
        second = await _resolve(registry, 2)
        assert second is not None

    asyncio.run(go())
