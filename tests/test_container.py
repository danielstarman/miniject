"""Tests for the DI container."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from typing import Annotated, Any, cast

import pytest

from miniject import Container, ResolutionError

# ── Test fixtures ────────────────────────────────────────────────────


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


class _AnnotatedService:
    def __init__(self, database: Annotated[_Database, "primary"]) -> None:
        self.database = database


class _MissingRuntimeDependency:
    pass


class _MissingRuntimeHintService:
    def __init__(self, database: _MissingRuntimeDependency) -> None:
        self.database = database


class _UntypedService:
    def __init__(self, dependency) -> None:
        self.dependency = dependency


class _CircularA:
    def __init__(self, b: _CircularB) -> None:
        self.b = b


class _CircularB:
    def __init__(self, a: _CircularA) -> None:
        self.a = a


# ── bind + resolve: instance ─────────────────────────────────────────


def test_bind_instance_and_resolve() -> None:
    c = Container()
    db = _Database("test.db")
    c.bind(_Database, instance=db)

    resolved = c.resolve(_Database)

    assert resolved is db
    assert resolved.path == "test.db"


def test_instance_is_singleton() -> None:
    c = Container()
    db = _Database()
    c.bind(_Database, instance=db)

    assert c.resolve(_Database) is c.resolve(_Database)


def test_rebinding_singleton_replaces_cached_instance() -> None:
    c = Container()
    c.bind(_Database, instance=_Database("/first"))
    c.bind(_Database, factory=lambda: _Database("/second"), singleton=True)

    assert c.resolve(_Database).path == "/second"


# ── bind + resolve: auto-wired ───────────────────────────────────────


def test_auto_wire_transient() -> None:
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_Repo)

    repo = c.resolve(_Repo)

    assert isinstance(repo, _Repo)
    assert isinstance(repo.database, _Database)


def test_auto_wire_transient_creates_new_each_time() -> None:
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_Repo)

    r1 = c.resolve(_Repo)
    r2 = c.resolve(_Repo)

    assert r1 is not r2
    assert r1.database is r2.database  # shared singleton Database


def test_auto_wire_deep_chain() -> None:
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_Repo)
    c.bind(_Service)

    svc = c.resolve(_Service)

    assert isinstance(svc.repo, _Repo)
    assert isinstance(svc.database, _Database)
    assert svc.repo.database is svc.database  # same Database singleton


# ── bind + resolve: factory ──────────────────────────────────────────


def test_factory_transient() -> None:
    c = Container()
    c.bind(_Database, factory=lambda: _Database("/custom"))

    db = c.resolve(_Database)

    assert db.path == "/custom"


def test_factory_singleton() -> None:
    c = Container()
    c.bind(_Database, factory=lambda: _Database("/singleton"), singleton=True)

    d1 = c.resolve(_Database)
    d2 = c.resolve(_Database)

    assert d1 is d2
    assert d1.path == "/singleton"


def test_singleton_factory_rejects_resolve_overrides() -> None:
    c = Container()
    c.bind(_Database, instance=_Database("/bound"))
    c.bind(_Repo, singleton=True)

    with pytest.raises(
        ResolutionError,
        match="overrides are not supported for singleton bindings",
    ):
        c.resolve(_Repo, database=_Database("/override"))


def test_instance_singleton_rejects_resolve_overrides() -> None:
    c = Container()
    c.bind(_Database, instance=_Database("/bound"))

    with pytest.raises(
        ResolutionError,
        match="overrides are not supported for singleton bindings",
    ):
        c.resolve(_Database, path="/override")


def _repo_factory(database: _Database) -> _Repo:
    return _Repo(database)


def test_factory_with_deps() -> None:
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_Repo, factory=_repo_factory)

    repo = c.resolve(_Repo)

    assert isinstance(repo.database, _Database)


async def _async_database_factory() -> _Database:
    await asyncio.sleep(0)
    return _Database("/async")


async def _async_repo_factory(database: _Database) -> _Repo:
    await asyncio.sleep(0)
    return _Repo(database)


def test_sync_resolve_rejects_async_factory() -> None:
    c = Container()
    c.bind(_Database, factory=_async_database_factory)

    with pytest.raises(
        ResolutionError,
        match=r"Cannot resolve _Database: factory '_async_database_factory' is async; "
        r"use resolve_async\(\) \(_Database\)",
    ):
        c.resolve(_Database)


def test_sync_resolve_shows_chain_for_indirect_async_dependency() -> None:
    c = Container()
    c.bind(_Database, factory=_async_database_factory)
    c.bind(_Repo)

    with pytest.raises(
        ResolutionError,
        match=r"Cannot resolve _Database: factory '_async_database_factory' is async; "
        r"use resolve_async\(\) \(_Repo -> _Database\)",
    ):
        c.resolve(_Repo)


def test_async_resolve_supports_async_factory() -> None:
    async def _run() -> None:
        c = Container()
        c.bind(_Database, factory=_async_database_factory)

        db = await c.resolve_async(_Database)

        assert db.path == "/async"

    asyncio.run(_run())


def test_async_resolve_supports_async_factory_dependencies() -> None:
    async def _run() -> None:
        c = Container()
        c.bind(_Database, factory=_async_database_factory)
        c.bind(_Repo, factory=_async_repo_factory)

        repo = await c.resolve_async(_Repo)

        assert repo.database.path == "/async"

    asyncio.run(_run())


def test_async_resolve_can_auto_wire_sync_types_over_async_dependencies() -> None:
    async def _run() -> None:
        c = Container()
        c.bind(_Database, factory=_async_database_factory)
        c.bind(_Repo)

        repo = await c.resolve_async(_Repo)

        assert repo.database.path == "/async"

    asyncio.run(_run())


def test_async_singleton_factory_is_initialized_once_under_concurrent_resolution() -> None:
    async def _run() -> None:
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

        results = await asyncio.gather(*(c.resolve_async(_Database) for _ in range(8)))

        assert all(result is results[0] for result in results)
        assert call_count == 1

    asyncio.run(_run())


def test_async_resolve_supports_sync_factory_with_override() -> None:
    async def _run() -> None:
        def _factory(path: str = "/custom") -> _Database:
            return _Database(path)

        c = Container()
        c.bind(_Database, factory=_factory)

        db = await c.resolve_async(_Database, path="/override")

        assert db.path == "/override"

    asyncio.run(_run())


def test_async_singleton_rejects_overrides() -> None:
    async def _run() -> None:
        c = Container()
        c.bind(_Database, factory=_async_database_factory, singleton=True)

        with pytest.raises(
            ResolutionError,
            match="overrides are not supported for singleton bindings",
        ):
            await c.resolve_async(_Database, path="/override")

    asyncio.run(_run())


def test_async_scope_inherits_parent_bindings() -> None:
    async def _run() -> None:
        parent = Container()
        parent.bind(_Database, factory=_async_database_factory)
        parent.bind(_Repo)

        child = parent.scope()
        repo = await child.resolve_async(_Repo)

        assert repo.database.path == "/async"

    asyncio.run(_run())


def test_async_scope_override_does_not_affect_parent() -> None:
    async def _run() -> None:
        parent = Container()
        parent.bind(_Database, instance=_Database("/parent"))

        child = parent.scope()
        child.bind(_Database, factory=_async_database_factory)

        parent_db = parent.resolve(_Database)
        child_db = await child.resolve_async(_Database)

        assert parent_db.path == "/parent"
        assert child_db.path == "/async"

    asyncio.run(_run())


def test_async_parent_singleton_factory_is_shared_with_children() -> None:
    async def _run() -> None:
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

        results = await asyncio.gather(*(child.resolve_async(_Database) for child in children))

        assert all(result is results[0] for result in results)
        assert call_count == 1

    asyncio.run(_run())


def test_async_parent_singleton_does_not_capture_child_override_on_first_resolution() -> None:
    async def _run() -> None:
        parent = Container()
        parent.bind(_Database, instance=_Database("/parent"))
        parent.bind(_Repo, factory=_async_repo_factory, singleton=True)

        child = parent.scope()
        child.bind(_Database, instance=_Database("/child"))

        child_repo = await child.resolve_async(_Repo)
        parent_repo = await parent.resolve_async(_Repo)

        assert child_repo is parent_repo
        assert child_repo.database.path == "/parent"
        assert parent_repo.database.path == "/parent"

    asyncio.run(_run())


def test_async_resolve_rejects_circular_dependencies() -> None:
    async def _run() -> None:
        c = Container()
        c.bind(_CircularA)
        c.bind(_CircularB)

        with pytest.raises(ResolutionError, match="Circular dependency"):
            await c.resolve_async(_CircularA)

    asyncio.run(_run())


# ── scope (child containers) ─────────────────────────────────────────


def test_scope_inherits_parent_bindings() -> None:
    parent = Container()
    parent.bind(_Database, instance=_Database("/parent"))
    parent.bind(_Repo)

    child = parent.scope()
    repo = child.resolve(_Repo)

    assert repo.database.path == "/parent"


def test_scope_override_does_not_affect_parent() -> None:
    parent = Container()
    parent.bind(_Database, instance=_Database("/parent"))

    child = parent.scope()
    child.bind(_Database, instance=_Database("/child"))

    assert parent.resolve(_Database).path == "/parent"
    assert child.resolve(_Database).path == "/child"


def test_scope_override_propagates_to_auto_wiring() -> None:
    parent = Container()
    parent.bind(_Database, instance=_Database("/parent"))
    parent.bind(_Repo)

    child = parent.scope()
    child.bind(_Database, instance=_Database("/child"))

    parent_repo = parent.resolve(_Repo)
    child_repo = child.resolve(_Repo)

    assert parent_repo.database.path == "/parent"
    assert child_repo.database.path == "/child"


def test_parent_singleton_factory_is_shared_with_children() -> None:
    parent = Container()
    parent.bind(_Database, factory=lambda: _Database("/shared"), singleton=True)

    child = parent.scope()

    parent_db = parent.resolve(_Database)
    child_db = child.resolve(_Database)

    assert parent_db is child_db


def test_parent_singleton_dependency_is_shared_when_resolved_through_child() -> None:
    parent = Container()
    parent.bind(_Database, factory=lambda: _Database("/shared"), singleton=True)
    parent.bind(_Repo)

    child = parent.scope()

    parent_repo = parent.resolve(_Repo)
    child_repo = child.resolve(_Repo)

    assert parent_repo.database is child_repo.database


def test_parent_singleton_does_not_capture_child_override_on_first_resolution() -> None:
    parent = Container()
    parent.bind(_Database, instance=_Database("/parent"))
    parent.bind(_Repo, singleton=True)

    child = parent.scope()
    child.bind(_Database, instance=_Database("/child"))

    child_repo = child.resolve(_Repo)
    parent_repo = parent.resolve(_Repo)

    assert child_repo is parent_repo
    assert child_repo.database.path == "/parent"
    assert parent_repo.database.path == "/parent"


def test_singleton_factory_is_initialized_once_under_concurrent_resolution() -> None:
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

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_resolve, range(8)))

    assert all(result is results[0] for result in results)
    assert call_count == 1


def test_parent_singleton_is_initialized_once_when_children_resolve_concurrently() -> None:
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

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_resolve, children))

    assert all(result is results[0] for result in results)
    assert call_count == 1


# ── default parameter behavior ───────────────────────────────────────


def test_default_used_when_no_binding() -> None:
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_ServiceWithDefault)

    svc = c.resolve(_ServiceWithDefault)

    assert svc.timeout == 30  # default preserved


def test_scalar_bindings_are_rejected() -> None:
    c = Container()

    with pytest.raises(TypeError, match="scalar builtins"):
        c.bind(int, instance=99)


def test_typed_value_object_binding_is_injected() -> None:
    c = Container()
    settings = _TimeoutSettings(99)
    c.bind(_Database, instance=_Database())
    c.bind(_TimeoutSettings, instance=settings)
    c.bind(_ServiceWithSettings)

    svc = c.resolve(_ServiceWithSettings)

    assert svc.settings is settings
    assert svc.settings.seconds == 99


def test_optional_binding_overrides_none_default() -> None:
    c = Container()
    db = _Database("/bound")
    c.bind(_Database, instance=db)
    c.bind(_ServiceWithOptionalDatabaseDefault)

    svc = c.resolve(_ServiceWithOptionalDatabaseDefault)

    assert svc.database is db


def test_optional_binding_uses_none_default_when_unbound() -> None:
    c = Container()
    c.bind(_ServiceWithOptionalDatabaseDefault)

    svc = c.resolve(_ServiceWithOptionalDatabaseDefault)

    assert svc.database is None


def test_optional_binding_without_default_still_requires_binding() -> None:
    c = Container()
    c.bind(_ServiceWithOptionalDatabaseRequired)

    with pytest.raises(ResolutionError, match="missing binding for parameter 'database'"):
        c.resolve(_ServiceWithOptionalDatabaseRequired)


# ── error handling ───────────────────────────────────────────────────


def test_bind_none_instance() -> None:
    """None is a valid instance value — it should not fall through to auto-wiring."""
    c = Container()
    c.bind(type(None), instance=None)

    assert c.resolve(type(None)) is None


def test_missing_binding_raises_resolution_error() -> None:
    c = Container()

    with pytest.raises(ResolutionError, match="no binding"):
        c.resolve(_Database)


def test_missing_dep_shows_chain() -> None:
    c = Container()
    c.bind(_Repo)  # needs _Database but it's not registered

    with pytest.raises(ResolutionError, match="_Database"):
        c.resolve(_Repo)


def test_annotated_dependency_requires_explicit_factory() -> None:
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_AnnotatedService)

    with pytest.raises(ResolutionError, match="Annotated"):
        c.resolve(_AnnotatedService)


def test_unresolvable_runtime_type_hints_raise_resolution_error() -> None:
    c = Container()
    c.bind(_MissingRuntimeHintService)

    original = globals().pop("_MissingRuntimeDependency")
    try:
        with pytest.raises(ResolutionError, match="failed to evaluate type hints"):
            c.resolve(_MissingRuntimeHintService)
    finally:
        globals()["_MissingRuntimeDependency"] = original


def test_untyped_required_param_shows_unknown_type() -> None:
    c = Container()
    c.bind(_UntypedService)

    with pytest.raises(ResolutionError, match=r"type=\?"):
        c.resolve(_UntypedService)


def test_circular_dependency_detected() -> None:
    c = Container()
    c.bind(_CircularA)
    c.bind(_CircularB)

    with pytest.raises(ResolutionError, match="Circular dependency"):
        c.resolve(_CircularA)


def _load_module_from_source(tmp_path: Path, source: str) -> dict[str, object]:
    module_name = f"_miniject_test_{uuid.uuid4().hex}"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(source, encoding="utf-8")

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module.__dict__


def test_resolve_deferred_forward_reference_type_hints_in_runtime_annotations_injects_dependency(
    tmp_path: Path,
) -> None:
    if sys.version_info < (3, 14):
        pytest.skip("Python 3.14+ only")

    namespace = _load_module_from_source(
        tmp_path,
        """
class Repo:
    def __init__(self, database: Database) -> None:
        self.database = database

class Database:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
""",
    )

    repo_type = cast("type[Any]", namespace["Repo"])
    database_type = cast("type[Any]", namespace["Database"])
    assert isinstance(repo_type, type)
    assert isinstance(database_type, type)

    c = Container()
    c.bind(database_type, instance=database_type("/deferred"))
    c.bind(repo_type)

    repo = c.resolve(repo_type)

    assert repo.database.path == "/deferred"


def test_resolve_unresolvable_runtime_deferred_annotations_raises_resolution_error(
    tmp_path: Path,
) -> None:
    if sys.version_info < (3, 14):
        pytest.skip("Python 3.14+ only")

    namespace = _load_module_from_source(
        tmp_path,
        """
class Service:
    def __init__(self, dependency: MissingType) -> None:
        self.dependency = dependency
""",
    )

    service_type = cast("type[Any]", namespace["Service"])
    assert isinstance(service_type, type)

    c = Container()
    c.bind(service_type)

    with pytest.raises(ResolutionError, match="failed to evaluate type hints"):
        c.resolve(service_type)
