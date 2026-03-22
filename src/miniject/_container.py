"""Lightweight dependency injection container.

Provides constructor-based auto-wiring, singleton/transient scopes, and scoped
child containers for testing and experiment overrides.  Only composition roots
should call ``.resolve()``; all other code receives dependencies via constructor
injection.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from typing import Any, TypeVar, cast

_T = TypeVar("_T")

_EMPTY: object = object()


class ResolutionError(Exception):
    """Raised when a dependency cannot be resolved."""


class _Binding:
    """Internal registration record."""

    __slots__ = ("factory", "instance", "singleton")

    def __init__(
        self,
        *,
        factory: Callable[..., Any] | None = None,
        instance: object = _EMPTY,
        singleton: bool = False,
    ) -> None:
        self.factory = factory
        self.instance = instance
        self.singleton = singleton


class Container:
    """Minimal DI container with auto-wiring and scoped child containers.

    API::

        container = Container()
        container.bind(Database, instance=db)      # singleton by instance
        container.bind(TradeRepository)             # auto-wired transient
        container.bind(BalanceTracker, factory=fn, singleton=True)

        db = container.resolve(Database)

        scoped = container.scope()                  # child container
        scoped.bind(FooParams, instance=modified)   # override in child
        strat = scoped.resolve(MyStrategy)          # parent unaffected
    """

    def __init__(self, *, _parent: Container | None = None) -> None:
        self._bindings: dict[type, _Binding] = {}
        self._singletons: dict[type, object] = {}
        self._parent = _parent

    def bind(
        self,
        service: type[_T],
        *,
        factory: Callable[..., _T] | None = None,
        instance: _T | object = _EMPTY,
        singleton: bool = False,
    ) -> None:
        """Register a service type.

        * ``bind(SomeType)`` — auto-wire from ``__init__`` type hints (transient)
        * ``bind(SomeType, instance=obj)`` — singleton by instance
        * ``bind(SomeType, factory=fn)`` — custom factory (transient by default)
        * ``bind(SomeType, factory=fn, singleton=True)`` — factory singleton
        """
        if instance is not _EMPTY:
            self._bindings[service] = _Binding(instance=instance)
            self._singletons[service] = instance
        elif factory is not None:
            self._bindings[service] = _Binding(factory=factory, singleton=singleton)
        else:
            self._bindings[service] = _Binding(factory=service, singleton=singleton)

    def resolve(self, service: type[_T], **overrides: Any) -> _T:
        """Resolve a service, auto-wiring constructor dependencies.

        Raises :class:`ResolutionError` on missing bindings or circular deps.
        """
        return self._resolve(service, _stack=(), **overrides)

    def scope(self) -> Container:
        """Create a child container inheriting all parent bindings.

        Overrides in the child do **not** affect the parent.
        """
        return Container(_parent=self)

    # ── internals ────────────────────────────────────────────────────

    def _resolve(self, service: type[_T], *, _stack: tuple[type, ...], **overrides: Any) -> _T:
        # Check for circular dependency
        if service in _stack:
            chain = " -> ".join(t.__name__ for t in (*_stack, service))
            raise ResolutionError(f"Circular dependency: {chain}")

        # Check local singletons first
        if service in self._singletons:
            return cast(_T, self._singletons[service])

        # Find binding (local then parent chain)
        binding = self._find_binding(service)
        if binding is None:
            chain = " -> ".join(t.__name__ for t in (*_stack, service))
            raise ResolutionError(f"Cannot resolve {service.__name__}: no binding ({chain})")

        # Instance binding (already stored)
        if binding.instance is not _EMPTY:
            return cast(_T, binding.instance)

        # Factory binding
        assert binding.factory is not None
        instance = self._invoke_factory(binding.factory, _stack=(*_stack, service), **overrides)

        if binding.singleton:
            self._singletons[service] = instance

        return cast(_T, instance)

    def _find_binding(self, service: type) -> _Binding | None:
        if service in self._bindings:
            return self._bindings[service]
        if self._parent is not None:
            return self._parent._find_binding(service)
        return None

    def _invoke_factory(
        self, factory: Callable[..., Any], *, _stack: tuple[type, ...], **overrides: Any
    ) -> Any:
        """Call a factory, resolving its parameters from the container."""
        sig_and_hints = _introspect_factory(factory)
        if sig_and_hints is None:
            return factory()
        sig, hints = sig_and_hints

        kwargs: dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            if param_name in overrides:
                kwargs[param_name] = overrides[param_name]
                continue

            param_type = hints.get(param_name)
            if param_type is not None and isinstance(param_type, type):
                binding = self._find_binding(param_type)
                if binding is not None:
                    kwargs[param_name] = self._resolve(param_type, _stack=_stack)
                    continue

            # No binding found — use default if available
            if param.default is not inspect.Parameter.empty:
                continue  # let Python's default apply
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue

            # Required param with no binding and no default
            chain = " -> ".join(t.__name__ for t in _stack)
            raise ResolutionError(
                f"Cannot resolve {factory.__name__}: "
                f"missing binding for parameter '{param_name}' "
                f"(type={param_type.__name__ if param_type else '?'}) "
                f"({chain})"
            )

        return factory(**kwargs)


def _introspect_factory(
    factory: Callable[..., Any],
) -> tuple[inspect.Signature, dict[str, Any]] | None:
    """Extract signature and type hints for a factory, or None if not introspectable."""
    try:
        sig = inspect.signature(factory)
    except (ValueError, TypeError):
        return None
    hint_target = factory.__init__ if isinstance(factory, type) else factory
    hints = _get_type_hints_safe(hint_target)
    return sig, hints


def _get_type_hints_safe(fn: Callable[..., Any]) -> dict[str, Any]:
    """Get type hints, resolving string annotations from ``from __future__ import annotations``."""
    try:
        return typing.get_type_hints(fn)
    except (AttributeError, NameError, TypeError, ValueError):
        return {}
