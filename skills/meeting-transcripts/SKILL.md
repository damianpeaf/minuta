---
name: meeting-transcripts
description: Optional workflow for getting accurate transcripts of meetings recorded with minuta. Find a meeting, transcribe it, build a per-meeting glossary, keep a dictionary of frequent errors, and run a careful correction pass that does not summarize. Use when asked to transcribe a minuta recording, improve a transcript's accuracy, or fix names and jargon in one.
---

# Meeting transcripts with minuta

A recommendation, not a requirement: minuta works without any of this. Follow it when a transcript
has to be accurate (names, numbers, decisions) rather than just searchable.

## 1. Find the meeting

```bash
minuta list                 # id, start, minutes, status, transcript or not, title, failed deliveries
minuta list --json          # the same, plus delivery state, for scripts
```

Each meeting is a folder `<data>/meetings/<id>/` with `meeting.json`, the audio
(`audio.opus` or `audio.mp3`) and, once transcribed, `transcript.md`, `.json` and `.vtt`.
`minuta status` prints the data folder. A meeting can be referred to by its full id, the start of
its id, or `latest`.

## 2. Build a glossary for this meeting

Before transcribing, write down what the model cannot guess:

- **Context:** one sentence, in the meeting's language, saying what the meeting is about. A
  sentence in the right language also keeps the model from drifting into another one.
- **Glossary:** 30 to 40 terms that will be said: people's names, product and project names,
  acronyms, tools, places. Most important first.

Whisper models read both as a prompt limited to about 224 tokens; anything past that is cut. Keep
the context short and drop terms that any model spells right anyway. Other providers receive the
glossary in their own format.

Put them in `config.toml` for the run (and restore the general ones afterwards if they differ):

```toml
[transcription]
language = "en"
context = "Planning meeting of the data team about the March migration."
glossary = ["Postgres", "dbt", "Airflow", "Lena", "Marcus", "Q2 roadmap"]
```

## 3. Transcribe

```bash
minuta transcribe <id>                     # local engine, free
minuta transcribe <id> --force             # again, after changing the glossary
minuta transcribe <id> --engine vercel --estimate   # paid engine: see the cost first
minuta transcribe <id> --from 10 --to 25   # only minutes 10 to 25, to test settings quickly
```

Try the glossary on a short range first; transcribe the whole meeting once it looks right.

## 4. Keep a dictionary of frequent errors

Keep a plain file (outside the minuta repository) with the mistakes that come back meeting after
meeting, as `heard -> correct`:

```text
cube control -> kubectl
graph Ana -> Grafana
Marcos -> Marcus        (only for the team meeting)
```

When a term keeps failing, add it to the glossary. When the glossary is full, the dictionary is
what the correction pass uses.

## 5. Correction pass with an agent

Give an agent `transcript.md`, the context, the glossary and the dictionary, with these rules:

- Fix only what is clearly a transcription error: misspelled names and terms, words that do not
  fit the sentence, entries from the dictionary.
- **Do not summarize, shorten, reorder or "polish".** Keep filler words, repetitions and broken
  sentences: they are what was said.
- Do not change speakers or timestamps. Do not merge or split turns.
- When unsure, keep the original and mark it with `[?]`, for example `we move it to Tuesday [?]`.
- Return the full transcript and, separately, the list of changes (`before -> after`).

Review the change list, not the whole text. Write the corrected version next to the original
(for example `transcript.corrected.md`) instead of overwriting it, so `minuta transcribe --force`
and the destinations keep working on minuta's own files.

## Privacy

Transcripts contain other people's words. Keep the glossary, the dictionary and the corrected files
where the recordings are, and do not paste them into tools or services the participants did not
agree to.
