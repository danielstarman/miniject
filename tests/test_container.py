"""Tests for the DI container."""

from __future__ import annotations

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


# ── default parameter behavior ───────────────────────────────────────


def test_default_used_when_no_binding() -> None:
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_ServiceWithDefault)

    svc = c.resolve(_ServiceWithDefault)

    assert svc.timeout == 30  # default preserved


def test_binding_overrides_default() -> None:
    """If a binding exists for the param type, it wins over the default."""
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(int, instance=99)
    c.bind(_ServiceWithDefault)

    svc = c.resolve(_ServiceWithDefault)

    assert svc.timeout == 99  # binding wins


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


def test_circular_dependency_detected() -> None:
    c = Container()
    c.bind(_CircularA)
    c.bind(_CircularB)

    with pytest.raises(ResolutionError, match="Circular dependency"):
        c.resolve(_CircularA)
