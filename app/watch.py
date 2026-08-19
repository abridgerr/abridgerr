#!/usr/bin/env python3
"""
watch.py -- event-driven watcher for abridge.py, configured via config.json.

Monitors a set of input folders (bind-mounted) for .mkv files, and once a
file has gone QUIET_SECONDS with no further filesystem events touching it
(i.e. a copy/download into the folder has actually finished, not just
paused), runs abridge.py against it, writing output into the corresponding
output folder with the same subfolder structure preserved.

Each pair's speed and language come from Radarr/Sonarr, not static config:
every pair has a "type" ("movies" or "shows"), which selects the matching
RADARR_URL/RADARR_API_KEY or SONARR_URL/SONARR_API_KEY env var pair (see
compose.yml). For a given file, the movie/series owning it (matched by
folder path) supplies the language via its own originalLanguage, and both
speed and language can be overridden per-title with an
abridgerr-speed-<high|low|dialog> or abridgerr-lang-<code> tag in
Radarr's/Sonarr's UI -- see resolve_arr_overrides. A pair's own
"default_speed" (falls back to "high") is only used when no speed tag is
present; there is no equivalent static fallback for language anymore -- a
file with no Radarr/Sonarr match and no original-language data simply can't
be processed (see check_and_process_file) until it's actually imported
through Radarr's/Sonarr's dedicated toabridge root folder.

A file abridge.py fails on gets tagged abridgerr-failed on its Radarr/Sonarr
entry directly (see set_arr_failed_tag) rather than remembered in a local
state file -- makes failures visible right where the title is already
managed, and "retry" is just removing the tag there, instead of needing to
edit a JSON file inside the container. There's deliberately no local
persistence at all anymore (an earlier version kept /state/watch_state.json
purely for this): a file that never matches a Radarr/Sonarr entry in the
first place has nowhere to record "failed" either way (nothing to tag), and
just re-logs a warning (de-duplicated in-process, see _warned_no_lang) on
every retrigger instead of being remembered across restarts -- acceptable
since nothing expensive is being skipped by not remembering it.

Per-file logs live NEXT TO the output (<out_dir>/<source-stem>.log), not in
a separate bind-mounted /logs tree (an earlier version did this) -- named
after the SOURCE file's own stem rather than the resolved "-ABRIDGEDxX"
output name on purpose: that suffix needs an ffprobe call abridge.py does
internally to resolve (deliberately not duplicated here, see the existing-
output glob check below), so the source stem is the only name known before
the run even starts, success or failure alike. Each pair's "log_level"
(error/debug/info/none, default "info") controls both whether a log gets
written and how much of the run's output it contains -- see
build_log_content.

Stability is a PER-FILE debounce timer (via the `watchdog` package for
inotify events), not a periodic full-tree poll: every relevant event for a
given file (re)starts a QUIET_SECONDS timer for that exact file; only once
it fires with no further events in between is the file actually checked
and (if needed) processed. This directly matches how a real copy behaves
-- confirmed empirically (a live watchdog test, not assumed from docs)
that a Windows Explorer / SMB copy writes directly to the final filename
(no temp+rename) and fires a steady stream of events throughout an active
transfer, so "quiet for N seconds" is a genuine signal the write finished,
not an arbitrary fixed wait that might catch it mid-copy. This also
correctly covers Radarr/Sonarr moving a file into a toabridge root folder
after the fact (re-routing an existing library title, rather than
downloading directly into one) -- confirmed live that this move is a
same-filesystem rename, which fires a real inotify event; there is no
Radarr/Sonarr webhook for this specific action (checked directly against a
real instance -- none of onGrab/onDownload/onUpgrade/onRename/onMovieAdded/
onMovieDelete/onMovieFileDelete fired for a root-folder change), so the
filesystem watcher is the only mechanism that sees it, which is exactly
why there's no periodic safety-net rescan either (a prior version had
one) -- an inotify event that's silently dropped (queue overflow) or a
pair not mounted at startup won't self-heal without a restart; accepted
tradeoff for not spawning a full abridge.py subprocess (which itself runs
ffprobe) on every already-finished file forever, which is what the
periodic version actually did in practice.

A hardlinked-in file (common for *arr apps sharing a filesystem with their
download client) produces a create event with no write/close activity to
react to -- its QUIET_SECONDS timer still fires normally (it's just never
retriggered), so it gets checked the same as any other file.

Every file's own timer is independent, but actually RUNNING abridge.py is
serialized process-wide through a single shared priority queue, drained by
one dedicated worker thread: several files going quiet at once (e.g. a
whole season copied together, or the initial startup scan of a folder
that already has a season sitting in it) queue up and process strictly
one at a time, the same guarantee a single-threaded poll loop gave for
free -- deliberate, since concurrent encodes would contend for the same
GPU/CPU encode resources. Priority-queued BY THE FILE'S OWN PATH, not
arrival order, specifically so a batch that all become ready around the
same moment processes in sorted order (S01E01 before S01E02 before ...)
rather than whatever arbitrary order their independent timer threads
happened to wake up in -- see process_queue_worker.
"""
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

