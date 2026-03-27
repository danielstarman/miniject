"""Lightweight dependency injection container.

Provides constructor-based auto-wiring, singleton/transient scopes, and scoped
child containers for testing and experiment overrides.  Only composition roots
should call ``.resolve()``; all other code receives dependencies via constructor
injection.
"""

from __future__ import annotations

import asyncio
import inspect
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any, TypeVar, cast

_T = TypeVar("_T")
_SyncFactory = Callable[..., _T]
_AsyncFactory = Callable[..., Awaitable[_T]]

_EMPTY: object = object()
_DISALLOWED_AUTO_INJECT_TYPES: frozenset[type] = frozenset({bool, bytes, float, int, str})
_NONE_TYPE: type[None] = type(None)
_UNION_TYPES: tuple[object, ...] = (typing.Union, types.UnionType)
_INTROSPECTION_CACHE: dict[object, tuple[inspect.Signature, dict[str, Any]] | None] = {}


class ResolutionError(Exception):
    """Raised when a dependency cannot be resolved."""


@dataclass(frozen=True, slots=True)
class _ResolvedParamType:
    """Normalized parameter type metadata used during dependency resolution."""

    binding_key: type | None
    display_name: str


@dataclass(frozen=True, slots=True)
class _Binding:
    """Internal registration record."""

    provider_kind: typing.Literal["factory", "instance"]
    provider: object
    lifetime: typing.Literal["singleton", "transient"]

    @classmethod
    def from_factory(cls, factory: Callable[..., Any], *, singleton: bool) -> _Binding:
        return cls(
            provider_kind="factory",
            provider=factory,
            lifetime="singleton" if singleton else "transient",
        )

    @classmethod
    def from_instance(cls, instance: object) -> _Binding:
        return cls(provider_kind="instance", provider=instance, lifetime="singleton")


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
        self._pending_async_singletons: dict[type, asyncio.Task[object]] = {}
        self._parent = _parent
        self._lock = RLock()

    def bind(
        self,
        service: type[_T],
        *,
        factory: _SyncFactory[_T] | _AsyncFactory[_T] | None = None,
        instance: _T | object = _EMPTY,
        singleton: bool = False,
    ) -> None:
        """Register a service type.

        * ``bind(SomeType)`` — auto-wire from ``__init__`` type hints (transient)
        * ``bind(SomeType, instance=obj)`` — singleton by instance
        * ``bind(SomeType, factory=fn)`` — custom factory (transient by default)
        * ``bind(SomeType, factory=fn, singleton=True)`` — factory singleton
        """
        _validate_service_type(service)
        self._singletons.pop(service, None)
        if instance is not _EMPTY:
            self._bindings[service] = _Binding.from_instance(instance)
            self._singletons[service] = instance
        elif factory is not None:
            self._bindings[service] = _Binding.from_factory(factory, singleton=singleton)
        else:
            self._bindings[service] = _Binding.from_factory(service, singleton=singleton)

    def resolve(self, service: type[_T], **overrides: Any) -> _T:
        """Resolve a service, auto-wiring constructor dependencies.

        Raises :class:`ResolutionError` on missing bindings or circular deps.
        """
        return self._resolve(service, _stack=(), **overrides)

    async def resolve_async(self, service: type[_T], **overrides: Any) -> _T:
        """Resolve a service, awaiting async factories as needed."""
        return await self._resolve_async(service, _stack=(), **overrides)

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

        # Find binding owner (local then parent chain)
        binding_owner_and_binding = self._find_binding_owner(service)
        if binding_owner_and_binding is None:
            chain = " -> ".join(t.__name__ for t in (*_stack, service))
            raise ResolutionError(f"Cannot resolve {service.__name__}: no binding ({chain})")
        binding_owner, binding = binding_owner_and_binding

        if overrides and binding.lifetime == "singleton":
            raise ResolutionError(
                f"Cannot resolve {service.__name__}: overrides are not supported "
                "for singleton bindings; use a child scope or an explicit "
                "factory instead",
            )

        # Singleton factories should be shared and initialized safely where the
        # binding is defined.
        if binding.lifetime == "singleton":
            return cast(
                "_T",
                binding_owner._resolve_singleton(
                    service,
                    binding,
                    stack=(*_stack, service),
                    overrides=overrides,
                ),
            )

        # Instance binding (already stored)
        if binding.provider_kind == "instance":
            return cast("_T", binding.provider)

        # Factory binding
        factory = _require_factory(binding)
        instance = self._invoke_factory(factory, _stack=(*_stack, service), **overrides)

        return cast("_T", instance)

    async def _resolve_async(
        self,
        service: type[_T],
        *,
        _stack: tuple[type, ...],
        **overrides: Any,
    ) -> _T:
        if service in _stack:
            chain = " -> ".join(t.__name__ for t in (*_stack, service))
            raise ResolutionError(f"Circular dependency: {chain}")

        binding_owner_and_binding = self._find_binding_owner(service)
        if binding_owner_and_binding is None:
            chain = " -> ".join(t.__name__ for t in (*_stack, service))
            raise ResolutionError(f"Cannot resolve {service.__name__}: no binding ({chain})")
        binding_owner, binding = binding_owner_and_binding

        if overrides and binding.lifetime == "singleton":
            raise ResolutionError(
                f"Cannot resolve {service.__name__}: overrides are not supported "
                "for singleton bindings; use a child scope or an explicit "
                "factory instead",
            )

        if binding.lifetime == "singleton":
            return cast(
                "_T",
                await binding_owner._resolve_singleton_async(
                    service,
                    binding,
                    stack=(*_stack, service),
                    overrides=overrides,
                ),
            )

        if binding.provider_kind == "instance":
            return cast("_T", binding.provider)

        factory = _require_factory(binding)
        instance = await self._invoke_factory_async(
            factory,
            _stack=(*_stack, service),
            **overrides,
        )
        return cast("_T", instance)

    def _find_binding(self, service: type) -> _Binding | None:
        binding_owner_and_binding = self._find_binding_owner(service)
        if binding_owner_and_binding is None:
            return None
        _, binding = binding_owner_and_binding
        return binding

    def _find_binding_owner(self, service: type) -> tuple[Container, _Binding] | None:
        if service in self._bindings:
            return self, self._bindings[service]
        if self._parent is not None:
            return self._parent._find_binding_owner(service)
        return None

    def _resolve_singleton(
        self,
        service: type,
        binding: _Binding,
        *,
        stack: tuple[type, ...],
        overrides: dict[str, Any],
    ) -> object:
        with self._lock:
            existing = self._singletons.get(service, _EMPTY)
            if existing is not _EMPTY:
                return existing

            if binding.provider_kind == "instance":
                self._singletons[service] = binding.provider
                return binding.provider

            factory = _require_factory(binding)
            instance = self._invoke_factory(
                factory,
                _stack=stack,
                **overrides,
            )
            self._singletons[service] = instance
            return instance

    async def _resolve_singleton_async(
        self,
        service: type,
        binding: _Binding,
        *,
        stack: tuple[type, ...],
        overrides: dict[str, Any],
    ) -> object:
        with self._lock:
            existing = self._singletons.get(service, _EMPTY)
            if existing is not _EMPTY:
                return existing

            if binding.provider_kind == "instance":
                self._singletons[service] = binding.provider
                return binding.provider

            pending = self._pending_async_singletons.get(service)
            if pending is None:
                factory = _require_factory(binding)
                pending = asyncio.create_task(
                    self._invoke_factory_async(
                        factory,
                        _stack=stack,
                        **overrides,
                    ),
                )
                self._pending_async_singletons[service] = pending

        try:
            instance = await pending
        except Exception:
            with self._lock:
                if self._pending_async_singletons.get(service) is pending:
                    self._pending_async_singletons.pop(service, None)
            raise

        with self._lock:
            self._singletons[service] = instance
            if self._pending_async_singletons.get(service) is pending:
                self._pending_async_singletons.pop(service, None)
        return instance

    def _invoke_factory(
        self,
        factory: Callable[..., Any],
        *,
        _stack: tuple[type, ...],
        **overrides: Any,
    ) -> Any:
        """Call a factory, resolving its parameters from the container."""
        sig_and_hints = _introspect_factory(factory)
        if sig_and_hints is None:
            if inspect.iscoroutinefunction(factory):
                _raise_async_resolution_error(factory, stack=_stack)
            instance = factory()
            if inspect.isawaitable(instance):
                if inspect.iscoroutine(instance):
                    instance.close()
                _raise_async_resolution_error(factory, stack=_stack, returned_awaitable=True)
            return instance
        sig, hints = sig_and_hints
        kwargs = self._build_factory_kwargs(
            factory,
            sig,
            hints,
            _stack=_stack,
            overrides=overrides,
        )
        if inspect.iscoroutinefunction(factory):
            _raise_async_resolution_error(factory, stack=_stack)

        instance = factory(**kwargs)
        if inspect.isawaitable(instance):
            if inspect.iscoroutine(instance):
                instance.close()
            _raise_async_resolution_error(factory, stack=_stack, returned_awaitable=True)
        return instance

    async def _invoke_factory_async(
        self,
        factory: Callable[..., Any],
        *,
        _stack: tuple[type, ...],
        **overrides: Any,
    ) -> Any:
        """Call a factory, awaiting it if needed and resolving deps async."""
        sig_and_hints = _introspect_factory(factory)
        if sig_and_hints is None:
            instance = factory()
            if inspect.isawaitable(instance):
                return await instance
            return instance
        sig, hints = sig_and_hints
        kwargs = await self._build_factory_kwargs_async(
            factory,
            sig,
            hints,
            _stack=_stack,
            overrides=overrides,
        )

        instance = factory(**kwargs)
        if inspect.isawaitable(instance):
            return await instance
        return instance

    def _build_factory_kwargs(
        self,
        factory: Callable[..., Any],
        sig: inspect.Signature,
        hints: dict[str, Any],
        *,
        _stack: tuple[type, ...],
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            if param_name in overrides:
                kwargs[param_name] = overrides[param_name]
                continue

            resolved_type = _resolve_param_type(
                hints.get(param_name),
                factory_name=_callable_name(factory),
                param_name=param_name,
            )
            if resolved_type.binding_key is not None:
                binding = self._find_binding(resolved_type.binding_key)
                if binding is not None:
                    kwargs[param_name] = self._resolve(resolved_type.binding_key, _stack=_stack)
                    continue

            _validate_or_skip_missing_param(
                factory,
                param_name,
                param,
                resolved_type.display_name,
                stack=_stack,
            )
        return kwargs

    async def _build_factory_kwargs_async(
        self,
        factory: Callable[..., Any],
        sig: inspect.Signature,
        hints: dict[str, Any],
        *,
        _stack: tuple[type, ...],
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            if param_name in overrides:
                kwargs[param_name] = overrides[param_name]
                continue

            resolved_type = _resolve_param_type(
                hints.get(param_name),
                factory_name=_callable_name(factory),
                param_name=param_name,
            )
            if resolved_type.binding_key is not None:
                binding = self._find_binding(resolved_type.binding_key)
                if binding is not None:
                    kwargs[param_name] = await self._resolve_async(
                        resolved_type.binding_key,
                        _stack=_stack,
                    )
                    continue

            _validate_or_skip_missing_param(
                factory,
                param_name,
                param,
                resolved_type.display_name,
                stack=_stack,
            )
        return kwargs


def _introspect_factory(
    factory: Callable[..., Any],
) -> tuple[inspect.Signature, dict[str, Any]] | None:
    """Extract signature and type hints for a factory, or None if not introspectable."""
    try:
        cached = _INTROSPECTION_CACHE.get(factory, _EMPTY)
    except TypeError:
        return _compute_factory_introspection(factory)
    if cached is not _EMPTY:
        return cast("tuple[inspect.Signature, dict[str, Any]] | None", cached)

    result = _compute_factory_introspection(factory)
    _INTROSPECTION_CACHE[factory] = result
    return result


def _compute_factory_introspection(
    factory: Callable[..., Any],
) -> tuple[inspect.Signature, dict[str, Any]] | None:
    """Compute signature and type hints for a factory without caching."""
    try:
        sig = inspect.signature(factory)
    except (ValueError, TypeError):
        return None
    hint_target = factory.__init__ if isinstance(factory, type) else factory
    hints = _get_type_hints_or_raise(hint_target, factory_name=_callable_name(factory))
    return sig, hints


def _get_type_hints_or_raise(
    fn: Callable[..., Any],
    *,
    factory_name: str,
) -> dict[str, Any]:
    """Get runtime-resolvable type hints for a factory or constructor."""
    try:
        return typing.get_type_hints(fn, include_extras=True)
    except (AttributeError, NameError, TypeError, ValueError) as exc:
        target_name = _callable_name(fn)
        raise ResolutionError(
            f"Cannot resolve {factory_name}: failed to evaluate type hints for "
            f"{target_name}; make annotations importable at runtime or use an explicit "
            f"factory ({exc.__class__.__name__}: {exc})",
        ) from exc


def _resolve_param_type(
    param_type: Any,
    *,
    factory_name: str,
    param_name: str,
) -> _ResolvedParamType:
    """Normalize a parameter annotation into a DI binding key and semantics."""
    if param_type is None:
        return _ResolvedParamType(binding_key=None, display_name="?")

    origin = typing.get_origin(param_type)
    if origin is typing.Annotated:
        raise ResolutionError(
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
                raise ResolutionError(
                    f"Cannot resolve {factory_name}: parameter '{param_name}' uses "
                    "Annotated[...] which miniject does not support; use an explicit "
                    "factory instead",
                )
            binding_key = inner if _is_auto_injectable_type(inner) else None
            return _ResolvedParamType(
                binding_key=binding_key,
                display_name=_format_type_name(param_type),
            )

    binding_key = param_type if _is_auto_injectable_type(param_type) else None
    return _ResolvedParamType(
        binding_key=binding_key,
        display_name=_format_type_name(param_type),
    )


def _format_type_name(param_type: Any) -> str:
    """Render a readable type name for resolution errors."""
    if param_type is None:
        return "?"
    if isinstance(param_type, type):
        return param_type.__name__
    return str(param_type).replace("typing.", "")


def _is_auto_injectable_type(param_type: Any) -> bool:
    """Return whether a type hint should be used as a DI lookup key."""
    return isinstance(param_type, type) and param_type not in _DISALLOWED_AUTO_INJECT_TYPES


def _callable_name(fn: Callable[..., Any]) -> str:
    """Best-effort human-readable callable name for diagnostics."""
    return getattr(fn, "__name__", fn.__class__.__name__)


def _require_factory(binding: _Binding) -> Callable[..., Any]:
    """Return a binding factory or raise if the binding is malformed."""
    if binding.provider_kind != "factory":
        msg = "Binding is missing a factory"
        raise ResolutionError(msg)
    return cast("Callable[..., Any]", binding.provider)


def _raise_async_resolution_error(
    factory: Callable[..., Any],
    *,
    stack: tuple[type, ...],
    returned_awaitable: bool = False,
) -> typing.NoReturn:
    """Raise a consistent error when sync resolution hits async work."""
    service_name = stack[-1].__name__ if stack else _callable_name(factory)
    chain = " -> ".join(t.__name__ for t in stack)
    behavior = "returned awaitable" if returned_awaitable else "is async"
    raise ResolutionError(
        f"Cannot resolve {service_name}: factory '{_callable_name(factory)}' {behavior}; "
        f"use resolve_async() ({chain})",
    )


def _validate_or_skip_missing_param(
    factory: Callable[..., Any],
    param_name: str,
    param: inspect.Parameter,
    display_name: str,
    *,
    stack: tuple[type, ...],
) -> None:
    """Raise for unresolved required params and otherwise let Python apply defaults."""
    if param.default is not inspect.Parameter.empty:
        return
    if param.kind in (
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    ):
        return

    chain = " -> ".join(t.__name__ for t in stack)
    raise ResolutionError(
        f"Cannot resolve {factory.__name__}: "
        f"missing binding for parameter '{param_name}' "
        f"(type={display_name}) "
        f"({chain})",
    )


def _validate_service_type(service: type[Any]) -> None:
    """Reject unsupported DI keys up front."""
    if service in _DISALLOWED_AUTO_INJECT_TYPES:
        msg = (
            f"Cannot bind {service.__name__}: scalar builtins are not supported as DI keys; "
            "use a typed value object or an explicit factory instead"
        )
        raise TypeError(msg)
