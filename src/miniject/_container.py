"""Lightweight dependency injection container.

Provides constructor-based auto-wiring, singleton/transient scopes, and scoped
child containers for testing and experiment overrides.  Only composition roots
should call ``.resolve()``; all other code receives dependencies via constructor
injection.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any, Literal, NoReturn, TypeVar, cast

from miniject._introspection import (
    callable_name,
    introspect_factory,
    resolve_param_type,
    validate_service_type,
)

_T = TypeVar("_T")
_SyncFactory = Callable[..., _T]
_AsyncFactory = Callable[..., Awaitable[_T]]

_EMPTY: object = object()


class ResolutionError(Exception):
    """Raised when a dependency cannot be resolved."""


@dataclass(frozen=True, slots=True)
class _Binding:
    """Internal registration record."""

    provider_kind: Literal["factory", "instance"]
    provider: object
    lifetime: Literal["singleton", "transient"]

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
        validate_service_type(service)
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
        plan = introspect_factory(factory, resolution_error=ResolutionError)
        if plan is None:
            return _call_sync_factory_checked(
                factory,
                stack=_stack,
                is_async=inspect.iscoroutinefunction(factory),
            )
        fast_args = self._build_fast_positional_args(
            plan.fast_positional_deps,
            _stack=_stack,
            overrides=overrides,
        )
        if fast_args is not None:
            return _call_sync_factory_checked(
                factory,
                args=fast_args,
                stack=_stack,
                is_async=plan.is_async,
            )
        kwargs = self._build_factory_kwargs(
            factory,
            plan.signature,
            plan.hints,
            _stack=_stack,
            overrides=overrides,
        )
        return _call_sync_factory_checked(
            factory,
            kwargs=kwargs,
            stack=_stack,
            is_async=plan.is_async,
        )

    async def _invoke_factory_async(
        self,
        factory: Callable[..., Any],
        *,
        _stack: tuple[type, ...],
        **overrides: Any,
    ) -> Any:
        """Call a factory, awaiting it if needed and resolving deps async."""
        plan = introspect_factory(factory, resolution_error=ResolutionError)
        if plan is None:
            instance = factory()
            if inspect.isawaitable(instance):
                return await instance
            return instance
        fast_args = await self._build_fast_positional_args_async(
            plan.fast_positional_deps,
            _stack=_stack,
            overrides=overrides,
        )
        if fast_args is not None:
            instance = factory(*fast_args)
            if inspect.isawaitable(instance):
                return await instance
            return instance
        kwargs = await self._build_factory_kwargs_async(
            factory,
            plan.signature,
            plan.hints,
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

            resolved_type = resolve_param_type(
                hints.get(param_name),
                factory_name=callable_name(factory),
                param_name=param_name,
                resolution_error=ResolutionError,
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

    def _build_fast_positional_args(
        self,
        fast_positional_deps: tuple[type, ...] | None,
        *,
        _stack: tuple[type, ...],
        overrides: dict[str, Any],
    ) -> tuple[object, ...] | None:
        if fast_positional_deps is None or overrides:
            return None

        resolved: list[object] = []
        for dep in fast_positional_deps:
            if self._find_binding(dep) is None:
                return None
            resolved_instance = cast("object", self._resolve(dep, _stack=_stack))
            resolved.append(resolved_instance)
        return tuple(resolved)

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

            resolved_type = resolve_param_type(
                hints.get(param_name),
                factory_name=callable_name(factory),
                param_name=param_name,
                resolution_error=ResolutionError,
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

    async def _build_fast_positional_args_async(
        self,
        fast_positional_deps: tuple[type, ...] | None,
        *,
        _stack: tuple[type, ...],
        overrides: dict[str, Any],
    ) -> tuple[object, ...] | None:
        if fast_positional_deps is None or overrides:
            return None

        resolved: list[object] = []
        for dep in fast_positional_deps:
            if self._find_binding(dep) is None:
                return None
            resolved_instance = cast("object", await self._resolve_async(dep, _stack=_stack))
            resolved.append(resolved_instance)
        return tuple(resolved)


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
) -> NoReturn:
    """Raise a consistent error when sync resolution hits async work."""
    service_name = stack[-1].__name__ if stack else callable_name(factory)
    chain = " -> ".join(t.__name__ for t in stack)
    behavior = "returned awaitable" if returned_awaitable else "is async"
    raise ResolutionError(
        f"Cannot resolve {service_name}: factory '{callable_name(factory)}' {behavior}; "
        f"use resolve_async() ({chain})",
    )


def _call_sync_factory_checked(
    factory: Callable[..., Any],
    *,
    stack: tuple[type, ...],
    is_async: bool,
    args: tuple[object, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> Any:
    if is_async:
        _raise_async_resolution_error(factory, stack=stack)

    instance = factory(*args, **({} if kwargs is None else kwargs))
    if inspect.isawaitable(instance):
        if inspect.iscoroutine(instance):
            instance.close()
        _raise_async_resolution_error(factory, stack=stack, returned_awaitable=True)
    return instance


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