KNOWN_PAIR_FIELDS = {"input", "output", "type", "default_speed", "log_level", "extra_args"}
LOG_LEVELS = {"error", "debug", "info", "none"}

# Container-wide (not per-pair) hardware-encoding choice, passed straight
# through to every abridge.py invocation as --encoder/--video-codec -- see
# docker-compose.example.yml for the values and what each requires on the
# host. Validated once here, at startup (see main()), rather than letting
# an unrecognized value surface as the same argparse error on every single
# file processed -- same reasoning as resolve_arr_env's up-front check.
ENCODER_CHOICES = {"auto", "qsv", "vaapi", "nvenc", "cpu"}
VIDEO_CODEC_CHOICES = {"h264", "hevc"}
ENCODER = os.environ.get("ENCODER", "auto")
VIDEO_CODEC = os.environ.get("VIDEO_CODEC", "h264")

# Selects which Radarr/Sonarr env vars a pair's "type" maps to -- see the
# module docstring and resolve_arr_overrides.
ARR_ENV_BY_TYPE = {
    "movies": ("radarr", "RADARR_URL", "RADARR_API_KEY"),
    "shows": ("sonarr", "SONARR_URL", "SONARR_API_KEY"),
}

_ARR_SPEED_TAG_RE = re.compile(r"^abridgerr-speed-(high|low|dialog)$", re.IGNORECASE)
_ARR_LANG_TAG_RE = re.compile(r"^abridgerr-lang-([a-z]{2,3})$", re.IGNORECASE)
_ARR_FAILED_TAG = "abridgerr-failed"

# How long a file must go with NO further filesystem events before it's
# considered done writing and gets checked for processing -- see the
# module docstring for why this is a per-file debounce, not a fixed
# one-shot delay or a periodic poll.
QUIET_SECONDS = 10.0


def log(msg):
    print(f"[watch] {msg}", flush=True)


def load_config(config_path):
    if not config_path.exists():
        sys.exit(f"Config file not found: {config_path}")
    try:
        config = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"Couldn't parse {config_path} as JSON: {e}")

    pairs = config.get("pairs", [])
    if not pairs:
        sys.exit(f"{config_path} has no 'pairs' entries -- nothing to watch.")

    parsed = []
    for i, pair in enumerate(pairs):
        unknown = set(pair) - KNOWN_PAIR_FIELDS
        if unknown:
            log(f"[!] pairs[{i}] has unrecognized field(s) {sorted(unknown)} -- "
                f"typo? Known fields: {sorted(KNOWN_PAIR_FIELDS)}. Ignoring them.")
        if "input" not in pair or "output" not in pair:
            log(f"[!] pairs[{i}] is missing 'input' or 'output' -- skipping this pair entirely.")
            continue
        log_level = pair.get("log_level", "info")
        if log_level not in LOG_LEVELS:
            log(f"[!] pairs[{i}] has log_level={log_level!r}, expected one of {sorted(LOG_LEVELS)} -- using 'info'.")
            log_level = "info"
        parsed.append({
            "input": Path(pair["input"]),
            "output": Path(pair["output"]),
            "type": pair.get("type"),
            "default_speed": pair.get("default_speed", "high"),
            "log_level": log_level,
            "extra_args": [str(a) for a in pair.get("extra_args", [])],
        })

    return {
        "min_free_gb": float(config.get("min_free_gb", 0)),
        "pairs": parsed,
    }


