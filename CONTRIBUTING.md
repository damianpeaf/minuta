# Contributing

Thanks for helping. Please read the [privacy rule](#no-private-data) first: minuta handles other
people's conversations, and the repository must never hold any.

## Set up

You need [uv](https://docs.astral.sh/uv/), ffmpeg and, for the extension tests, Node.js 22.

```bash
git clone https://github.com/damianpeaf/minuta && cd minuta
uv sync --extra local          # add --extra cuda on Linux or Windows with an NVIDIA GPU
uv run minuta --help
```

To try changes end to end, run the server from the checkout and load `extension/` unpacked in
Chrome, Edge or Brave:

```bash
uv run minuta setup --no-service   # once
uv run minuta serve
uv run minuta pair
```

To keep this apart from an installed minuta, export `MINUTA_HOME=/tmp/minuta-dev` (config, data and
logs go there) and pick another port with `setup --port` (and the same port in the popup). After changing the extension, click reload on its card in the extensions page and reload the Meet
tab.

## Tests

```bash
uv run ruff check
uv run pytest                                 # fast tests, what CI runs on every OS
uv run playwright install chromium
uv run pytest -m "browser and not slow"       # the extension and capture in Chromium
```

The other markers (`slow`, `service`, `rclone_live`, `notion_live`) need a model download, a real
background service, Docker or a Notion token; the README's
[Development](README.md#development) section has the commands. CI runs the fast tests on Linux,
macOS and Windows, the browser tests, and both installers from the checkout.

## No private data

Do not commit personal data, credentials or real ids; use placeholder values.

- Use made-up names in tests, fixtures, examples and docs.
- Never commit recordings, transcripts, `meeting.json` files from real meetings, tokens, keys or
  screenshots of real calls.

## Adding a destination

1. Read [docs/destinations.md](docs/destinations.md#writing-your-own): the interface, and the rules
   for `check()`, `configure()`, idempotent sends, errors, receipts and secrets.
2. Write the class in `src/minuta/destinations/<name>.py` and register it as an entry point in
   `[project.entry-points."minuta.destinations"]` in `pyproject.toml`. (A destination can also live
   in its own package with only the entry point.)
3. Add `tests/test_<name>.py` in the shape of `tests/test_webhook.py` or `tests/test_obsidian.py`:
   no network beyond a local endpoint or a fake, `check()` without side effects, and a second send
   with the previous receipt that does not create a duplicate. If the destination holds the audio,
   test `fetch_audio()`. Tests against a real service go behind their own `*_live` marker.
4. Add a section to `docs/destinations.md` and a row to the README's destinations table.

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/) in English, imperative:
  `feat: ...`, `fix(cli): ...`, `docs: ...`, `test: ...`, `refactor: ...`, `chore: ...`.
- One commit per unit of work, with its tests in the same commit.
- Fill the pull request template. Documentation that names a command or flag must match
  `uv run minuta <command> --help`.

## Licensing

minuta is under the [MIT license](LICENSE). By opening a pull request you confirm that you can
submit your contribution under it.
