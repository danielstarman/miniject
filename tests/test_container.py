"""Tests for container resolution, scopes, and lifetimes."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from miniject import Container, ResolutionError


class _Database:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path


class _Repo:
    def __init__(self, database: _Database) -> None:
        self.database = database


class _Service:
    def __init__(self, repo: _Repo, database: _Database) -> None:
        self.repo = repo
        self.database = database


class _ServiceWithDefault:
    def __init__(self, database: _Database, timeout: int = 30) -> None:
        self.database = database
        self.timeout = timeout


class _TimeoutSettings:
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds


class _ServiceWithSettings:
    def __init__(self, database: _Database, settings: _TimeoutSettings) -> None:
        self.database = database
        self.settings = settings


class _ServiceWithOptionalDatabaseDefault:
    def __init__(self, database: _Database | None = None) -> None:
        self.database = database


class _ServiceWithOptionalDatabaseRequired:
    def __init__(self, database: _Database | None) -> None:
        self.database = database


class _CircularA:
    def __init__(self, b: _CircularB) -> None:
        self.b = b


class _CircularB:
    def __init__(self, a: _CircularA) -> None:
        self.a = a


def test_resolve_bound_instance_returns_same_instance() -> None:
    # Arrange
    c = Container()
    db = _Database("test.db")
    c.bind(_Database, instance=db)

    # Act
    resolved = c.resolve(_Database)

    # Assert
    assert resolved is db
    assert resolved.path == "test.db"


def test_resolve_instance_binding_returns_same_instance_each_time() -> None:
    # Arrange
    c = Container()
    db = _Database()
    c.bind(_Database, instance=db)

    # Act + Assert
    assert c.resolve(_Database) is c.resolve(_Database)


def test_bind_rebound_singleton_binding_replaces_cached_instance() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, instance=_Database("/first"))
    c.bind(_Database, factory=lambda: _Database("/second"), singleton=True)

    # Act + Assert
    assert c.resolve(_Database).path == "/second"


def test_resolve_autowired_transient_binding_returns_injected_instance() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_Repo)

    # Act
    repo = c.resolve(_Repo)

    # Assert
    assert isinstance(repo, _Repo)
    assert isinstance(repo.database, _Database)


def test_resolve_autowired_transient_binding_returns_new_instance_each_time() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_Repo)

    # Act
    r1 = c.resolve(_Repo)
    r2 = c.resolve(_Repo)

    # Assert
    assert r1 is not r2
    assert r1.database is r2.database


def test_resolve_autowired_deep_dependency_chain_returns_shared_dependency_graph() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_Repo)
    c.bind(_Service)

    # Act
    svc = c.resolve(_Service)

    # Assert
    assert isinstance(svc.repo, _Repo)
    assert isinstance(svc.database, _Database)
    assert svc.repo.database is svc.database


def test_resolve_transient_factory_binding_returns_factory_result() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, factory=lambda: _Database("/custom"))

    # Act
    db = c.resolve(_Database)

    # Assert
    assert db.path == "/custom"


def test_resolve_singleton_factory_binding_returns_cached_instance() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, factory=lambda: _Database("/singleton"), singleton=True)

    # Act
    d1 = c.resolve(_Database)
    d2 = c.resolve(_Database)

    # Assert
    assert d1 is d2
    assert d1.path == "/singleton"


def test_resolve_singleton_factory_binding_with_overrides_raises_resolution_error() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, instance=_Database("/bound"))
    c.bind(_Repo, singleton=True)

    # Act + Assert
    with pytest.raises(
        ResolutionError,
        match="overrides are not supported for singleton bindings",
    ):
        c.resolve(_Repo, database=_Database("/override"))


def test_resolve_instance_binding_with_overrides_raises_resolution_error() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, instance=_Database("/bound"))

    # Act + Assert
    with pytest.raises(
        ResolutionError,
        match="overrides are not supported for singleton bindings",
    ):
        c.resolve(_Database, path="/override")


def _repo_factory(database: _Database) -> _Repo:
    return _Repo(database)


def test_resolve_factory_binding_with_dependencies_injects_factory_arguments() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_Repo, factory=_repo_factory)

    # Act
    repo = c.resolve(_Repo)

    # Assert
    assert isinstance(repo.database, _Database)


async def _async_database_factory() -> _Database:
    await asyncio.sleep(0)
    return _Database("/async")


async def _async_repo_factory(database: _Database) -> _Repo:
    await asyncio.sleep(0)
    return _Repo(database)


def test_resolve_async_factory_binding_raises_resolution_error() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, factory=_async_database_factory)

    # Act + Assert
    with pytest.raises(
        ResolutionError,
        match=r"Cannot resolve _Database: factory '_async_database_factory' is async; "
        r"use resolve_async\(\) \(_Database\)",
    ):
        c.resolve(_Database)


def test_resolve_indirect_async_dependency_includes_resolution_chain() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, factory=_async_database_factory)
    c.bind(_Repo)

    # Act + Assert
    with pytest.raises(
        ResolutionError,
        match=r"Cannot resolve _Database: factory '_async_database_factory' is async; "
        r"use resolve_async\(\) \(_Repo -> _Database\)",
    ):
        c.resolve(_Repo)


def test_resolve_async_async_factory_binding_returns_factory_result() -> None:
    async def _run() -> None:
        # Arrange
        c = Container()
        c.bind(_Database, factory=_async_database_factory)

        # Act
        db = await c.resolve_async(_Database)

        # Assert
        assert db.path == "/async"

    asyncio.run(_run())


def test_resolve_async_async_factory_dependencies_awaits_nested_factories() -> None:
    async def _run() -> None:
        # Arrange
        c = Container()
        c.bind(_Database, factory=_async_database_factory)
        c.bind(_Repo, factory=_async_repo_factory)

        # Act
        repo = await c.resolve_async(_Repo)

        # Assert
        assert repo.database.path == "/async"

    asyncio.run(_run())


def test_resolve_async_autowired_sync_type_with_async_dependency_returns_instance() -> None:
    async def _run() -> None:
        # Arrange
        c = Container()
        c.bind(_Database, factory=_async_database_factory)
        c.bind(_Repo)

        # Act
        repo = await c.resolve_async(_Repo)

        # Assert
        assert repo.database.path == "/async"

    asyncio.run(_run())


def test_resolve_async_concurrent_singleton_factory_initializes_once() -> None:
    async def _run() -> None:
        # Arrange
        call_count = 0
        count_lock = Lock()

        async def _factory() -> _Database:
            nonlocal call_count
            await asyncio.sleep(0)
            with count_lock:
                call_count += 1
            return _Database("/shared-async")

        c = Container()
        c.bind(_Database, factory=_factory, singleton=True)

        # Act
        results = await asyncio.gather(*(c.resolve_async(_Database) for _ in range(8)))

        # Assert
        assert all(result is results[0] for result in results)
        assert call_count == 1

    asyncio.run(_run())


def test_resolve_async_sync_factory_with_overrides_returns_override_result() -> None:
    async def _run() -> None:
        # Arrange
        def _factory(path: str = "/custom") -> _Database:
            return _Database(path)

        c = Container()
        c.bind(_Database, factory=_factory)

        # Act
        db = await c.resolve_async(_Database, path="/override")

        # Assert
        assert db.path == "/override"

    asyncio.run(_run())


def test_resolve_async_singleton_binding_with_overrides_raises_resolution_error() -> None:
    async def _run() -> None:
        # Arrange
        c = Container()
        c.bind(_Database, factory=_async_database_factory, singleton=True)

        # Act + Assert
        with pytest.raises(
            ResolutionError,
            match="overrides are not supported for singleton bindings",
        ):
            await c.resolve_async(_Database, path="/override")

    asyncio.run(_run())


def test_resolve_async_child_scope_with_parent_bindings_returns_parent_dependency() -> None:
    async def _run() -> None:
        # Arrange
        parent = Container()
        parent.bind(_Database, factory=_async_database_factory)
        parent.bind(_Repo)

        child = parent.scope()

        # Act
        repo = await child.resolve_async(_Repo)

        # Assert
        assert repo.database.path == "/async"

    asyncio.run(_run())


def test_resolve_async_child_scope_override_keeps_parent_binding_unchanged() -> None:
    async def _run() -> None:
        # Arrange
        parent = Container()
        parent.bind(_Database, instance=_Database("/parent"))

        child = parent.scope()
        child.bind(_Database, factory=_async_database_factory)

        # Act
        parent_db = parent.resolve(_Database)
        child_db = await child.resolve_async(_Database)

        # Assert
        assert parent_db.path == "/parent"
        assert child_db.path == "/async"

    asyncio.run(_run())


def test_resolve_async_parent_singleton_binding_across_children_returns_shared_instance() -> None:
    async def _run() -> None:
        # Arrange
        call_count = 0
        count_lock = Lock()

        async def _factory() -> _Database:
            nonlocal call_count
            await asyncio.sleep(0)
            with count_lock:
                call_count += 1
            return _Database("/shared-async")

        parent = Container()
        parent.bind(_Database, factory=_factory, singleton=True)
        children = [parent.scope() for _ in range(8)]

        # Act
        results = await asyncio.gather(*(child.resolve_async(_Database) for child in children))

        # Assert
        assert all(result is results[0] for result in results)
        assert call_count == 1

    asyncio.run(_run())


def test_resolve_async_parent_singleton_first_resolved_in_child_ignores_child_override() -> None:
    async def _run() -> None:
        # Arrange
        parent = Container()
        parent.bind(_Database, instance=_Database("/parent"))
        parent.bind(_Repo, factory=_async_repo_factory, singleton=True)

        child = parent.scope()
        child.bind(_Database, instance=_Database("/child"))

        # Act
        child_repo = await child.resolve_async(_Repo)
        parent_repo = await parent.resolve_async(_Repo)

        # Assert
        assert child_repo is parent_repo
        assert child_repo.database.path == "/parent"
        assert parent_repo.database.path == "/parent"

    asyncio.run(_run())


def test_resolve_async_circular_dependency_graph_raises_resolution_error() -> None:
    async def _run() -> None:
        # Arrange
        c = Container()
        c.bind(_CircularA)
        c.bind(_CircularB)

        # Act + Assert
        with pytest.raises(ResolutionError, match="Circular dependency"):
            await c.resolve_async(_CircularA)

    asyncio.run(_run())


def test_scope_child_container_inherits_parent_bindings() -> None:
    # Arrange
    parent = Container()
    parent.bind(_Database, instance=_Database("/parent"))
    parent.bind(_Repo)

    child = parent.scope()

    # Act
    repo = child.resolve(_Repo)

    # Assert
    assert repo.database.path == "/parent"


def test_scope_child_override_keeps_parent_binding_unchanged() -> None:
    # Arrange
    parent = Container()
    parent.bind(_Database, instance=_Database("/parent"))

    child = parent.scope()
    child.bind(_Database, instance=_Database("/child"))

    # Act + Assert
    assert parent.resolve(_Database).path == "/parent"
    assert child.resolve(_Database).path == "/child"


def test_resolve_child_scope_override_used_for_autowiring() -> None:
    # Arrange
    parent = Container()
    parent.bind(_Database, instance=_Database("/parent"))
    parent.bind(_Repo)

    child = parent.scope()
    child.bind(_Database, instance=_Database("/child"))

    # Act
    parent_repo = parent.resolve(_Repo)
    child_repo = child.resolve(_Repo)

    # Assert
    assert parent_repo.database.path == "/parent"
    assert child_repo.database.path == "/child"


def test_resolve_parent_singleton_binding_through_child_returns_shared_instance() -> None:
    # Arrange
    parent = Container()
    parent.bind(_Database, factory=lambda: _Database("/shared"), singleton=True)

    child = parent.scope()

    # Act
    parent_db = parent.resolve(_Database)
    child_db = child.resolve(_Database)

    # Assert
    assert parent_db is child_db


def test_resolve_child_service_with_parent_singleton_dependency_reuses_parent_singleton() -> None:
    # Arrange
    parent = Container()
    parent.bind(_Database, factory=lambda: _Database("/shared"), singleton=True)
    parent.bind(_Repo)

    child = parent.scope()

    # Act
    parent_repo = parent.resolve(_Repo)
    child_repo = child.resolve(_Repo)

    # Assert
    assert parent_repo.database is child_repo.database


def test_resolve_parent_singleton_first_resolved_in_child_ignores_child_override() -> None:
    # Arrange
    parent = Container()
    parent.bind(_Database, instance=_Database("/parent"))
    parent.bind(_Repo, singleton=True)

    child = parent.scope()
    child.bind(_Database, instance=_Database("/child"))

    # Act
    child_repo = child.resolve(_Repo)
    parent_repo = parent.resolve(_Repo)

    # Assert
    assert child_repo is parent_repo
    assert child_repo.database.path == "/parent"
    assert parent_repo.database.path == "/parent"


def test_resolve_concurrent_singleton_factory_initializes_once() -> None:
    # Arrange
    start_barrier = Barrier(8)
    call_count = 0
    count_lock = Lock()

    def _factory() -> _Database:
        nonlocal call_count
        with count_lock:
            call_count += 1
        return _Database("/shared")

    c = Container()
    c.bind(_Database, factory=_factory, singleton=True)

    def _resolve(_: int) -> _Database:
        start_barrier.wait()
        return c.resolve(_Database)

    # Act
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_resolve, range(8)))

    # Assert
    assert all(result is results[0] for result in results)
    assert call_count == 1


def test_resolve_concurrent_child_scope_access_to_parent_singleton_initializes_once() -> None:
    # Arrange
    start_barrier = Barrier(8)
    call_count = 0
    count_lock = Lock()

    def _factory() -> _Database:
        nonlocal call_count
        with count_lock:
            call_count += 1
        return _Database("/shared")

    parent = Container()
    parent.bind(_Database, factory=_factory, singleton=True)
    children = [parent.scope() for _ in range(8)]

    def _resolve(child: Container) -> _Database:
        start_barrier.wait()
        return child.resolve(_Database)

    # Act
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_resolve, children))

    # Assert
    assert all(result is results[0] for result in results)
    assert call_count == 1


def test_resolve_unbound_parameter_with_default_uses_default_value() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_ServiceWithDefault)

    # Act
    svc = c.resolve(_ServiceWithDefault)

    # Assert
    assert svc.timeout == 30


def test_resolve_typed_value_object_binding_injects_bound_instance() -> None:
    # Arrange
    c = Container()
    settings = _TimeoutSettings(99)
    c.bind(_Database, instance=_Database())
    c.bind(_TimeoutSettings, instance=settings)
    c.bind(_ServiceWithSettings)

    # Act
    svc = c.resolve(_ServiceWithSettings)

    # Assert
    assert svc.settings is settings
    assert svc.settings.seconds == 99


def test_resolve_optional_binding_with_bound_value_overrides_none_default() -> None:
    # Arrange
    c = Container()
    db = _Database("/bound")
    c.bind(_Database, instance=db)
    c.bind(_ServiceWithOptionalDatabaseDefault)

    # Act
    svc = c.resolve(_ServiceWithOptionalDatabaseDefault)

    # Assert
    assert svc.database is db


def test_resolve_optional_binding_without_binding_uses_none_default() -> None:
    # Arrange
    c = Container()
    c.bind(_ServiceWithOptionalDatabaseDefault)

    # Act
    svc = c.resolve(_ServiceWithOptionalDatabaseDefault)

    # Assert
    assert svc.database is None


def test_resolve_required_optional_binding_without_binding_raises_resolution_error() -> None:
    # Arrange
    c = Container()
    c.bind(_ServiceWithOptionalDatabaseRequired)

    # Act + Assert
    with pytest.raises(ResolutionError, match="missing binding for parameter 'database'"):
        c.resolve(_ServiceWithOptionalDatabaseRequired)


def test_resolve_bound_none_instance_returns_none() -> None:
    """None is a valid instance value and should not fall through to auto-wiring."""

    # Arrange
    c = Container()
    c.bind(type(None), instance=None)

    # Act + Assert
    assert c.resolve(type(None)) is None


def test_resolve_unbound_service_raises_resolution_error() -> None:
    # Arrange
    c = Container()

    # Act + Assert
    with pytest.raises(ResolutionError, match="no binding"):
        c.resolve(_Database)


def test_resolve_missing_dependency_includes_dependency_chain() -> None:
    # Arrange
    c = Container()
    c.bind(_Repo)

    # Act + Assert
    with pytest.raises(ResolutionError, match="_Database"):
        c.resolve(_Repo)


def test_resolve_circular_dependency_graph_raises_resolution_error() -> None:
    # Arrange
    c = Container()
    c.bind(_CircularA)
    c.bind(_CircularB)

    # Act + Assert
    with pytest.raises(ResolutionError, match="Circular dependency"):
        c.resolve(_CircularA)
