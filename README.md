# miniject

Lightweight dependency injection container for Python. Auto-wires constructor
dependencies from type hints, supports singleton and transient scopes, and
provides scoped child containers for testing and per-context overrides.

**~160 lines. Zero dependencies. Fully typed.**

## Installation

```bash
pip install miniject
```

Or from source:

```bash
pip install git+https://github.com/dstarman/miniject.git
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
| `bind(SomeType, factory=fn, singleton=True)` | Custom factory (singleton) |

**Auto-wiring** inspects constructor parameters via `typing.get_type_hints()`
and resolves each typed parameter from the container. Parameters with default
values are left to Python when no binding exists.

### `container.resolve(service, **overrides)`

Resolve a service, recursively auto-wiring all dependencies. Keyword
`overrides` are passed directly to the factory/constructor, bypassing the
container for those parameters.

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
and populate the container at application start, then resolve the object graph
once. There is no locking around singleton creation. If you resolve the same
singleton concurrently from multiple threads during startup, you may get
duplicate instantiation. In practice this is not an issue when the container
is fully resolved before serving requests.

## Comparison

miniject occupies a specific niche — if you need more, use a larger framework:

| Feature | miniject | dependency-injector | lagom | punq |
|---------|----------|-------------------|-------|------|
| Auto-wiring from type hints | ✅ | ✅ | ✅ | ✅ |
| Scoped child containers | ✅ | ✅ | ❌ | ❌ |
| Circular dep detection | ✅ | ❌ | ❌ | ❌ |
| Async support | ❌ | ✅ | ✅ | ❌ |
| Decorators / markers | ❌ | ✅ | ✅ | ❌ |
| Dependencies | 0 | 1 | 0 | 0 |
| Lines of code | ~160 | ~15k | ~3k | ~500 |

## License

MIT
