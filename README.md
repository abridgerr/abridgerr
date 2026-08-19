# abridgerr

Watches a folder for `.mkv` files and re-encodes each one onto a fixed
59.94fps (60000/1001) timeline, speeding up dialog and non-dialog stretches
by different amounts rather than leaving dialog untouched: dialog plays a
gentle 1.25x faster (a 1:2 source-to-output frame ratio), while non-dialog
stretches jump to 5x by default or 2.5x optionally (2:1 or 1:1 frame
ratios) — all resyncing subtitles to the new timeline. Runs as a
long-lived Docker container that hooks into Radarr/Sonarr to figure out
each title's language and any per-title speed override, rather than
needing that configured by hand.

## How it works

- `watch.py` watches one or more input folders (via inotify) for `.mkv`
  files. Once a file has gone quiet for 10s with no further filesystem
  events (i.e. a copy/download has actually finished), it hands the file
  to `abridge.py` and writes the result into the matching output folder.
- `abridge.py` does the actual work: detects dialog vs. non-dialog
  segments and re-times each onto a fixed 59.94fps (60000/1001) output
  timeline using exact source-to-output frame ratios rather than a
  continuous speed multiplier. For a standard 23.976fps (24000/1001)
  source, dialog segments use a 1:2 ratio (1.25x), and non-dialog segments
  use 2:1 (5x) by default or, optionally, 1:1 (2.5x). Other common source
  rates — e.g. 29.97fps (30000/1001) — are handled the same way, just
  scaled to whatever ratio lands cleanly on that fixed 59.94fps target.
  Everything is then re-encoded (hardware-accelerated via Intel QSV/VAAPI
  when available, falling back to CPU), with subtitles resynced/embedded
  — including OCR (via `pgsrip` + Tesseract) for image-based PGS/Blu-ray
  subtitle tracks.
- Each input/output pair is tagged `movies` or `shows`, which tells
  `watch.py` which Radarr or Sonarr instance to ask for that title's
  original language. Speed and language can be overridden per-title with
  Radarr/Sonarr tags:
  - `abridgerr-speed-<high|low|dialog>`
  - `abridgerr-lang-<code>` (e.g. `abridgerr-lang-eng`)
- A file `abridge.py` fails on gets tagged `abridgerr-failed` directly on
  its Radarr/Sonarr entry — there's no separate state file. Remove the tag
  in Radarr/Sonarr to retry it.

## Requirements

- Docker
- A Radarr and/or Sonarr instance reachable from the container, each with
  a dedicated root folder that titles you want abridged get imported
  into/moved to (this is how `watch.py` matches a file on disk back to a
  Radarr/Sonarr entry).
- Optional: an Intel iGPU passed through as `/dev/dri` for QSV/VAAPI
  hardware encode/decode. Falls back to CPU encoding otherwise.

## Setup

1. Copy `config.example.json` to `config/config.json` and adjust the
   `input`/`output` paths to match folders inside your media mount (see
   `pairs` below).
2. Copy `docker-compose.example.yml` to `docker-compose.yml` (or add the
   service to your existing stack), set your media mount path, and set
   `RADARR_API_KEY`/`SONARR_API_KEY` (e.g. via a `.env` file).
3. `docker compose up -d` — this pulls the published
   `ghcr.io/abridgerr/abridgerr:latest` image. Swap in `build: .` instead
   if you'd rather build it yourself from this repo.

## Image

Published automatically on every push to `master` via GitHub Actions:

```
ghcr.io/abridgerr/abridgerr:latest
```

Tagged images (`vX.Y.Z`) are also published for any pushed `v*.*.*` git
tag.

## config.json

```json
{
    "min_free_gb": 20,
    "pairs": [
        {
            "input": "/mnt/media/toabridge-movies",
            "output": "/mnt/media/abridged-movies",
            "type": "movies",
            "default_speed": "high",
            "log_level": "info"
        }
    ]
}
```

| Field | Description |
|---|---|
| `min_free_gb` | Skip processing (retrying later) if the output filesystem has less than this many GB free. `0` disables the check. |
| `pairs[].input` / `output` | Folders to watch / write results to (paths as seen *inside* the container). Subfolder structure is preserved. |
| `pairs[].type` | `movies` or `shows` — selects the `RADARR_*` or `SONARR_*` env vars used to look up each file's title. |
| `pairs[].default_speed` | `high`, `low`, or `dialog` — used when a title has no `abridgerr-speed-*` tag. Default `high`. |
| `pairs[].log_level` | `none`, `error`, `info`, or `debug` — controls the per-file `.log` written next to each output. Default `info`. |
| `pairs[].extra_args` | Optional list of extra CLI args passed straight through to `abridge.py`. |

## Environment variables

| Variable | Description |
|---|---|
| `PUID` / `PGID` | User/group ID the container runs as (linuxserver.io-style). Default `1000`/`1000`. |
| `TZ` | Timezone, e.g. `Europe/Madrid`. Default `Etc/UTC`. |
| `RADARR_URL` / `RADARR_API_KEY` | Required for any pair with `"type": "movies"`. |
| `SONARR_URL` / `SONARR_API_KEY` | Required for any pair with `"type": "shows"`. |
| `ABRIDGE_CONFIG` | Path to the config file inside the container. Default `/config/config.json`. |

## Running `abridge.py` standalone

The watcher is just a wrapper — `abridge.py` can also be run directly
against a single file or a directory for one-off/batch use, outside of
the watch/Radarr/Sonarr flow entirely:

```
python3 app/abridge.py input.mkv output.mkv --speed high --lang eng --embed-subs
```

Run `python3 app/abridge.py --help` for the full set of options (encoder
selection, subtitle/audio track selection, burn-in vs. soft subs,
segment dump/debug flags, etc).