def resolve_arr_env(pair):
    """Attaches arr_type/arr_url/arr_api_key to a parsed pair dict, based on
    its "type" field and the matching RADARR_*/SONARR_* env vars (see
    ARR_ENV_BY_TYPE). Warns clearly at startup -- once, here, rather than
    letting every file in the pair fail one-by-one with a less obvious
    per-file message -- if "type" is missing/unrecognized, or the env vars
    it maps to aren't actually set."""
    pair["arr_type"] = pair["arr_url"] = pair["arr_api_key"] = None
    pair_type = pair["type"]
    if pair_type is None:
        log(f"[!] Pair {pair['input']} has no \"type\" set -- language can't be auto-detected from "
            f"Radarr/Sonarr, every file in this pair will fail until this is fixed. "
            f"Add \"type\": \"movies\" or \"shows\".")
        return
    if pair_type not in ARR_ENV_BY_TYPE:
        log(f"[!] Pair {pair['input']} has type={pair_type!r}, expected \"movies\" or \"shows\" -- "
            f"treating as unset (see above for what that means).")
        return
    arr_type, url_env, key_env = ARR_ENV_BY_TYPE[pair_type]
    arr_url, arr_api_key = os.environ.get(url_env), os.environ.get(key_env)
    if not arr_url or not arr_api_key:
        log(f"[!] Pair {pair['input']} has type={pair_type!r} but {url_env}/{key_env} aren't both "
            f"set -- language can't be auto-detected, every file in this pair will fail until "
            f"these are set (see compose.yml).")
        return
    pair["arr_type"], pair["arr_url"], pair["arr_api_key"] = arr_type, arr_url, arr_api_key


_arr_warned_urls = set()
_arr_warned_lock = threading.Lock()


