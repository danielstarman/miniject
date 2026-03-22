"""Lightweight dependency injection container.

Provides constructor-based auto-wiring, singleton/transient scopes, and scoped
child containers for testing and experiment overrides. Only composition roots
should call ``.resolve()``; all other code receives dependencies via constructor
injection.
"""

from miniject._container import Container, ResolutionError

__all__ = ["Container", "ResolutionError"]
