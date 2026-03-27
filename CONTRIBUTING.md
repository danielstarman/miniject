# Contributing

## Setup

Create a repo-local virtualenv and install the dev dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Install [`repocert`](https://crates.io/crates/repocert) separately if you want
the certification workflow and local Git hook enforcement:

```bash
cargo install --locked --force repocert
```

## Checks

The repo contract in `.repocert/config.toml` mirrors the normal development
checks:

- `.venv/bin/python -m ruff format --check .`
- `.venv/bin/python -m ruff check .`
- `.venv/bin/python -m pyright --pythonpath .venv/bin/python -p .`
- `.venv/bin/python -m pytest -q`

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
