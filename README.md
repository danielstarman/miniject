# miniject

Lightweight dependency injection container for Python. Auto-wires constructor
dependencies from type hints, supports singleton and transient scopes, and
provides scoped child containers for testing and per-context overrides.

**Small codebase. Zero dependencies. Fully typed.**

## Installation

```bash
pip install miniject
```

## Quick start

```python
from miniject import Container

class Database:
    def __init__(self, url: str = "sqlite:///:memory:") -> None:
        self.url = url

class UserRepo:
    def __init__(self, database: Database) -> None:
        self.database = database

class UserService:
    def __init__(self, repo: UserRepo) -> None:
        self.repo = repo

container = Container()
container.bind(Database, instance=Database("postgres://localhost/mydb"))
container.bind(UserRepo)       # auto-wired from type hints
container.bind(UserService)    # resolves UserRepo → Database automatically

service = container.resolve(UserService)
assert service.repo.database.url == "postgres://localhost/mydb"
```

## API

### `Container()`

Create a new container.

### `container.bind(service, *, factory=..., instance=..., singleton=...)`

Register a service type. Four modes:

| Call | Behavior |
|------|----------|
| `bind(SomeType)` | Auto-wire from `__init__` type hints (transient) |
| `bind(SomeType, instance=obj)` | Singleton by instance |
| `bind(SomeType, factory=fn)` | Custom factory (transient) |
| `bind(SomeType, factory=fn, singleton=True)` | Custom factory (singleton, shared by child scopes) |

**Auto-wiring** inspects constructor parameters via `typing.get_type_hints()`
and resolves each typed parameter from the container. Parameters with default
values are left to Python when no binding exists. Nullable dependencies such as
`Database | None = None` are supported: if `Database` is bound it is injected,
otherwise Python keeps the default `None`.

Common scalar builtins like `int`, `str`, `float`, `bool`, and `bytes` are not
supported as DI keys. Prefer typed value objects or explicit factories for
scalar configuration values.

Type hints must be importable at runtime. If `get_type_hints()` cannot resolve
an annotation, miniject raises `ResolutionError` instead of silently skipping
injection. `Annotated[...]` is intentionally unsupported. miniject does not
provide qualifier-style multiple bindings for the same base type. If two
dependencies mean different things, model them as different types. If the
distinction is construction logic, use an explicit factory.

## Design Philosophy

miniject prefers minimal container magic:

- constructors should describe semantic dependencies, not container selection rules
- composition roots and factories should own non-trivial wiring decisions
- if two dependencies mean different things, they should usually be different types
- if construction depends on runtime policy, use an explicit factory rather than metadata

### `container.resolve(service, **overrides)`

Resolve a service, recursively auto-wiring all dependencies. Keyword
`overrides` are passed directly to the factory/constructor, bypassing the
container for those parameters.

Overrides are only supported for non-singleton resolutions. Resolving a
singleton binding with overrides raises `ResolutionError`; use a child scope
or an explicit factory when construction needs per-call inputs.

Raises `ResolutionError` on missing bindings or circular dependencies, with
a full dependency chain in the message.

### `container.scope()`

Create a child container that inherits all parent bindings. Overrides in the
child do not affect the parent.

```python
parent = Container()
parent.bind(Database, instance=production_db)
parent.bind(UserRepo)

child = parent.scope()
child.bind(Database, instance=test_db)    # override in child only

child.resolve(UserRepo).database   # → test_db
parent.resolve(UserRepo).database  # → production_db
```

Singletons defined in the parent remain shared when resolved through a child
scope. If a child re-binds a service, that override is isolated to the child.

Use cases:
- **Testing** — swap specific dependencies without rebuilding the whole graph
- **Per-request isolation** — override config for a specific context

### `ResolutionError`

Raised when resolution fails. The message includes the full dependency chain:

```
ResolutionError: Cannot resolve UserRepo: missing binding for parameter 'database'
  (type=Database) (UserService -> UserRepo)
```

Circular dependencies are detected and reported:

```
ResolutionError: Circular dependency: A -> B -> A
```

## Composition root pattern

Only composition roots (startup code, CLI entrypoints, test fixtures) should
call `container.resolve()`. All other code receives dependencies via constructor
injection:

```python
# src/myapp/container.py — composition root
from miniject import Container

def create_container(config: Config) -> Container:
    c = Container()
    c.bind(Config, instance=config)
    c.bind(Database, factory=lambda: Database(config.db_url), singleton=True)
    c.bind(UserRepo)
    c.bind(UserService)
    return c

# src/myapp/services.py — normal code, no container import
class UserService:
    def __init__(self, repo: UserRepo) -> None:
        self.repo = repo
```

## Thread safety

miniject is designed for the **composition-root-at-startup** pattern: build
and populate the container at application start, then share it for resolution.
Concurrent `resolve()` calls are safe after configuration is complete, and
singleton factories are initialized at most once per owning container.

Rebinding services on a container that is already being shared across threads is
not supported. If you need runtime reconfiguration, build a new container or a
child scope instead of mutating a shared container in place.

## When to use miniject

miniject is a good fit when you want:

- constructor injection from type hints
- a tiny composition-root container with very little magic
- child scopes for tests and context-specific overrides
- explicit failure when runtime annotations are not actually resolvable

miniject is probably **not** the right fit when you need:

- async/resource lifecycle management
- framework integration or function/method wiring
- multiple qualified bindings for the same base type
- extensive provider types, configuration loaders, or container metaprogramming

## Comparison

miniject is intentionally narrower than larger Python DI frameworks.

- Compared with `dependency-injector`, miniject is much smaller and easier to
  hold in your head, but it does not try to compete with provider graphs,
  configuration providers, wiring, async resources, or broader framework
  integrations.
- Compared with `lagom`, miniject is more opinionated and lower-surface-area.
  Lagom supports async usage, richer integrations, and more advanced type-driven
  behavior. miniject aims to stay focused on composition-root constructor
  injection.
- Compared with `punq`, miniject lives in a more similar simplicity tier. The
  main differences are miniject's scoped child containers, circular dependency
  detection, and stricter stance on runtime-resolvable type hints.

The goal is not to be the most powerful DI library. The goal is to be a small,
predictable one that stays useful without turning into a framework.

## License

MIT
