# AGENTS.md

## Purpose

This repository builds `miniject`, a small, typed dependency injection
container for Python with constructor auto-wiring, singleton/transient
lifetimes, and scoped child containers.

The goal is not to be the most feature-rich DI framework. The goal is to keep
the container small, predictable, and honest about what it does.

## Source Of Truth

Use these sources for different purposes:

1. `README.md`
   Canonical source for product intent, public API shape, design philosophy,
   and stated non-goals.

2. Code
   Source of truth for what is actually implemented today.

3. GitHub issues
   Source of truth for planned work, sequencing, and open design questions.

4. `CONTRIBUTING.md`
   Source of truth for local workflow, checks, and repository policy.

Do not assume these always match perfectly during active development.

If they diverge:
- do not silently pick whichever is most convenient
- reconcile the mismatch in a way that keeps the library coherent
- do not treat the README as something that must be updated for every internal detail

## Design Principles

- Preserve clear DI semantics over convenience.
- Prefer minimal container magic and explicit behavior.
- Keep the public API small and easy to hold in your head.
- Favor explicit factories when construction logic is policy-driven or non-trivial.
- When relying on auto-wiring, use type hints that are resolvable at runtime; otherwise prefer an explicit factory.
- Fail explicitly when dependencies cannot be resolved.
- Keep child-scope behavior and singleton ownership semantically honest.
- Prefer predictable behavior and stable errors over clever inference.
- Protect the library's narrow scope; do not casually grow it into a framework.

## Working Style

- Keep the big picture in mind while implementing local changes.
- If the correct solution is broader than the immediate task, flag it and follow the broader design when justified.
- Do not force implementations into an artificially narrow scope when the surrounding design suggests a better abstraction.
- Refactoring is normal and expected when it improves clarity, ownership, or correctness.
- Leave the code better than you found it.
- Prefer complete changes over temporary compatibility layers.
- Do not add compatibility shims by default.
- When interfaces change, update in-repo callers, tests, and README examples in the same change when practical.

## Implementation Guidance

- Prefer simple, testable units and explicit data flow.
- Keep core container semantics separate from helper or workflow code.
- Keep binding registration, resolution, lifetime management, and error reporting conceptually distinct.
- Prefer inspectable behavior over hidden magic.
- Mirror source modules with corresponding test modules in `tests/` when adding or splitting code.
- Prefer behavior-based test names in the form `MethodUnderTest_BehaviorBeingTested_ExpectedResult`.
- Prefer an explicit `Arrange`, `Act`, `Assert` test structure when it improves readability; use `Act + Assert` when those steps are naturally combined.
- Do not treat passing tests alone as sufficient if intended DI semantics would be violated.
- When the same semantic concept appears in multiple places, prefer promoting it into a named model over repeating ad hoc flags or tuples.
- Prefer narrow, honest abstractions over generic extension mechanisms.

## Library Guardrails

- `resolve()` belongs at composition roots, not throughout application code.
- Constructor parameters should represent real semantic dependencies, not container selection tricks.
- Parent-defined singletons should remain shared through child scopes unless a child intentionally re-binds the service.
- Prefer building a new container or child scope over mutating a shared container in place.

## Checks

Before concluding work, read and follow the relevant workflow and validation guidance in
`CONTRIBUTING.md`.

Use judgment about scope, but do not skip validation casually when behavior or public
semantics have changed.

## Agent Behavior

- Do not weaken guarantees to make a task easier.
- Do not silently relax resolution rules or lifetime semantics.
- When unsure, choose the interpretation that better preserves API clarity and architectural honesty.
- Surface meaningful ambiguities instead of papering over them.
- Prefer changes that keep `miniject` small, understandable, and explicit.