def _arr_request(arr_url, arr_api_key, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Api-Key": arr_api_key}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{arr_url.rstrip('/')}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def resolve_arr_entry(arr_type, arr_url, arr_api_key, file_path):
    """Fetches the movie/series list + tag catalog, and returns the entry
    (movie or series dict) whose own folder is an ancestor of file_path
    (verified live: Radarr's/Sonarr's own root-folder-per-title path
    exactly matches what abridgerr sees, since both mount the same
    /mnt/media), with two extra keys attached: "tag_labels" (this entry's
    own tags resolved to label strings) and "_tag_catalog" (the full
    system-wide label->id map, so set_arr_failed_tag can find or create the
    abridgerr-failed tag without a second fetch). Returns None if there's
    no match or the request failed. Fetched fresh every call, no caching,
    so a tag change in Radarr's/Sonarr's UI takes effect on the very next
    check."""
    entries_path = "/api/v3/movie" if arr_type == "radarr" else "/api/v3/series"
    try:
        entries = _arr_request(arr_url, arr_api_key, entries_path)
        tags = _arr_request(arr_url, arr_api_key, "/api/v3/tag")
    except (urllib.error.URLError, ValueError, OSError) as e:
        with _arr_warned_lock:
            if arr_url not in _arr_warned_urls:
                _arr_warned_urls.add(arr_url)
                log(f"[!] Couldn't reach {arr_type} at {arr_url}: {e} -- using pair defaults for now "
                    f"(won't warn again for this instance until it's reachable again).")
        return None
    with _arr_warned_lock:
        _arr_warned_urls.discard(arr_url)  # reachable again -- allow one fresh warning if it drops again later

    id_to_label = {t["id"]: t["label"] for t in tags}
    file_str = str(file_path)
    entry = next((e for e in entries if e.get("path") and
                  (file_str == e["path"] or file_str.startswith(e["path"].rstrip("/") + "/"))), None)
    if entry is None:
        return None

    entry["tag_labels"] = [id_to_label[tid] for tid in entry.get("tags", []) if tid in id_to_label]
    entry["_tag_catalog"] = {t["label"]: t["id"] for t in tags}
    return entry


def resolve_arr_overrides(entry):
    """(speed_override, lang_override) from an already-matched entry's
    tag_labels + originalLanguage -- see resolve_arr_entry.
    abridgerr-speed-<mode> overrides the pair's own default_speed;
    abridgerr-lang-<code> overrides the language, and absent that tag, the
    entry's own originalLanguage becomes the language instead (via
    babelfish -- confirmed "English" -> alpha3 "eng" against the real API
    response shape). Either comes back None if not determinable -- callers
    fall back to their own default (speed) or treat it as unresolvable
    (lang, which has no static fallback)."""
    labels = entry["tag_labels"]
    title = entry.get("title", "?")

    speed_matches = [m.group(1).lower() for l in labels if (m := _ARR_SPEED_TAG_RE.match(l))]
    speed_override = None
    if len(speed_matches) == 1:
        speed_override = speed_matches[0]
    elif len(speed_matches) > 1:
        log(f"[!] {title!r} has conflicting speed tags {speed_matches} -- using the pair's default_speed instead.")

    lang_matches = [m.group(1).lower() for l in labels if (m := _ARR_LANG_TAG_RE.match(l))]
    lang_override = None
    if len(lang_matches) == 1:
        lang_override = lang_matches[0]
    elif len(lang_matches) > 1:
        log(f"[!] {title!r} has conflicting language tags {lang_matches} -- falling back to its original language.")
    else:
        original_name = (entry.get("originalLanguage") or {}).get("name")
        if original_name:
            try:
                from babelfish import Language
                lang_override = Language.fromname(original_name).alpha3
            except Exception:
                pass

    return speed_override, lang_override


def is_arr_failed(entry):
    return _ARR_FAILED_TAG in entry["tag_labels"]


def set_arr_failed_tag(arr_type, arr_url, arr_api_key, entry, failed):
    """Adds (failed=True) or removes (failed=False) the abridgerr-failed
    tag on entry -- a no-op if it's already in the desired state (avoids a
    pointless PUT on every successful run). Creates the tag definition
    itself via POST /api/v3/tag the first time it's ever needed. This is
    the ONLY record of a failure kept anywhere -- see the module docstring
    for why that replaced a local state file: visible right in Radarr's/
    Sonarr's own UI, and "retry" is just removing the tag there."""
    if is_arr_failed(entry) == failed:
        return
    tag_id = entry["_tag_catalog"].get(_ARR_FAILED_TAG)
    if tag_id is None:
        if not failed:
            return  # tag doesn't even exist yet, nothing to remove
        tag_id = _arr_request(arr_url, arr_api_key, "/api/v3/tag", method="POST",
                               body={"label": _ARR_FAILED_TAG})["id"]
    tag_ids = set(entry.get("tags", []))
    if failed:
        tag_ids.add(tag_id)
    else:
        tag_ids.discard(tag_id)
    entry["tags"] = sorted(tag_ids)
    resource = "movie" if arr_type == "radarr" else "series"
    put_body = {k: v for k, v in entry.items() if k not in ("tag_labels", "_tag_catalog")}
    _arr_request(arr_url, arr_api_key, f"/api/v3/{resource}/{entry['id']}", method="PUT", body=put_body)


def find_mkv_files(root):
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("**/*.mkv") if p.is_file())


def shutil_disk_free_gb(path):
    try:
        import shutil
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
    except OSError:
        return None


