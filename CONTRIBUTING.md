# Contributing

## Setup

Install [`uv`](https://docs.astral.sh/uv/) and sync the project environment:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

Install [`repocert`](https://crates.io/crates/repocert) separately if you want
the certification workflow and local Git hook enforcement:

```bash
cargo install --locked --force repocert
```

## Checks

The repo contract in `.repocert/config.toml` mirrors the normal development
checks:

- `uv run --locked ruff format --check .`
- `uv run --locked ruff check .`
- `uv run --locked pyright -p .`
- `uv run --locked pytest -q`

You can run them through `repocert`:

```bash
repocert validate
repocert check
repocert fix
repocert status --assert-certified
```

This repo uses a repo-scoped `ssh-signed` repocert key. To certify a commit,
pass the matching private SSH key whose public key is listed in
`.repocert/config.toml`, or set `REPOCERT_SIGNING_KEY` to that key path:

```bash
repocert certify --signing-key /path/to/private/key
```

`uv sync` creates a repo-local `.venv` next to `pyproject.toml`, so run it once
per worktree or fresh checkout.

## Hooks

This repo uses `repocert` generated hooks:

- `pre-commit` and `pre-merge-commit` enforce local policy
- `pre-push` and `update` enforce certification for protected refs

To activate them in your local checkout:

```bash
repocert install-hooks
```

Hook installation is checkout-local, so run it once per checkout or worktree
where you want enforcement.

The current `repocert` generated `pre-commit` hook enforces local policy but
does not automatically run the repo's full `repocert check` profile. Run
`repocert check` explicitly before certification and push.
