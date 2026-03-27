"""Tests for annotation and binding-key introspection behavior."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any, cast

import pytest

from miniject import Container, ResolutionError


class _Database:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path


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


def test_bind_scalar_builtin_instance_raises_type_error() -> None:
    # Arrange
    c = Container()

    # Act + Assert
    with pytest.raises(TypeError, match="scalar builtins"):
        c.bind(int, instance=99)


def test_resolve_annotated_dependency_without_factory_raises_resolution_error() -> None:
    # Arrange
    c = Container()
    c.bind(_Database, instance=_Database())
    c.bind(_AnnotatedService)

    # Act + Assert
    with pytest.raises(ResolutionError, match="Annotated"):
        c.resolve(_AnnotatedService)


def test_resolve_unresolvable_runtime_type_hints_raises_resolution_error() -> None:
    # Arrange
    c = Container()
    c.bind(_MissingRuntimeHintService)

    original = globals().pop("_MissingRuntimeDependency")
    try:
        # Act + Assert
        with pytest.raises(ResolutionError, match="failed to evaluate type hints"):
            c.resolve(_MissingRuntimeHintService)
    finally:
        globals()["_MissingRuntimeDependency"] = original


def test_resolve_untyped_required_parameter_shows_unknown_type() -> None:
    # Arrange
    c = Container()
    c.bind(_UntypedService)

    # Act + Assert
    with pytest.raises(ResolutionError, match=r"type=\?"):
        c.resolve(_UntypedService)


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

    # Arrange
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

    # Act
    repo = c.resolve(repo_type)

    # Assert
    assert repo.database.path == "/deferred"


def test_resolve_unresolvable_deferred_runtime_annotations_raises_resolution_error(
    tmp_path: Path,
) -> None:
    if sys.version_info < (3, 14):
        pytest.skip("Python 3.14+ only")

    # Arrange
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

    # Act + Assert
    with pytest.raises(ResolutionError, match="failed to evaluate type hints"):
        c.resolve(service_type)