def run_abridge(cmd):
    """Runs abridge.py, streaming its combined stdout/stderr live to our own
    stdout (so `docker logs -f` still shows real-time per-segment progress
    during a long encode) while also buffering every line to return as one
    string -- that string becomes the per-file log written by
    write_run_log below."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    proc.wait()
    return proc.returncode, "".join(lines)


def format_elapsed(seconds):
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def build_log_content(log_level, ok, output_text, elapsed_seconds):
    """Returns the text to write for this run's log, or None to write
    nothing at all (see write_run_log -- None also removes any stale log
    left over from a previous run at that same path, so a log's mere
    presence/absence always reflects the CURRENT run's outcome under the
    CURRENT log_level, not a leftover from before either was changed).

    - "none": never anything.
    - "error": nothing on success; on failure, the same short excerpt as
      "info" below.
    - "info": the "=== Summary ===" block abridge.py already prints on
      success (its own before/after duration breakdown) plus how long the
      abridge.py subprocess itself actually ran for (elapsed_seconds --
      NOT anything abridge.py prints itself, it has no way to know its own
      wall-clock runtime). On failure, there's no summary block to extract
      (the run never got that far), so this is the last ~15 lines of
      output instead -- confirmed against every real failure mode hit
      during development (multiple audio tracks, VC-1 decode errors, etc.)
      that the actual reason is always in the last few lines, since
      abridge.py's own error handling always prints it as close to last as
      it gets.
    - "debug": the complete captured output (this is what a prior version
      of this project unconditionally wrote for every run), plus the same
      elapsed-time line appended.
    """
    if log_level == "none":
        return None
    took = f"Took {format_elapsed(elapsed_seconds)}."
    if log_level == "debug":
        return f"{output_text.rstrip()}\n\n{took}\n"
    if not ok:
        tail = "\n".join(output_text.strip().splitlines()[-15:])
        return f"FAILED. {took}\n\n{tail}\n"
    if log_level == "error":
        return None
    idx = output_text.find("=== Summary ===")
    summary = output_text[idx:].rstrip() if idx != -1 else output_text.strip()
    return f"{summary}\n\n{took}\n"


def write_run_log(out_dir, stem, log_level, ok, output_text, elapsed_seconds):
    """One log file per processed mkv, living right next to its output
    (<out_dir>/<stem>.log) -- see the module docstring for why it's named
    after the source stem rather than the resolved output filename."""
    log_path = out_dir / f"{stem}.log"
    content = build_log_content(log_level, ok, output_text, elapsed_seconds)
    if content is None:
        log_path.unlink(missing_ok=True)
        return
    log_path.write_text(content)


# All "file went quiet" events -- from the initial startup scan AND from
# live filesystem events alike -- funnel into this one shared queue,
# drained by a single dedicated worker thread (process_queue_worker)
# rather than each debounce timer calling check_and_process_file directly
# and racing every other ready timer for a lock. That race is what used
# to make processing order effectively arbitrary: many per-file
# threading.Timer threads with the same QUIET_SECONDS delay, all started
# within the same tight startup loop, wake up within milliseconds of each
# other -- CPython gives no ordering guarantee for who wins a contended
# Lock, so a whole season's episodes (all becoming "quiet" and ready at
# the same startup moment) previously processed in whatever arbitrary
# order the OS happened to schedule their threads in. Priority-queued by
# the file's own path string instead: a batch that all become ready
# around the same time now always drains in sorted order (e.g. S01E01
# before S01E02 before ... before S01E10), which is what actually matters
# for someone wanting to start watching a show from the beginning while
# later episodes are still being processed. The itertools.count()
# tiebreaker exists only so heapq never has to fall back to comparing two
# `pair` dicts (not orderable) -- str(path) is effectively always unique
# in practice, so it's defensive, not load-bearing.
_queue_tiebreak = itertools.count()
processing_queue = queue.PriorityQueue()


def process_queue_worker():
    """The single consumer draining processing_queue -- see above for why
    there's exactly one of these and not one per pair/timer. Runs for the
    lifetime of the container (daemon thread, started once in main())."""
    while True:
        _, _, path, pair, min_free_gb = processing_queue.get()
        try:
            check_and_process_file(path, pair, min_free_gb)
        except Exception as e:
            log(f"[!] Unexpected error processing {path}: {e}")
        finally:
            processing_queue.task_done()

# De-dupes the "no language available" log line per file, in-process only
# (not persisted -- see the module docstring) so a file with no
# Radarr/Sonarr match doesn't re-print the same warning on every debounce
# retrigger.
_warned_no_lang = set()
_warned_no_lang_lock = threading.Lock()


def check_and_process_file(f, pair, min_free_gb):
    """Called once a file's QUIET_SECONDS debounce timer fires with no
    further events in between -- i.e. this is the "actually look at it"
    step, not the "did something happen" step (that's DebouncedPairWatcher).
    Safe to call for a file that's already fully processed (the common
    steady-state case): the existing-output check below is a cheap glob,
    so re-confirming "nothing to do" here costs nothing like spawning
    abridge.py would. Only ever called from process_queue_worker, which is
    what actually provides the "one at a time, in sorted order" guarantee
    -- see its docstring and processing_queue above; nothing in this
    function itself is safe to call concurrently from multiple threads."""
    in_root, out_root = pair["input"], pair["output"]
    try:
        f.stat()
    except OSError:
        return  # gone by the time the timer fired -- e.g. deleted incomplete download, or a stale timer for an old path

    rel_dir = f.parent.relative_to(in_root)
    out_dir = out_root / rel_dir if str(rel_dir) != "." else out_root

    # Cheap, direct filesystem check for an existing output -- avoids
    # spawning a whole abridge.py subprocess (which itself runs
    # ffprobe just to rebuild the output filename) for a file that's
    # already fully done. A glob on the stem is enough --
    # build_abridged_output_path always names it "<stem>-ABRIDGED<X>X"
    # -- without duplicating abridge.py's own speed-suffix computation
    # (which needs an ffprobe call) here at all. This stays a LIVE
    # filesystem check rather than a cached "done" flag on purpose: if
    # the output ever disappears (has happened once already, for
    # reasons outside abridge.py itself), this naturally stops
    # matching and the file gets rebuilt next time its timer fires.
    if any(out_dir.glob(f"{f.stem}-ABRIDGED*")):
        return

    if min_free_gb > 0:
        free_gb = shutil_disk_free_gb(out_root)
        if free_gb is not None and free_gb < min_free_gb:
            log(f"[!] Only {free_gb:.1f}GB free at {out_root} (below "
                f"min_free_gb={min_free_gb}) -- skipping {f} for now, will retry on its next event.")
            return

    entry = None
    if pair["arr_url"]:
        entry = resolve_arr_entry(pair["arr_type"], pair["arr_url"], pair["arr_api_key"], f)

    if entry is not None and is_arr_failed(entry):
        return  # already known-bad -- remove the abridgerr-failed tag in Radarr/Sonarr to retry

    speed_override = lang_override = None
    if entry is not None:
        speed_override, lang_override = resolve_arr_overrides(entry)
    speed = speed_override or pair["default_speed"]
    lang = lang_override

    if lang is None:
        key = str(f)
        with _warned_no_lang_lock:
            already_warned = key in _warned_no_lang
            _warned_no_lang.add(key)
        if not already_warned:
            log(f"[!] No language available for {f} -- no Radarr/Sonarr match (import it through "
                f"Radarr's/Sonarr's dedicated toabridge root folder) or no original-language data, "
                f"and no abridgerr-lang-<code> tag either. Skipping.")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"Processing (quiet {QUIET_SECONDS:.0f}s, speed={speed}, lang={lang}, "
        f"encoder={ENCODER}, video_codec={VIDEO_CODEC}): {f}")
    # ENCODER/VIDEO_CODEC come before extra_args, not after: argparse
    # keeps the LAST occurrence of a repeated flag, so a pair's own
    # extra_args (e.g. ["--encoder", "cpu"] for one pair that needs to
    # skip hardware encode) can still override these container-wide
    # defaults on a per-pair basis.
    cmd = ([sys.executable, "/app/abridge.py", str(f), str(out_dir),
            "--speed", speed, "--lang", lang, "--embed-subs",
            "--encoder", ENCODER, "--video-codec", VIDEO_CODEC] + pair["extra_args"])
    start = time.monotonic()
    returncode, output_text = run_abridge(cmd)
    elapsed = time.monotonic() - start
    write_run_log(out_dir, f.stem, pair["log_level"], returncode == 0, output_text, elapsed)

    set_arr_failed_tag(pair["arr_type"], pair["arr_url"], pair["arr_api_key"], entry, returncode != 0)

    if returncode != 0:
        log(f"[!] abridge.py failed on {f} (exit {returncode}, took {format_elapsed(elapsed)}) -- tagged "
            f"{_ARR_FAILED_TAG} in {pair['arr_type']}, remove the tag there to retry.")
    else:
        log(f"Done: {f} (took {format_elapsed(elapsed)})")


class DebouncedPairWatcher(FileSystemEventHandler):
    """One instance per input/output pair. Restarts a QUIET_SECONDS timer
    for a specific file on every event that touches it, so
    check_and_process_file only ever runs after that file has gone truly
    quiet -- not on a fixed delay from the FIRST event, which could still
    land mid-copy for a large file. Also used directly for the initial
    startup scan (see main()): every pre-existing file gets its timer
    started the same way a live event would, rather than being trusted
    immediately -- a file could already be mid-write by another process
    at the moment this container starts."""

    def __init__(self, pair, min_free_gb):
        self.pair = pair
        self.min_free_gb = min_free_gb
        self._timers = {}
        self._timers_lock = threading.Lock()

    def schedule(self, path):
        key = str(path)
        timer = threading.Timer(QUIET_SECONDS, self._fire, args=(path,))
        timer.daemon = True
        with self._timers_lock:
            old = self._timers.get(key)
            if old is not None:
                old.cancel()
            self._timers[key] = timer
        timer.start()

    def _fire(self, path):
        with self._timers_lock:
            self._timers.pop(str(path), None)
        processing_queue.put((str(path), next(_queue_tiebreak), path, self.pair, self.min_free_gb))

    def on_any_event(self, event):
        path = getattr(event, "dest_path", None) or event.src_path
        if not event.is_directory and path.lower().endswith(".mkv"):
            self.schedule(Path(path))


def main():
    if ENCODER not in ENCODER_CHOICES:
        sys.exit(f"ENCODER={ENCODER!r} is not one of {sorted(ENCODER_CHOICES)} -- fix the environment "
                  f"variable (see docker-compose.example.yml).")
    if VIDEO_CODEC not in VIDEO_CODEC_CHOICES:
        sys.exit(f"VIDEO_CODEC={VIDEO_CODEC!r} is not one of {sorted(VIDEO_CODEC_CHOICES)} -- fix the "
                  f"environment variable (see docker-compose.example.yml).")

    config_path = Path(os.environ.get("ABRIDGE_CONFIG", "/config/config.json"))
    config = load_config(config_path)

    for pair in config["pairs"]:
        resolve_arr_env(pair)

    log(f"Watching {len(config['pairs'])} pair(s), reacting to filesystem events "
        f"(processing once a file is quiet for {QUIET_SECONDS:.0f}s):")
    for pair in config["pairs"]:
        arr_bit = f"{pair['arr_type']} @ {pair['arr_url']}" if pair["arr_url"] else "NO arr instance configured"
        log(f"  {pair['input']} -> {pair['output']}  [default_speed={pair['default_speed']}, "
            f"log_level={pair['log_level']}, {arr_bit}]")

    threading.Thread(target=process_queue_worker, daemon=True).start()

    observers = []
    for pair in config["pairs"]:
        in_root = pair["input"]
        if not in_root.is_dir():
            log(f"[!] {in_root} not mounted yet -- no watch set up for it "
                f"(needs a restart once it appears, there's no periodic retry).")
            continue
        pair["output"].mkdir(parents=True, exist_ok=True)

        watcher = DebouncedPairWatcher(pair, config["min_free_gb"])
        obs = Observer()
        obs.schedule(watcher, str(in_root), recursive=True)
        obs.start()
        observers.append(obs)

        # Starts every pre-existing file's quiet timer exactly as if a
        # live event had just fired for it -- see DebouncedPairWatcher's
        # docstring for why this doesn't just process them immediately.
        for f in find_mkv_files(in_root):
            watcher.schedule(f)

    try:
        threading.Event().wait()  # all real work happens in timer/observer threads from here on
    except KeyboardInterrupt:
        pass
    finally:
        for obs in observers:
            obs.stop()
        for obs in observers:
            obs.join()


if __name__ == "__main__":
    main()
