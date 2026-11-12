# Troubleshooting

Start with these two; most problems show up in one of them:

```bash
minuta status
minuta doctor
```

## Where the logs are

| | Server log | Service output |
|---|---|---|
| macOS | `~/Library/Logs/minuta/server.log` | `~/Library/Logs/minuta/server.out.log`, `server.err.log` |
| Linux | `~/.local/state/minuta/log/server.log` | `journalctl --user -u minuta.service` |
| Windows | `%LOCALAPPDATA%\minuta\Logs\server.log` | - |

Secrets are masked in every log. The extension's own messages are in the extensions page: minuta
card, *Inspect views: service worker*, Console.

## The extension

**The popup says "minuta is not answering".**
The local server is down or on another port. `minuta status` shows both. `minuta service restart`
starts it; `minuta doctor` says why it does not. If another program uses the port, change
`[server] port` in `config.toml`, run `minuta service restart`, and set the same port in the popup
(*Port*). While the server is down the extension keeps up to about three hours of audio in memory
and sends it when the server is back; beyond that, chunks are lost and the popup says how many.

**The popup asks for a pairing code, or says "Not paired".**
Run `minuta pair` and type the code in the popup. Codes last 10 minutes and allow 5 attempts.
`minuta pair --rotate` creates a new token, so every browser has to pair again.

**Nothing is recorded when I join a call.**
Reload the Meet tab. The recorder is attached when the tab loads, so tabs opened before the
extension was installed, reloaded or updated are not recorded. Check that the minuta card in the
extensions page is enabled and shows no errors.

**"Cannot find Meet's microphone button: your voice is not recorded."**
minuta records your voice only when Meet confirms you are unmuted. If it cannot find the button
(Meet changed its page, or the call is shown in an unusual layout), your channel stays silent.
Reload the tab; if it persists, [open an issue](https://github.com/damianpeaf/minuta/issues/new/choose)
with your browser version.

**"The others' voices are arriving but are not being recorded."**
Speech is arriving over the network but the others' channel is silent. Reload the Meet tab. If
the others' channel ends up silent, the meeting gets a warning and its local audio is not deleted
automatically.

**"No microphone found."**
Meet has no microphone: check the browser's microphone permission for meet.google.com.

## Meetings

**A meeting says `error` in `minuta list`.**
`meeting.json` in its folder has the `error`. The most common cause is ffmpeg missing for the
service: `minuta doctor` checks it. The service runs with a minimal `PATH` plus Homebrew and
`~/.local/bin`; an ffmpeg installed elsewhere may need a link in one of those.

**"incomplete recording: no audio from the other participants was captured".**
Other participants were in the call but their channel has no sound. Usually the Meet tab was open
before the extension was (re)loaded. The local audio is kept.

## Destinations

**A new destination never receives older meetings.**
The service picks up a new destination from the next meeting on, without a restart. Run
`minuta retry` to send it the meetings recorded before it was added.

**A delivery failed.**
`minuta list` marks it (`[failed: NAME:meeting]`) and `minuta list --json` shows the state per
destination; `minuta destinations test NAME` runs the check;
`minuta retry` sends again every failed step (or `minuta retry <id>` for one meeting). Errors
that need you, such as a revoked token or a signed-out rclone remote, are marked permanent and
are not retried until you fix them and run `minuta retry`. Per destination details are in
[destinations.md](destinations.md).

**Notion refuses the audio.**
Free Notion workspaces take files up to 5 MB, about ten minutes of audio. Use a paid plan, or
`--send metadata,transcript` for that destination.

## Transcription

**Local transcription is slow.**
On a CPU the default is `small`. A GPU makes a large difference: on Linux and Windows with an
NVIDIA card, install with the `cuda` extra (`MINUTA_EXTRAS=local,cuda` for the installer; the Linux
installer adds it when `nvidia-smi` is found). `minuta doctor` shows which engine is installed.

**"faster_whisper is not installed" (or `mlx_whisper`).**
minuta was installed without the `local` extra. Run the installer again, or set
`install_extras = "local"` under `[general]` and run `minuta update`.

**The transcript invents phrases or misspells names.**
Set `[transcription] language`, a one-sentence `context` and a `glossary`. The
[meeting-transcripts skill](../skills/meeting-transcripts/SKILL.md) describes a fuller process.

**Vercel: "over the cap" or "Pass --yes".**
`minuta transcribe --estimate` shows the cost; raise the cap with `--max-usd` or `max_usd`, and
pass `--yes` when there is no terminal to confirm in.

**Vercel: "AI_GATEWAY_API_KEY is not set".**
Run `minuta setup` again and choose `vercel`, or set the `AI_GATEWAY_API_KEY` environment variable.

## Starting over

`minuta setup` can run again at any time and keeps your answers as defaults. To remove everything,
see [Update and uninstall](../README.md#update-and-uninstall).
