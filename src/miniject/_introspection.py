"""Internal helpers for callable introspection and annotation normalization."""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

_EMPTY: object = object()
_DISALLOWED_AUTO_INJECT_TYPES: frozenset[type] = frozenset({bool, bytes, float, int, str})
_NONE_TYPE: type[None] = type(None)
_UNION_TYPES: tuple[object, ...] = (typing.Union, types.UnionType)
_INTROSPECTION_CACHE: dict[object, tuple[inspect.Signature, dict[str, Any]] | None] = {}


@dataclass(frozen=True, slots=True)
class ResolvedParamType:
    """Normalized parameter type metadata used during dependency resolution."""

    binding_key: type | None
    display_name: str


def introspect_factory(
    factory: Callable[..., Any],
    *,
    resolution_error: type[Exception],
) -> tuple[inspect.Signature, dict[str, Any]] | None:
    """Extract signature and type hints for a factory, or None if not introspectable."""
    try:
        cached = _INTROSPECTION_CACHE.get(factory, _EMPTY)
    except TypeError:
        return _compute_factory_introspection(factory, resolution_error=resolution_error)
    if cached is not _EMPTY:
        return cast("tuple[inspect.Signature, dict[str, Any]] | None", cached)

    result = _compute_factory_introspection(factory, resolution_error=resolution_error)
    _INTROSPECTION_CACHE[factory] = result
    return result


def _compute_factory_introspection(
    factory: Callable[..., Any],
    *,
    resolution_error: type[Exception],
) -> tuple[inspect.Signature, dict[str, Any]] | None:
    """Compute signature and type hints for a factory without caching."""
    try:
        sig = inspect.signature(factory)
    except (ValueError, TypeError):
        return None
    hint_target = factory.__init__ if isinstance(factory, type) else factory
    hints = _get_type_hints_or_raise(
        hint_target,
        factory_name=callable_name(factory),
        resolution_error=resolution_error,
    )
    return sig, hints


def _get_type_hints_or_raise(
    fn: Callable[..., Any],
    *,
    factory_name: str,
    resolution_error: type[Exception],
) -> dict[str, Any]:
    """Get runtime-resolvable type hints for a factory or constructor."""
    try:
        return typing.get_type_hints(fn, include_extras=True)
    except (AttributeError, NameError, TypeError, ValueError) as exc:
        target_name = callable_name(fn)
        raise resolution_error(
            f"Cannot resolve {factory_name}: failed to evaluate type hints for "
            f"{target_name}; make annotations importable at runtime or use an explicit "
            f"factory ({exc.__class__.__name__}: {exc})",
        ) from exc


def resolve_param_type(
    param_type: Any,
    *,
    factory_name: str,
    param_name: str,
    resolution_error: type[Exception],
) -> ResolvedParamType:
    """Normalize a parameter annotation into a DI binding key and semantics."""
    if param_type is None:
        return ResolvedParamType(binding_key=None, display_name="?")

    origin = typing.get_origin(param_type)
    if origin is typing.Annotated:
        raise resolution_error(
            f"Cannot resolve {factory_name}: parameter '{param_name}' uses Annotated[...] "
            "which miniject does not support; use an explicit factory instead",
        )

    if origin in _UNION_TYPES:
        args = typing.get_args(param_type)
        non_none_args = tuple(arg for arg in args if arg is not _NONE_TYPE)
        if len(non_none_args) == 1 and len(non_none_args) != len(args):
            inner = non_none_args[0]
            inner_origin = typing.get_origin(inner)
            if inner_origin is typing.Annotated:
                raise resolution_error(
                    f"Cannot resolve {factory_name}: parameter '{param_name}' uses "
                    "Annotated[...] which miniject does not support; use an explicit "
                    "factory instead",
                )
            binding_key = inner if is_auto_injectable_type(inner) else None
            return ResolvedParamType(
                binding_key=binding_key,
                display_name=format_type_name(param_type),
            )

    binding_key = param_type if is_auto_injectable_type(param_type) else None
    return ResolvedParamType(
        binding_key=binding_key,
        display_name=format_type_name(param_type),
    )


def format_type_name(param_type: Any) -> str:
    """Render a readable type name for resolution errors."""
    if param_type is None:
        return "?"
    if isinstance(param_type, type):
        return param_type.__name__
    return str(param_type).replace("typing.", "")


def is_auto_injectable_type(param_type: Any) -> bool:
    """Return whether a type hint should be used as a DI lookup key."""
    return isinstance(param_type, type) and param_type not in _DISALLOWED_AUTO_INJECT_TYPES


def callable_name(fn: Callable[..., Any]) -> str:
    """Best-effort human-readable callable name for diagnostics."""
    return getattr(fn, "__name__", fn.__class__.__name__)


def validate_service_type(service: type[Any]) -> None:
    """Reject unsupported DI keys up front."""
    if service in _DISALLOWED_AUTO_INJECT_TYPES:
        msg = (
            f"Cannot bind {service.__name__}: scalar builtins are not supported as DI keys; "
            "use a typed value object or an explicit factory instead"
        )
        raise TypeError(msg)
