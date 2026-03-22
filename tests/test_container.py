"""Tests for the DI container."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from typing import Annotated

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


def _repo_factory(database: _Database) -> _Repo:
    return _Repo(database)


def test_factory_with_deps() -> None:
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_Repo, factory=_repo_factory)

    repo = c.resolve(_Repo)

    assert isinstance(repo.database, _Database)


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
