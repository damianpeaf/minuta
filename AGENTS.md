# Agent instructions

Start here if you are an AI agent working on this repository.

- **What it is:** a browser extension (`extension/`, Manifest V3, plain JavaScript) that records
  Google Meet, and a Python package (`src/minuta/`) with a local server, a CLI, destinations and
  transcription. The architecture is in [docs/architecture.md](docs/architecture.md).
- **Setup:** `uv sync --extra local`. Lint with `uv run ruff check`, test with `uv run pytest`
  (fast tests only; other markers are listed in the README's Development section).
- **Adding a destination:** read [docs/destinations.md](docs/destinations.md#writing-your-own)
  and follow an existing adapter and its test.

Conventions:

- **No private data, ever.** Do not commit personal data, credentials or real ids (people,
  companies, hosts, meeting titles, recordings, transcripts) in code, tests, fixtures, docs or
  commit messages; use placeholder values.
- Code, comments, logs and files are in English. User-facing CLI strings live in
  `src/minuta/i18n.py` in English and Spanish (neutral Spanish with "tú").
- Secrets never go in `config.toml`, logs or test output: use the secret store and `redact`.
- Never run anything through a shell (`shell=True`); build argument lists.
- Keep tests with the behavior they verify; tests must not need the network unless they carry a
  `*_live` marker.
- Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`),
  imperative, in English.
- Documentation must match the real `--help`: check `uv run minuta <command> --help` before
  documenting a flag.
