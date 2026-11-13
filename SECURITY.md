# Security Policy

minuta records meetings, so a bug here can expose conversations. Please report security issues
privately through [GitHub private vulnerability reporting](https://github.com/damianpeaf/minuta/security/advisories/new),
not in a public issue. Include the version (`minuta --version`), your OS and browser, and the steps
to reproduce. Reports are acknowledged and fixed as soon as possible.

Do not attach real recordings or transcripts to a report. A synthetic example is enough.

## What minuta protects

| Asset | Where it lives |
|---|---|
| Recordings, transcripts, `meeting.json` | The data folder, readable by your user |
| API keys and destination tokens | The OS keyring, or `.env` in the config folder with 0600 permissions |
| The server token | `server-token` in the config folder (0600), and the extension's local storage |

## Threat model

**In scope:**

- **Web pages and other origins.** The server listens only on `127.0.0.1`. It refuses a `Host`
  other than `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding), any browser `Origin` that is
  not a browser extension, and any request without the token except `/health` and `/pair`. It
  answers no CORS preflight.
- **Other browser extensions.** Pairing needs a 6-digit code shown in your terminal, valid for 10
  minutes and 5 attempts, stored only as a hash. The paired extension's origin is remembered and
  token requests from other extension origins are refused.
- **Secrets in logs and files.** Secrets never go in `config.toml`; every secret read or written is
  masked in logs, as are common credential shapes.
- **The minuta page.** The extension's own page (`app.html`) uses the paired extension's token and
  passes the same checks. It can change settings, add or remove destinations and start
  transcriptions, so the token is as sensitive as before. Secrets can be sent to the server but no
  endpoint returns one (only whether it is saved); job errors are redacted. The server opens only a
  meeting's own folder or transcript with the system's app, never a path from the request. Bodies of
  these endpoints are at most 64 KB. The page runs under a content security policy that loads
  nothing remote and connects only to `127.0.0.1`, and writes data with `textContent`. A new pairing
  code still comes only from the terminal.
- **Destinations.** `rclone` and `command` run programs with an argument list, never through a
  shell, and validate paths. The `command` destination passes a short allowlist of environment
  variables and never a variable whose value is a secret. Webhooks are signed (HMAC-SHA256 with a
  timestamp), require `https://` except for loopback, verify TLS and follow only same-origin
  307/308 redirects. Details in [docs/destinations.md](docs/destinations.md).
- **Malformed input from the extension:** meeting ids, chunk numbers, the start time, text fields
  and body sizes are validated; a body needs a `Content-Length`, `/pair` takes at most 1 KB and a
  client has 15 seconds to send its request. A crafted start time or title cannot place an
  Obsidian note outside its folder, and the title is written as one escaped Markdown line.
- **Files on disk.** On macOS and Linux the config, data and meeting folders are 0700 and
  `config.toml`, `.env` and the server token 0600.
- **Installing and updating.** The installers and `minuta update` install the dependency versions
  pinned in `uv.lock` (`constraints.txt`). The installer runs only once the whole script is
  downloaded, and replaces or resets the source folder only if it is empty or was made by the
  installer.

**Out of scope:**

- Malware or another program running as your user: it can read your files, your keyring and your
  extension storage anyway.
- Anyone with access to your unlocked computer.
- What a destination or provider does with data you chose to send it (Notion, a cloud drive, your
  webhook receiver, Vercel AI Gateway and the model provider behind it).
- Consent: telling other participants that you record is your responsibility (see the README).

For bugs that are not security issues, use the [issue templates](https://github.com/damianpeaf/minuta/issues/new/choose).
