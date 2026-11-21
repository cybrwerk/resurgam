# Contributing to RESURGAM

Thanks for your interest! RESURGAM is a public beta, so feedback from real PBS/PVE environments is
the most valuable contribution right now.

## Ways to help

- **Report bugs**: open a [bug report](https://github.com/cybrwerk/resurgam/issues/new/choose).
  Include your RESURGAM, PBS and PVE versions.
- **Suggest features**: open a feature request and describe the problem you want solved, not
  only the solution.
- **Ask questions** in [Discussions](https://github.com/cybrwerk/resurgam/discussions).
- **Security issues**: never in public issues. See [SECURITY.md](./SECURITY.md).

## Pull requests

For anything bigger than a small fix, please open an issue first so we can agree on the approach
before you invest time.

1. Fork, create a branch from `main`.
2. Set up the components you touch. [README.md](./README.md#development) has the commands, and each
   component's own README has the details.
3. Make your change, with tests.
4. Make sure the same checks CI runs pass locally:

   ```bash
   # backend (needs the dev Postgres from backend/docker-compose.dev.yml)
   cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
   # restore-worker
   cd restore-worker && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
   # frontend
   cd frontend && npm run test && npm run build
   ```

5. Update the docs your change affects. The README, `docs/architecture-and-dependencies.md` and
   `docs/manual/index.md` are kept current with the code. Architectural decisions get an ADR in
   `docs/adr/` (ADRs are append-only: never rewrite one; add an addendum or a superseding ADR).
6. Open the pull request and fill in the template.

### Conventions

- **Commit messages:** `type: short summary` (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`).
- **API:** every FastAPI route and Pydantic field needs a description that makes sense on its own
  in the generated `/docs`.
- **UI text:** add every user-visible string to both `frontend/src/i18n/locales/en.json` and
  `de.json`.
- **Never** commit real hostnames, tokens, customer names or other data from your environment,
  including in tests and screenshots. Use `example`/`demo` values.

## Developer Certificate of Origin (DCO)

RESURGAM is licensed under the [GNU AGPL-3.0-or-later](./LICENSE). By contributing, you certify
that you wrote the change or otherwise have the right to submit it under that license, as stated
in the [Developer Certificate of Origin 1.1](https://developercertificate.org/).

You confirm this by adding a `Signed-off-by` line to **every commit**:

```text
Signed-off-by: Jane Doe <jane@example.com>
```

`git commit -s` adds it automatically, using your `user.name` and `user.email`. Pull requests with
unsigned commits cannot be merged. To sign off commits after the fact, run
`git rebase --signoff main` and force-push your branch.

## Code of Conduct

This project follows our [Code of Conduct](./CODE_OF_CONDUCT.md). By participating, you agree to
uphold it.
