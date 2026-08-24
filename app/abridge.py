#!/usr/bin/env python3
"""
abridge.py

Speeds up the no-dialog parts of a video (default 2x) while keeping dialog
at normal speed, and can resync subtitles to the new timeline.
"""

import argparse
import bisect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path


_active_procs = set()
_active_procs_lock = threading.Lock()
_interrupted = threading.Event()


def _kill_active_procs():
    with _active_procs_lock:
        procs = list(_active_procs)
    for p in procs:
        try:
            p.terminate()
        except OSError:
            pass
    for p in procs:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except OSError:
                pass
        except OSError:
            pass


def run(cmd, capture=False):
    kwargs = dict(stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if capture:
        kwargs["stdout"] = subprocess.PIPE
    proc = subprocess.Popen(cmd, **kwargs)
    with _active_procs_lock:
        _active_procs.add(proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        with _active_procs_lock:
            _active_procs.discard(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def get_duration(path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ]
    r = run(cmd, capture=True)
    return float(r.stdout.strip())


SRT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)


def _ts_to_sec(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_subtitles(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        content = f.read()

    blocks = []
    lines = content.splitlines()
    cur_text = []
    cur_times = None
    for line in lines:
        m = SRT_TIME_RE.search(line)
        if m:
            if cur_times is not None:
                blocks.append((cur_times[0], cur_times[1], cur_text))
            start = _ts_to_sec(*m.groups()[0:4])
            end = _ts_to_sec(*m.groups()[4:8])
            cur_times = (start, end)
            cur_text = []
        elif line.strip() == "":
            if cur_times is not None:
                blocks.append((cur_times[0], cur_times[1], cur_text))
                cur_times = None
                cur_text = []
        else:
            if cur_times is not None:
                if line.strip().isdigit() or line.strip().upper() == "WEBVTT":
                    continue
                cur_text.append(line)
    if cur_times is not None:
        blocks.append((cur_times[0], cur_times[1], cur_text))
    blocks.sort(key=lambda b: b[0])
    return blocks


_BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# Standard SDH convention for singing/music cues -- U+266A-U+266C, U+2669
# (note characters). A line is treated as pure music/lyrics the moment ANY
# note character appears in it, regardless of whether it's paired with a
# matching closing note -- confirmed against two different real files that
# real SDH tracks don't reliably pair notes within a single line, or even
# within a single cue: The Death of Robin Hood (2026) split an opening/
# closing pair across a cue's own two lines (line 1 opens, line 2 of the
# SAME cue closes), while From Dusk Till Dawn (1996) never closes the note
# AT ALL within any single cue -- every individual line of the song is its
# own separate subtitle entry prefixed with just one note character, with
# the "closing" note (if any) potentially many cues later. Matching pairs
# was tried twice now (once within a single line, then -- when that missed
# Robin Hood -- within a whole joined cue) and both proved unreliable in
# practice; a single unpaired note is sufficient and handles every real
# pattern seen so far, without needing any cue-level joining logic at all.
# Deliberately scoped to just this well-established musical-note
# convention rather than a broader "any special character" heuristic,
# which would risk false-positives on real dialogue using other
# punctuation for emphasis.
_MUSIC_RE = re.compile(r"[\u266A\u266B\u266C\u2669]")


def is_sdh_line(line):
    stripped = line.strip()
    if not stripped:
        return True
    if _MUSIC_RE.search(stripped):
        return True
    without_brackets = _BRACKET_RE.sub("", stripped).strip()
    return without_brackets == ""


_HI_TITLE_RE = re.compile(r"\b(SDH|CC|HI)\b|hearing[\s-]?impaired", re.IGNORECASE)
_FORCED_TITLE_RE = re.compile(r"\bforced\b", re.IGNORECASE)


def is_forced_track_title(title):
    """Whether a subtitle track's own title marks it as forced -- see
    is_forced_track (list_subtitle_tracks) for why this is checked
    alongside, not instead of, ffprobe's disposition.forced flag."""
    return bool(title) and bool(_FORCED_TITLE_RE.search(title))


def is_hi_track_title(title):
    """Whether a subtitle TRACK's own title marks it as a hearing-impaired/
    SDH/closed-caption variant -- title text, not ffprobe's disposition.
    hearing_impaired flag. Confirmed directly against a real Blu-ray remux
    (Evil Dead 2013) that a track titled exactly "SDH" still reports
    disposition.hearing_impaired=0 -- the flag can't be trusted, title text
    is the only signal that's actually populated in practice for this
    (the same kind of real-world metadata unreliability this project has
    hit before with other disposition-adjacent fields)."""
    return bool(title) and bool(_HI_TITLE_RE.search(title))


def subtitle_priority_tier(t):
    """Lower is preferred, for choosing among several subtitle tracks that
    all match the requested --lang. PGS/bitmap non-HI < PGS/bitmap HI <
    text non-HI < text HI -- a deliberate preference for the disc's own
    official PGS track (the studio's real theatrical timing) over a
    possibly-imprecise/fan-sourced text track when both exist, with the
    plain (non-SDH) version of whichever kind preferred over its SDH
    counterpart either way. Ties within a tier are broken by stream order
    (lowest abs_index first -- see choose_subtitle_track)."""
    is_hi = is_hi_track_title(t["title"])
    if not t["is_text"]:
        return 2 if is_hi else 1
    return 4 if is_hi else 3


def filter_sdh(sub_entries):
    kept = []
    dropped = 0
    for (s, e, text_lines) in sub_entries:
        meaningful = [l for l in text_lines if not is_sdh_line(l)]
        if meaningful:
            kept.append((s, e, meaningful))
        else:
            dropped += 1
    return kept, dropped


def resolve_speed(input_path, speed_mode):
    """Computes the actual speed multiplier for a given input file and
    --speed mode, without running the full pipeline -- used to build
    the speed-suffixed output filename before processing starts. Mirrors
    the exact derivation used inside process_one/process_one_dialog_mode
    (base_speed = target_fps/source_fps; high=2x that, low=that,
    dialog=half that), so the filename always matches what the file
    actually gets encoded at."""
    source_fps_frac = get_source_video_fps_fraction(input_path)
    target_frac = Fraction(60000, 1001)
    base_speed = float(target_frac / source_fps_frac)
    if speed_mode == "high":
        return base_speed * 2
    elif speed_mode == "dialog":
        return base_speed / 2
    return base_speed


def format_speed_for_filename(speed):
    """Formats a speed multiplier for an output filename -- at most 1
    decimal place, with a trailing '.0' trimmed for whole numbers (e.g.
    5.0 -> '5', 2.5 stays '2.5'). Uses standard round-half-up rather than
    Python's default round() (round-half-to-even/"banker's rounding"),
    which silently loses meaningful precision right at the .x5 boundary
    in a way that's genuinely misleading in a filename -- confirmed
    directly: round(1.25, 1) gives 1.2 by default, not 1.3, for exactly
    this file's own dialog-mode speed."""
    import decimal
    rounded = float(decimal.Decimal(str(speed)).quantize(decimal.Decimal("0.1"),
                                                           rounding=decimal.ROUND_HALF_UP))
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.1f}"


def build_abridged_output_path(input_path, output_dir, speed_mode):
    """Builds the full output path for a given input file inside
    output_dir: '<stem>-ABRIDGED<X>X.mp4', X being the actual
    resolved speed (see format_speed_for_filename)."""
    speed = resolve_speed(input_path, speed_mode)
    speed_str = format_speed_for_filename(speed)
    stem = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(output_dir, f"{stem}-ABRIDGED{speed_str}X.mp4")


def fmt_hms(t):
    """Formats seconds as a filename-safe hh.mm.ss.ss-style timestamp for
    --dump-segments filenames, e.g. 2572.48 -> '00h42m52.48s'. Uses letter
    separators (h/m/s) rather than colons or extra dots -- colons aren't
    valid in Windows filenames, and some sync/transfer tools mangle dots
    in filenames (seen firsthand: periods silently became underscores),
    which would make a dotted hh.mm.ss unreliable to parse back later."""
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}h{m:02d}m{s:05.2f}s"


def sec_to_srt_ts(t):
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for i, (start, end, text_lines) in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{sec_to_srt_ts(start)} --> {sec_to_srt_ts(end)}\n")
            f.write("\n".join(text_lines) + "\n\n")


def build_segment_srt(sub_entries, seg_start, seg_end, out_path, speed_factor=1.0):
    entries = []
    for (s, e, text_lines) in sub_entries:
        if e <= seg_start or s >= seg_end:
            continue
        new_s = max(0.0, s - seg_start) / speed_factor
        new_e = (min(seg_end, e) - seg_start) / speed_factor
        if new_e <= new_s + 0.01:
            continue
        entries.append((new_s, new_e, text_lines))
    if not entries:
        return False
    write_srt(out_path, entries)
    return True


def ffmpeg_escape_filter_path(path):
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}


def list_subtitle_tracks(path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "s",
        "-show_entries", "stream=index,codec_name,disposition:stream_tags=language,title",
        "-of", "json", path
    ]
    r = run(cmd, capture=True)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    tracks = []
    for s in data.get("streams", []):
        tags = s.get("tags", {}) or {}
        disposition = s.get("disposition", {}) or {}
        tracks.append({
            "abs_index": s["index"],
            "codec_name": s.get("codec_name", "?"),
            "lang": tags.get("language", "und"),
            "title": tags.get("title", ""),
            "is_text": s.get("codec_name", "").lower() in TEXT_SUB_CODECS,
            # ffprobe's own disposition.forced flag -- kept as a plain bool
            # here; is_forced_track() below is what callers should actually
            # use, since the flag alone isn't reliable enough on its own
            # (see there).
            "forced": bool(disposition.get("forced", 0)),
        })
    return tracks


def is_forced_track(t):
    """Whether a subtitle track should be treated as forced (foreign-
    -language-snippets-only, not full dialogue coverage) -- true if EITHER
    ffprobe's disposition.forced flag is set OR the track's own title says
    "forced" as a whole word. The flag alone isn't sufficient: confirmed
    directly on a real Blu-ray remux (Evil Dead 2013) where a track titled
    exactly "forced only" still reports disposition.forced=0, which meant
    choose_subtitle_track's --lang auto-selection picked it as if it were
    a full-coverage track. Checking title text was deliberately avoided
    for this in the past (see the removed comment this replaced) on the
    theory that the flag was the more reliable signal -- that theory
    turned out to be wrong often enough in practice to need this
    fallback, so now either signal is trusted."""
    return bool(t.get("forced")) or is_forced_track_title(t.get("title"))


def describe_track(i, t):
    bits = f"lang={t['lang']}"
    if t["title"]:
        bits += f' title="{t["title"]}"'
    bits += f" codec={t['codec_name']}"
    if is_forced_track(t):
        bits += "  [FORCED]"
    if not t["is_text"]:
        bits += "  (bitmap/image subtitle -- can't auto-extract to text)"
    return f"  [{i}] {bits}"


def extract_embedded_subtitle(path, abs_stream_index, out_srt_path):
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-i", path,
        "-map", f"0:{abs_stream_index}", "-c:s", "srt",
        out_srt_path
    ]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"Failed to extract subtitle track {abs_stream_index}:\n{r.stderr[-2000:]}")


def lang_alpha3_to_alpha2(lang_alpha3):
    """Converts an ISO 639-2 (3-letter, what ffprobe reports) language code
    to the IETF 2-letter code pgsrip needs baked into its input filename
    (confirmed directly against the installed library: it discovers the
    language from a `name.XX.sup` filename pattern via babelfish, and
    unlike a bare 3-letter code, this reliably matched in testing). Uses
    babelfish (already a pgsrip dependency, so no extra install) rather
    than a hand-rolled table, for accurate coverage. Returns None if the
    code isn't a recognized language (e.g. 'und'/undetermined)."""
    try:
        from babelfish import Language
        return Language(lang_alpha3).alpha2
    except Exception:
        return None


def ocr_pgs_subtitle(input_path, abs_stream_index, lang_alpha3, tmpdir):
    """OCRs an embedded bitmap (PGS/Blu-ray, or other image-based) subtitle
    track into an SRT, via pgsrip + Tesseract. Confirmed working end-to-end
    against the actual installed pgsrip library (not assumed from docs):
    extracts the specific stream to a standalone .sup file, named with the
    2-letter language code pgsrip's filename-based language detection
    needs, then invokes pgsrip on it. Returns the path to the resulting
    .srt, or None if OCR isn't available/usable (missing tools, unknown
    language, or the rip produced no lines) -- callers should treat None
    as "fall through to erroring the normal way", not crash the run.
    Requires `pip install pgsrip` and `apt install tesseract-ocr
    tesseract-ocr-<lang>` (e.g. tesseract-ocr-eng) -- prints clear
    installation instructions if either is missing rather than failing
    silently.

    Invokes via `python3 -m pgsrip` rather than a bare `pgsrip` command --
    confirmed directly this matters: `pip install --break-system-packages`
    (needed on externally-managed Ubuntu Pythons) falls back to a user
    install, which puts the pgsrip CLI script under ~/.local/bin. That
    directory is very commonly NOT on $PATH, so shutil.which("pgsrip")
    (and a bare "pgsrip" invocation) can both report "not found" even
    though the package itself is genuinely installed and importable --
    `python -m pgsrip` only needs the package on Python's own import
    path, not a wrapper script on $PATH, and sidesteps this entirely."""
    check = subprocess.run([sys.executable, "-m", "pgsrip", "--help"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if check.returncode != 0:
        print(f"[!] pgsrip not usable via '{sys.executable} -m pgsrip' -- can't OCR the bitmap subtitle "
              f"track. Actual error:\n{(check.stderr or '(no stderr)').strip()[-1000:]}\n"
              f"    If it says 'No module named pgsrip', install with:\n"
              f"      pip install pgsrip --break-system-packages\n"
              f"      sudo apt install tesseract-ocr tesseract-ocr-<lang>  (e.g. tesseract-ocr-eng)\n"
              f"    If pgsrip IS installed, this may mean it's installed for a DIFFERENT python3 than "
              f"the one running this script ({sys.executable}) -- check `which python3` vs where pip "
              f"installed it.")
        return None
    alpha2 = lang_alpha3_to_alpha2(lang_alpha3)
    if not alpha2:
        print(f"[!] Can't OCR: unrecognized language code {lang_alpha3!r} for this subtitle track.")
        return None

    sup_path = os.path.join(tmpdir, f"ocr_subs.{alpha2}.sup")
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", input_path,
           "-map", f"0:{abs_stream_index}", "-c:s", "copy", sup_path]
    r = run(cmd)
    if r.returncode != 0 or not os.path.exists(sup_path):
        print(f"[!] Failed to extract the bitmap subtitle track for OCR:\n{(r.stderr or '')[-1000:]}")
        return None

    print(f"OCRing bitmap subtitle track (stream #{abs_stream_index}, lang={lang_alpha3}) via pgsrip/Tesseract "
          f"-- this can take a while for a full movie's worth of lines...")
    r = run([sys.executable, "-m", "pgsrip", sup_path, "-l", alpha2, "-f"], capture=True)
    srt_path = os.path.join(tmpdir, f"ocr_subs.{alpha2}.srt")
    if not os.path.exists(srt_path):
        print(f"[!] pgsrip did not produce an SRT file -- OCR failed. Output:\n{(r.stdout or '')[-1500:]}\n"
              f"      Check that tesseract-ocr-{alpha2} (or the matching language pack) is installed.")
        return None
    print(f"OCR complete: {srt_path}")
    return srt_path


def best_non_forced_track(tracks):
    """Picks the single best track from an already language-filtered list
    (forced tracks excluded), via subtitle_priority_tier + stream order --
    the same rule choose_subtitle_track uses for the requested --lang,
    factored out so gather_cross_lang_dialog_ranges below can apply the
    identical selection to every OTHER language in the file too. Returns
    None if nothing non-forced is left."""
    candidates = [t for t in tracks if not is_forced_track(t)]
    if not candidates:
        return None
    return min(candidates, key=lambda t: (subtitle_priority_tier(t), t["abs_index"]))


def extract_and_filter_subtitle(input_path, track, tmpdir):
    """Extracts a single track (direct text extraction, or OCR if it's a
    bitmap track -- matches track["needs_ocr"]) and SDH-filters it via the
    same filter_sdh every other subtitle path in this file uses. Returns
    the filtered (start, end, text_lines) entries, or None if extraction/
    OCR produced nothing usable. Shared by gather_cross_lang_dialog_ranges
    below -- the target language's own extraction in process_one is left
    as its own separate, already-tested code path rather than rerouted
    through this, since it also has to track sub_lang/print messages this
    generic helper doesn't need to care about."""
    if track.get("needs_ocr"):
        extracted_path = ocr_pgs_subtitle(input_path, track["abs_index"], track["lang"], tmpdir)
    else:
        extracted_path = os.path.join(tmpdir, f"extract_{track['abs_index']}.srt")
        extract_embedded_subtitle(input_path, track["abs_index"], extracted_path)
    if not extracted_path or not os.path.exists(extracted_path):
        return None
    entries = parse_subtitles(extracted_path)
    filtered, _ = filter_sdh(entries)
    return filtered if filtered else None


# English/Spanish/French/German/Italian/Portuguese -- matches the
# tesseract-ocr-<lang> packs actually installed in the Docker image (see
# Dockerfile), so an other-language track picked here is guaranteed
# OCR-able if it turns out to be a bitmap/PGS track rather than plain
# text.
CROSS_LANG_OCR_LANGS = {"eng", "spa", "fra", "deu", "ita", "por"}


def gather_cross_lang_dialog_ranges(input_path, tracks, target_lang, tmpdir):
    """Picks a single OTHER-language subtitle track -- not every other
    language present in the file -- and returns ITS (start, end) cue
    ranges, used purely as an extra dialog-detection signal alongside the
    target language's own ranges, never for the actual embedded subtitle
    text (that still comes only from the target language's own track,
    completely unchanged).

    This recovers on-screen text (title cards, signage, etc.) that the
    target language's own track correctly DOESN'T translate -- nothing to
    translate for a reader who can already read it on screen -- but a
    non-target-language track has to. Confirmed directly against a real
    title (56 Days S01E01): comparing English against Spanish/French cue
    coverage found 127.6s (10.1%) of real additional coverage, and
    spot-checking it showed exactly this pattern -- "TODAY"/"DAY 1"-style
    on-screen date cards and "BOSTON POLICE / DO NOT CROSS"-style scene
    signage, translated in Spanish/French with no overlapping English cue
    at all, not a captioning oversight.

    Candidates are limited to CROSS_LANG_OCR_LANGS (besides target_lang)
    rather than every language present in the file, and only ONE track is
    used rather than the union of all matching ones -- bounds this
    enhancement's extraction/OCR time to at most one extra track instead
    of scaling with however many other-language tracks a release happens
    to have. Tried in the order their tracks appear in the file; the
    first candidate that yields usable cue ranges wins. Best-effort: a
    candidate that fails to extract/OCR is skipped (logged, not fatal) in
    favor of the next one, rather than aborting the whole run over an
    enhancement that's optional by nature -- the target language's own
    dialog detection still works completely fine without it."""
    candidates = []
    seen = set()
    for t in tracks:
        lang = t["lang"].lower()
        if lang in CROSS_LANG_OCR_LANGS and lang != target_lang.lower() and lang not in seen:
            seen.add(lang)
            candidates.append(t["lang"])

    for lang in candidates:
        best = best_non_forced_track([t for t in tracks if t["lang"] == lang])
        if best is None:
            continue
        track = dict(best)
        track["needs_ocr"] = not track["is_text"]
        try:
            filtered = extract_and_filter_subtitle(input_path, track, tmpdir)
        except Exception as e:
            print(f"  [!] Couldn't extract {lang} track for cross-language dialog detection, "
                  f"trying next candidate: {e}")
            continue
        if filtered:
            return [(s, e) for s, e, _ in filtered]
    return []


def choose_subtitle_track(tracks, requested_index, lang=None):
    """Returns the chosen track dict, with an added 'needs_ocr' key (True if
    it's a bitmap/image track selected for OCR rather than direct text
    extraction). When --lang matches more than one track, picks among them
    by subtitle_priority_tier (PGS non-HI > PGS HI > text non-HI > text HI,
    ties broken by stream order) rather than erroring out -- an explicit
    --lang is treated as consent to OCR a bitmap track if that's what the
    priority picks, the same as it always has been. Forced tracks are
    excluded from every tier: they only cover foreign-language snippets
    (e.g. alien dialogue in an otherwise-English film), not full dialogue
    coverage, regardless of what the priority order would otherwise prefer
    -- detected via ffprobe's own disposition.forced flag, not by guessing
    from the title text. When no --lang is given (or it matches nothing
    but forced tracks), the person is asked interactively. Returns None if
    nothing usable was found."""
    text_tracks = [t for t in tracks if t["is_text"]]
    bitmap_tracks = [t for t in tracks if not t["is_text"]]

    if requested_index is not None:
        if requested_index < 0 or requested_index >= len(tracks):
            sys.exit(f"--sub-track {requested_index} is out of range (input has {len(tracks)} subtitle track(s)).")
        chosen = dict(tracks[requested_index])
        if not chosen["is_text"]:
            print(f"Track [{requested_index}] is bitmap/image-based (codec={chosen['codec_name']}) -- "
                  f"will OCR it (explicitly requested via --sub-track).")
            chosen["needs_ocr"] = True
        return chosen

    if lang:
        lang_matches = [t for t in tracks if t["lang"].lower() == lang.lower()]
        best = best_non_forced_track(lang_matches)
        if best is not None:
            non_forced_count = sum(1 for t in lang_matches if not is_forced_track(t))
            kind = "text" if best["is_text"] else "PGS/bitmap"
            hi_tag = " HI/SDH" if is_hi_track_title(best["title"]) else ""
            title_bit = f', title="{best["title"]}"' if best["title"] else ""
            print(f"Auto-selected {kind}{hi_tag} subtitle track matching --lang {lang} "
                  f"(lang={best['lang']}, codec={best['codec_name']}{title_bit}) -- "
                  f"{non_forced_count} track(s) matched, chose by PGS-non-HI > PGS-HI > "
                  f"text-non-HI > text-HI priority.")
            chosen = dict(best)
            if not chosen["is_text"]:
                chosen["needs_ocr"] = True
            return chosen
        elif lang_matches:
            print(f"[!] --lang {lang} only matched {len(lang_matches)} FORCED subtitle track(s) -- "
                  f"these only cover foreign-language snippets, not full dialogue, so none were "
                  f"auto-selected. Pick one explicitly via --sub-track if that's really what you want.")
        elif tracks:
            print(f"[!] --lang {lang} matched no subtitle track of any kind (available: "
                  f"{', '.join(sorted(set(t['lang'] for t in tracks)))}). Falling back to normal selection.")

    if len(tracks) == 1:
        t = tracks[0]
        if t["is_text"]:
            return t
        print(f"Input has one subtitle track and it's bitmap/image-based (codec={t['codec_name']}).")
        return _offer_ocr(t)

    print(f"Input has {len(tracks)} subtitle tracks:")
    for i, t in enumerate(tracks):
        print(describe_track(i, t))

    if not text_tracks and not bitmap_tracks:
        print("No subtitle tracks usable.")
        return None

    if sys.stdin.isatty():
        while True:
            choice = input(f"Pick a track to use [0-{len(tracks)-1}] (blank to skip): ").strip()
            if choice == "":
                return None
            if choice.isdigit() and 0 <= int(choice) < len(tracks):
                t = tracks[int(choice)]
                if t["is_text"]:
                    return t
                return _offer_ocr(t)
            print("Invalid choice.")
    else:
        sys.exit("Multiple subtitle tracks found and no terminal to prompt in. "
                  "Re-run with --sub-track N (see the list above) -- add it to a bitmap track to OCR it "
                  "automatically -- or --lang, or --list-subs to just see the options.")


def _offer_ocr(bitmap_track):
    """Interactively asks whether to OCR a bitmap subtitle track. Only
    called when no --lang/--sub-track already implied consent."""
    if not sys.stdin.isatty():
        print(f"Track is bitmap/image-based (codec={bitmap_track['codec_name']}) and no terminal to prompt "
              f"in for OCR consent -- skipping. Re-run with --lang or --sub-track to OCR it automatically.")
        return None
    choice = input(f"This track is bitmap/image-based (codec={bitmap_track['codec_name']}) and can't be "
                    f"auto-extracted as text directly. OCR it with pgsrip/Tesseract instead? [y/N]: ").strip().lower()
    if choice != "y":
        return None
    chosen = dict(bitmap_track)
    chosen["needs_ocr"] = True
    return chosen


def list_audio_tracks(path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index,codec_name,channels,channel_layout:stream_tags=language,title",
        "-of", "json", path
    ]
    r = run(cmd, capture=True)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    tracks = []
    for s in data.get("streams", []):
        tags = s.get("tags", {}) or {}
        tracks.append({
            "abs_index": s["index"],
            "codec_name": s.get("codec_name", "?"),
            "channels": s.get("channels", "?"),
            "channel_layout": s.get("channel_layout", ""),
            "lang": tags.get("language", "und"),
            "title": tags.get("title", ""),
        })
    return tracks


_COMMENTARY_TITLE_RE = re.compile(r"\bcommentary\b", re.IGNORECASE)


def is_commentary_track(t):
    """Whether an audio track is a commentary track (director's/cast
    commentary, not the main program audio) -- title text only, ffprobe has
    no disposition flag for this. Confirmed against a real file: commentary
    tracks are reliably labeled with "commentary" in the title (e.g.
    "Commentary with <names>"), a word that wouldn't plausibly appear in an
    actual dialogue/program audio track's title."""
    return bool(t.get("title")) and bool(_COMMENTARY_TITLE_RE.search(t["title"]))


def describe_audio_track(i, t):
    bits = f"lang={t['lang']}"
    if t["title"]:
        bits += f' title="{t["title"]}"'
    bits += f" codec={t['codec_name']} channels={t['channels']}"
    if t.get("channel_layout"):
        bits += f" layout={t['channel_layout']}"
    if is_commentary_track(t):
        bits += "  [COMMENTARY]"
    return f"  [{i}] {bits}"


def choose_audio_track(tracks, requested_index, lang=None):
    if requested_index is not None:
        if requested_index < 0 or requested_index >= len(tracks):
            sys.exit(f"--audio-track {requested_index} is out of range (input has {len(tracks)} audio track(s)).")
        return tracks[requested_index]

    if lang:
        matches = [t for t in tracks if t["lang"].lower() == lang.lower()]
        if len(matches) == 1:
            print(f"Auto-selected audio track matching --lang {lang} (lang={matches[0]['lang']}, "
                  f"codec={matches[0]['codec_name']}, channels={matches[0]['channels']}).")
            return matches[0]
        elif len(matches) > 1:
            # Commentary tracks are excluded first when at least one
            # non-commentary match remains (a commentary track sharing the
            # main program's language is common and unambiguous to
            # exclude). Among whatever's left, ties are broken by picking
            # the first (lowest stream index) rather than erroring --
            # matches the same "first found wins a tie" policy already
            # used for subtitle track selection.
            non_commentary = [t for t in matches if not is_commentary_track(t)]
            candidates = non_commentary or matches
            chosen = min(candidates, key=lambda t: t["abs_index"])
            notes = []
            excluded = len(matches) - len(candidates)
            if excluded:
                notes.append(f"excluded {excluded} commentary track(s)")
            if len(candidates) > 1:
                notes.append(f"picked the first of {len(candidates)} tied match(es)")
            note = f" -- {', '.join(notes)}" if notes else ""
            print(f"Auto-selected audio track matching --lang {lang} (lang={chosen['lang']}, "
                  f"codec={chosen['codec_name']}, channels={chosen['channels']}){note}.")
            return chosen
        elif tracks:
            print(f"[!] --lang {lang} matched no audio track (available: "
                  f"{', '.join(sorted(set(t['lang'] for t in tracks)))}). Falling back to normal selection.")

    if len(tracks) <= 1:
        return tracks[0] if tracks else None

    print(f"Input has {len(tracks)} audio tracks:")
    for i, t in enumerate(tracks):
        print(describe_audio_track(i, t))

    if sys.stdin.isatty():
        while True:
            choice = input(f"Pick an audio track to use [0-{len(tracks)-1}] (blank for track 0): ").strip()
            if choice == "":
                return tracks[0]
            if choice.isdigit() and 0 <= int(choice) < len(tracks):
                return tracks[int(choice)]
            print("Invalid choice.")
    else:
        sys.exit("Multiple audio tracks found and no terminal to prompt in. "
                  "Re-run with --audio-track N (see the list above), or --list-audio to just see the options.")


def get_source_video_fps(path):
    """Native frame rate of the source's video stream, used to size the
    boundary-trim epsilon in process_one (see there for the full rationale).
    Prefers r_frame_rate over avg_frame_rate: r_frame_rate reports the
    stream's nominal/constant cadence (e.g. the true 24000/1001 for a
    23.976fps film source), which is what actually determines native frame
    spacing at a cut boundary. avg_frame_rate can differ if the source has
    any duplicate/dropped frames baked in, which would make it a worse
    estimate of the underlying native frame period specifically. Falls back
    to a conservative default if ffprobe can't determine either."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate",
        "-of", "json", path
    ]
    r = run(cmd, capture=True)
    try:
        data = json.loads(r.stdout)
        streams = data.get("streams", [])
        if streams:
            for key in ("r_frame_rate", "avg_frame_rate"):
                val = streams[0].get(key, "0/0")
                if "/" in val:
                    num, den = val.split("/")
                    num, den = float(num), float(den)
                    if den > 0 and num > 0:
                        return num / den
    except Exception:
        pass
    return 30.0


def get_source_video_codec(path):
    """Source video stream's codec_name (e.g. "h264", "vc1"), used to steer
    around known-bad hardware DECODE paths -- see
    _HW_DECODE_UNRELIABLE_CODECS. Returns "" if ffprobe can't determine it,
    which just means no override gets applied (hw_decode stays whatever it
    would otherwise have been)."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ]
    r = run(cmd, capture=True)
    return r.stdout.strip().lower()


# Source codecs whose Intel QSV/VAAPI hardware DECODER is unreliable enough
# on real hardware to not be worth attempting -- confirmed directly on a
# real VC-1 remux: vc1_qsv failed decode on every single segment
# ("video_get_buffer: image parameters invalid" -> cascading "Decode error
# rate 1 exceeds maximum 0.666667" -> encoder never receives a frame),
# forcing every segment through the full qsv-fails -> vaapi-fails -> cpu
# fallback chain, every time, for the whole file. Confirmed separately that
# hardware ENCODE is NOT the problem: software decode + QSV encode of the
# same content works cleanly (3x+ realtime, zero errors) -- so this only
# forces software DECODE for these codecs, hardware encode is untouched.
# VC-1 is a known weak spot for Intel QSV/VAAPI decode support in general
# (much less commonly exercised than H.264/HEVC), not specific to this one
# file -- add more codecs here only after confirming the same failure
# pattern directly, not preemptively.
_HW_DECODE_UNRELIABLE_CODECS = {"vc1"}


def get_source_video_fps_fraction(path):
    """Same probe as get_source_video_fps, but returns an EXACT
    fractions.Fraction (e.g. Fraction(24000, 1001)) built directly from
    ffprobe's own integer numerator/denominator, instead of a float division
    that immediately discards exactness. Used wherever the actual
    duplication/decimation ratio between source and target frame rates
    needs to be verified as a clean small-integer relationship (see
    resolve_frame_rate_strategy) -- floats are precise enough for
    frame-grid snapping, but proving "this ratio is EXACTLY 1/2, not just
    very close to it" requires rational, not floating-point, arithmetic."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate",
        "-of", "json", path
    ]
    r = run(cmd, capture=True)
    try:
        data = json.loads(r.stdout)
        streams = data.get("streams", [])
        if streams:
            for key in ("r_frame_rate", "avg_frame_rate"):
                val = streams[0].get(key, "0/0")
                if "/" in val:
                    num, den = val.split("/")
                    num, den = int(num), int(den)
                    if den > 0 and num > 0:
                        return Fraction(num, den)
    except Exception:
        pass
    return Fraction(30, 1)


def build_segments_from_ranges(duration, keep_normal_ranges):
    segments = []
    cursor = 0.0
    for s, e in keep_normal_ranges:
        if s > cursor:
            segments.append((cursor, s, True))
        if e > s:
            segments.append((s, e, False))
        cursor = max(cursor, e)
    if cursor < duration:
        segments.append((cursor, duration, True))
    segments = [(a, b, sp) for (a, b, sp) in segments if b - a > 0.02]
    return segments


def snap_segments_to_frame_grid(segments, fps):
    """Snaps every segment boundary to the nearest native source frame index
    at fps, replacing arbitrary subtitle/silence-derived floating-point
    second boundaries with an exact integer frame grid. This is what
    actually eliminates boundary ambiguity between adjacent segments: since
    segment i's end and segment i+1's start are the SAME float value before
    this call (shared by construction in build_segments_from_ranges),
    round()ing that shared value once gives both segments the IDENTICAL
    frame index -- not two independently-rounded floats that two separate
    ffmpeg invocations could resolve to two different actual frames.

    Snapped to the nearest EVEN frame index (the start of a source-frame
    PAIR), not just the nearest frame. The high-speed (2:1 decimate)
    strategy consumes source frames in pairs -- framestep=2 always counts
    locally from n=0 within its own segment. If a boundary landed on an odd
    global frame index, a decimate segment's local pairing would be phase-
    shifted relative to its neighbors', and the shared boundary itself
    could straddle a pair rather than sitting cleanly between two of them.
    Snapping every boundary to a pair start removes that ambiguity: every
    segment spans a whole number of pairs, decimate or not, so there's
    never a leftover half-pair at an edge for any strategy to have to
    guess about.

    Every downstream boundary (auto-speed scoring, printed stats,
    TimeMapper, and the actual per-segment cut in process_one) works off
    these snapped values from here on. Drops any segment that rounds to
    zero length."""
    out = []
    for (s, e, is_gap) in segments:
        start_frame = 2 * round(round(s * fps) / 2)
        end_frame = 2 * round(round(e * fps) / 2)
        if end_frame <= start_frame:
            continue
        out.append((start_frame / fps, end_frame / fps, is_gap))
    return out


def enforce_min_nondialog_duration(segments, min_nondialog_duration):
    if not segments or min_nondialog_duration <= 0:
        return segments
    flagged = [(s, e, is_gap and (e - s) >= min_nondialog_duration) for (s, e, is_gap) in segments]
    merged = [list(flagged[0])]
    for s, e, is_gap in flagged[1:]:
        last = merged[-1]
        if is_gap == last[2]:
            last[1] = e
        else:
            merged.append([s, e, is_gap])
    return [(s, e, is_gap) for s, e, is_gap in merged]


def merge_and_pad(ranges, pad, merge_gap, duration):
    if not ranges:
        return []
    padded = [(max(0.0, s - pad), min(duration, e + pad)) for s, e in sorted(ranges)]
    merged = [padded[0]]
    for s, e in padded[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e + merge_gap:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))
    return merged


_warned = set()


def warn_once(key, message):
    if key not in _warned:
        print(message)
        _warned.add(key)


def encoder_listed(vendor, video_codec):
    """Whether f"{video_codec}_{vendor}" (e.g. "hevc_nvenc") is compiled
    into this ffmpeg build -- a BUILD-capability check only, not a
    real-hardware check (this image ships qsv/vaapi/nvenc support
    together regardless of which GPU vendor is actually passed through at
    runtime -- see the Dockerfile). Real hardware absence is instead
    discovered lazily, per-segment, via the runtime failure/fallback
    chain in cut_segment -- this just decides what to try first."""
    r = run(["ffmpeg", "-hide_banner", "-encoders"], capture=True)
    return f"{video_codec}_{vendor}" in r.stdout


_NTSC_EXACT_FRACTIONS = {
    23.976: "24000/1001",
    29.97: "30000/1001",
    47.952: "48000/1001",
    59.94: "60000/1001",
    119.88: "120000/1001",
}


def fps_arg_str(fps):
    for val, frac in _NTSC_EXACT_FRACTIONS.items():
        if abs(fps - val) < 0.005:
            return frac
    return str(fps)


def target_fps_fraction(fps):
    """Exact Fraction for a target fps value, reusing the NTSC lookup table
    above (so 59.94 -> exactly Fraction(60000, 1001), not a lossy decimal
    approximation) with a sensible fallback for non-NTSC targets."""
    for val, frac_str in _NTSC_EXACT_FRACTIONS.items():
        if abs(fps - val) < 0.005:
            num, den = frac_str.split("/")
            return Fraction(int(num), int(den))
    return Fraction(fps).limit_denominator(100000)


def resolve_frame_rate_strategy(source_fps_frac, speed, target_fps):
    """Determines EXACTLY how a segment's frames reach the target output
    frame rate, using rational (not floating-point) arithmetic to tell a
    genuinely clean small-integer ratio apart from something that merely
    LOOKS close in decimal. All arithmetic here is exact Fraction math --
    speed values like 2.5/5.0/1.25 are exactly representable in binary
    floating point, so Fraction(speed) carries no rounding error.

    Speeds are restricted to exact ratios by design (see validate_speed and
    its callers): every accepted speed maps to EXACTLY one whole source
    frame per target frame, N source frames per target frame, or 1 source
    frame per N target frames -- there is no generic/approximate case to
    fall back to, so an unclean ratio here signals a bug upstream (a speed
    that should have been rejected at argument-parsing time), not a case
    to handle gracefully.

    Returns one of:
      ('passthrough', None)  -- effective rate (source_fps * speed) already
          equals target_fps exactly (e.g. 23.976fps source at 2.5x lands on
          exactly 59.94fps): no frame duplication or dropping needed.
      ('decimate', n)  -- effective rate is exactly n times FASTER than
          target (e.g. 5x on a 23.976fps source: 119.88fps effective is
          exactly 2x 59.94fps): keep exactly 1 of every n frames via a
          frame-INDEX-based `select` filter (not a PTS/time comparison),
          unambiguous regardless of any floating-point timestamp noise
          elsewhere in the chain.
      ('duplicate', n)  -- effective rate is exactly n times SLOWER than
          target (e.g. 1.25x on a 23.976fps source: 29.97fps effective is
          exactly half of 59.94fps): each frame repeated n times.
      (None, None) -- not a clean ratio. Should never happen for a speed
          that passed validate_speed."""
    speed_frac = Fraction(speed).limit_denominator(1000000)
    target_frac = target_fps_fraction(target_fps)
    effective = source_fps_frac * speed_frac
    ratio = target_frac / effective  # target frames per effective frame

    MAX_CLEAN_N = 12  # generous ceiling; beyond this it's not worth special-casing
    if ratio == 1:
        return ("passthrough", None)
    if ratio.numerator == 1 and 1 < ratio.denominator <= MAX_CLEAN_N:
        return ("decimate", ratio.denominator)
    if ratio.denominator == 1 and 1 < ratio.numerator <= MAX_CLEAN_N:
        return ("duplicate", ratio.numerator)
    return (None, None)


def validate_speed(name, speed, source_fps_frac, target_fps):
    """Speeds are restricted to values that map cleanly onto the target
    frame grid (see resolve_frame_rate_strategy) -- 1 source frame : 1
    target frame, N source : 1 target, or 1 source : N target, with no
    fractional/sub-frame remainder. This is what makes exact frame-rate
    matching possible without any resampling pass. Exits with a clear,
    actionable error (listing the valid speeds for THIS source's actual
    frame rate) if `speed` doesn't qualify."""
    strategy, _ = resolve_frame_rate_strategy(source_fps_frac, speed, target_fps)
    if strategy is not None:
        return
    valid = []
    for n in range(1, 13):
        s = float(target_fps_fraction(target_fps) / (source_fps_frac * n))  # duplicate: 1->n
        if resolve_frame_rate_strategy(source_fps_frac, s, target_fps)[0] is not None:
            valid.append(s)
        s = float(target_fps_fraction(target_fps) * n / source_fps_frac)  # decimate: n->1
        if resolve_frame_rate_strategy(source_fps_frac, s, target_fps)[0] is not None:
            valid.append(s)
    valid = sorted(set(round(v, 4) for v in valid))
    sys.exit(f"{name}={speed} doesn't map cleanly onto this source's frame rate "
              f"({float(source_fps_frac):.3f}fps) at the target rate ({target_fps:.3f}fps).\n"
              f"Valid speeds for this source: {', '.join(f'{v:g}' for v in valid)}")


def hwaccel_decode_args(encoder_mode):
    if encoder_mode == "qsv":
        return ["-hwaccel", "qsv", "-hwaccel_device", "hw", "-hwaccel_output_format", "qsv"]
    if encoder_mode == "vaapi":
        return ["-hwaccel", "vaapi", "-hwaccel_device", "va", "-hwaccel_output_format", "vaapi"]
    if encoder_mode == "nvenc":
        # No -hwaccel_device here (unlike qsv/vaapi): a bare "cuda" device
        # index of 0 is the right default for the common single-GPU
        # passthrough case (NVIDIA_VISIBLE_DEVICES selects WHICH host
        # GPU(s) the container can see at all; there's normally only one
        # visible once that's set). No explicit -init_hw_device/
        # -filter_hw_device is needed on the encode side either (see
        # video_encode_args) -- decoded cuda frames' hw_frames_ctx
        # propagates through the filter graph to nvenc automatically,
        # unlike qsv/vaapi which both require an explicit device init.
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    return []


def video_encode_args(encoder_mode, speed=1.0, burn_srt_path=None, vaapi_device="/dev/dri/renderD128",
                       hw_decode=False, crf=20, qsv_quality=23, vaapi_qp=23, nvenc_cq=23,
                       video_codec="h264"):
    """crf/qsv_quality/vaapi_qp/nvenc_cq: per-encoder quality knobs (all
    lower = higher quality, on roughly comparable ~1-51 scales), used the
    same way for every re-encode call site -- per-segment cuts and the
    concat-fallback re-encode alike -- since stream-copy concat (the
    default path) means there's normally only ever one encode generation
    for any given frame.
    video_codec ("h264" or "hevc") selects WHAT to encode to; encoder_mode
    selects the VENDOR doing it (qsv/vaapi/nvenc/cpu) -- orthogonal knobs,
    see --video-codec/--encoder. qsv/vaapi/nvenc all name their encoder as
    f"{video_codec}_{encoder_mode}" (e.g. "hevc_vaapi"), which is why AMD
    needs no separate vendor of its own here: it's the exact same VAAPI
    device/encoder names as Intel, just backed by Mesa's radeonsi driver
    instead of Intel's iHD one for whichever GPU /dev/dri actually points
    at (see the Dockerfile).
    No target-fps/-r/-fps_mode forcing here: speeds are restricted to exact
    small-integer frame-rate ratios (see resolve_frame_rate_strategy), so
    every segment's frames already land exactly on the target rate by
    construction (decimate/duplicate/passthrough in cut_segment) -- there's
    nothing left for a CFR-forcing pass to correct, at either the
    per-segment or concat-fallback re-encode call sites."""
    vf_parts = []
    if speed != 1.0:
        vf_parts.append(f"setpts={1/speed}*PTS")

    if hw_decode and burn_srt_path:
        vf_parts.append("hwdownload")
        vf_parts.append("format=nv12")

    if burn_srt_path:
        esc = ffmpeg_escape_filter_path(burn_srt_path)
        style = ("FontName=DejaVu Sans,FontSize=22,PrimaryColour=&H00FFFFFF&,"
                  "OutlineColour=&H00000000&,BorderStyle=1,Outline=2,Shadow=0,MarginV=28")
        vf_parts.append(f"subtitles='{esc}':force_style='{style}'")

    skip_upload = hw_decode and not burn_srt_path

    if encoder_mode == "qsv":
        global_args = ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"]
        if not skip_upload:
            vf_parts.append("format=nv12,hwupload=qsv")
        codec_args = ["-c:v", f"{video_codec}_qsv", "-preset", "veryfast", "-global_quality", str(qsv_quality), "-bf", "0"]
    elif encoder_mode == "vaapi":
        global_args = ["-init_hw_device", f"vaapi=va:{vaapi_device}", "-filter_hw_device", "va"]
        if not skip_upload:
            vf_parts.append("format=nv12,hwupload")
        codec_args = ["-c:v", f"{video_codec}_vaapi", "-qp", str(vaapi_qp), "-bf", "0"]
    elif encoder_mode == "nvenc":
        # No -init_hw_device/-filter_hw_device (unlike qsv/vaapi): nvenc
        # accepts either plain software frames or an already-cuda
        # hw_frames_ctx from decode (see hwaccel_decode_args) without
        # needing an explicit device/filter graph setup either way -- one
        # of the few genuine simplifications nvenc has over qsv/vaapi, not
        # an oversight. -rc vbr -cq (rather than -rc constqp -qp, the
        # closer analogue to vaapi's -qp) is nvenc's own recommended
        # constant-quality mode -- constqp fixes every frame to the same
        # QP regardless of complexity, while vbr+cq lets the rate
        # controller vary bitrate per-frame around that quality target,
        # which is what actually behaves like libx264's -crf/vaapi's -qp
        # in practice (comparable output size for comparable content).
        global_args = []
        codec_args = ["-c:v", f"{video_codec}_nvenc", "-preset", "p2", "-rc", "vbr", "-cq", str(nvenc_cq), "-bf", "0"]
    else:
        global_args = []
        libx = "libx264" if video_codec == "h264" else "libx265"
        codec_args = ["-c:v", libx, "-preset", "veryfast", "-crf", str(crf), "-bf", "0"]

    vf = ",".join(vf_parts) if vf_parts else None
    return global_args, vf, codec_args


def _fallback_encoder_mode(mode, video_codec):
    """Next vendor to try after `mode` fails at runtime (used by both
    cut_segment's per-segment retry and the concat-fallback re-encode
    loop), or "cpu" if there's nowhere further to fall back to. Only qsv
    chains onward to vaapi -- both are Intel-only paths sharing the same
    /dev/dri device, so a qsv failure with a real Intel iGPU present often
    still has vaapi work (see --encoder's help text). vaapi and nvenc both
    fall straight to cpu instead of trying each other: a failure there
    means that vendor's own device/runtime is broken, not that some OTHER
    GPU vendor happens to be reachable instead, so trying a second
    hardware vendor blind is unlikely to help and just costs another
    failed attempt."""
    if mode == "qsv":
        return "vaapi" if encoder_listed("vaapi", video_codec) else "cpu"
    return "cpu"


def resolve_encoder(requested, video_codec):
    if requested == "cpu":
        print(f"Using {'libx264' if video_codec == 'h264' else 'libx265'} (CPU) encoding.")
        return "cpu"

    if requested == "qsv":
        if encoder_listed("qsv", video_codec):
            print(f"Using Intel QSV ({video_codec}_qsv) for hardware-accelerated encoding "
                  "(will auto-fallback to VAAPI, then CPU, per-segment if it fails at runtime).")
            return "qsv"
        print(f"[!] {video_codec}_qsv is not compiled into this ffmpeg build.")
        if encoder_listed("vaapi", video_codec):
            print(f"Falling back to VAAPI ({video_codec}_vaapi) instead.")
            return "vaapi"
        print("VAAPI isn't available either -- falling back to CPU.")
        return "cpu"

    if requested == "vaapi":
        if encoder_listed("vaapi", video_codec):
            print(f"Using VAAPI ({video_codec}_vaapi) for hardware-accelerated encoding -- this is also the "
                  "path AMD GPUs use (Mesa's radeonsi driver backing the same /dev/dri device, no separate "
                  "vendor setting needed) "
                  "(will auto-fallback to CPU per-segment if it fails at runtime).")
            return "vaapi"
        print(f"[!] {video_codec}_vaapi is not compiled into this ffmpeg build -- falling back to CPU.")
        return "cpu"

    if requested == "nvenc":
        if encoder_listed("nvenc", video_codec):
            print(f"Using NVIDIA NVENC ({video_codec}_nvenc) for hardware-accelerated encoding "
                  "(requires the NVIDIA Container Toolkit on the host -- see the compose file) "
                  "(will auto-fallback to CPU per-segment if it fails at runtime).")
            return "nvenc"
        print(f"[!] {video_codec}_nvenc is not compiled into this ffmpeg build -- falling back to CPU.")
        return "cpu"

    if encoder_listed("qsv", video_codec):
        print(f"Using Intel QSV ({video_codec}_qsv) for hardware-accelerated encoding "
              "(will auto-fallback to VAAPI, then CPU, per-segment if it fails at runtime).")
        return "qsv"
    if encoder_listed("vaapi", video_codec):
        print(f"{video_codec}_qsv not available in this ffmpeg build, using VAAPI ({video_codec}_vaapi) instead "
              "(also the AMD path -- will auto-fallback to CPU per-segment if it fails at runtime).")
        return "vaapi"
    if encoder_listed("nvenc", video_codec):
        print(f"Neither QSV nor VAAPI available, using NVIDIA NVENC ({video_codec}_nvenc) instead "
              "(will auto-fallback to CPU per-segment if it fails at runtime).")
        return "nvenc"
    print("No hardware encoder available in this ffmpeg build, using CPU.")
    return "cpu"


_SIDE_LAYOUT_FIX = {
    "5.1(side)": "5.1",
    "6.1(front)": "6.1",
    "7.1(wide)": "7.1",
    "7.1(wide-side)": "7.1",
}
_CHANNELS_TO_SAFE_LAYOUT = {6: "5.1", 8: "7.1"}


def surround_layout_fix(channels, channel_layout):
    if channel_layout in _SIDE_LAYOUT_FIX:
        return f"aformat=channel_layouts={_SIDE_LAYOUT_FIX[channel_layout]}"
    if not channel_layout and channels in _CHANNELS_TO_SAFE_LAYOUT:
        return f"aformat=channel_layouts={_CHANNELS_TO_SAFE_LAYOUT[channels]}"
    return None


def audio_bitrate_for_channels(channels):
    """AAC bitrate scaled to channel count, ~80kbps/channel -- a flat rate
    (this project's prior behavior: 160k always) sounds noticeably
    compressed on discrete surround channels (a 160k budget split across
    6 channels is ~27kbps/channel, well below what AAC needs to sound
    clean once channels are split out) while being needlessly generous
    for mono. 80kbps/channel is a common encoding convention for AAC
    surround, not an arbitrary number -- lands stereo at 160k (same as
    the old flat default, so no regression there) and 5.1 at 480k.
    Falls back to a stereo-equivalent rate if the channel count isn't
    known (e.g. ffprobe reported something non-numeric)."""
    ch = channels if isinstance(channels, int) and channels > 0 else 2
    return f"{ch * 80}k"


def atempo_chain(speed):
    filters = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining}")
    return ",".join(filters)


def verify_and_fix_frame_count(seg_path, expected_frames, video_track_timescale=None):
    """Verifies a cut segment's actual output frame count matches the exact
    value computed ahead of time (from pair-snapped source-frame counts),
    and corrects it if not -- deterministically, regardless of WHY it
    doesn't match. -frames:v (in cut_segment) only caps a maximum; it can't
    pad a stream that -fps_mode cfr's internal tail-handling produced
    fewer frames than expected. Confirmed directly: the SAME cut_segment
    code, same source, same boundaries, gave 480 (exact) on ffmpeg 6.1.1,
    479 on 7.1.1, and a much larger deviation on 8.0.1 -- cfr's exact
    tail behavior genuinely differs across ffmpeg versions, in both
    directions (over AND under). Rather than chasing which specific
    filter/flag combination is version-stable (repeatedly proven not to
    be), this checks the real output and fixes it post-hoc: trims excess
    via a fast lossless stream-copy remux, or pads a shortfall by cloning
    the last frame via the `tpad` filter (a quick, small re-encode -- only
    ever a handful of frames). Silently returns if ffprobe can't read the
    file or the counts already match; failures here are logged, not
    raised, since this is a correctness refinement on an already-usable
    segment, not worth aborting the whole run over.
    video_track_timescale, when given, is passed straight through to
    whichever ffmpeg remux this runs -- MUST be the same common_tb
    cut_segment used to produce seg_path in the first place. Without it,
    this remux gets its own independently-chosen (and different) default
    timescale, silently reintroducing exactly the per-segment-timescale
    concat corruption this same value was already added to cut_segment to
    prevent -- confirmed directly: a duplicate-strategy segment that took
    this correction path came out with a different time_base than its
    neighbors even after cut_segment's own timescale fix, because this
    function's remux wasn't told about it yet."""
    try:
        r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=nb_frames", "-of", "csv=p=0", seg_path], capture=True)
        actual = int(r.stdout.strip())
    except (ValueError, AttributeError):
        return
    if actual == expected_frames:
        return
    tmp_path = seg_path + ".fixframes.mp4"
    ts_args = ["-video_track_timescale", str(video_track_timescale)] if video_track_timescale else []
    try:
        if actual > expected_frames:
            cmd = ["ffmpeg", "-nostdin", "-y", "-i", seg_path,
                   "-frames:v", str(expected_frames), "-c", "copy"] + ts_args + [tmp_path]
        else:
            missing = expected_frames - actual
            cmd = ["ffmpeg", "-nostdin", "-y", "-i", seg_path,
                   "-vf", f"tpad=stop_mode=clone:stop={missing}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-bf", "0",
                   "-c:a", "copy"] + ts_args + [tmp_path]
        r = run(cmd)
        if r.returncode == 0:
            os.replace(tmp_path, seg_path)
        else:
            print(f"  [!] Frame-count correction failed for {seg_path} "
                  f"({actual} vs expected {expected_frames}), leaving as-is: "
                  f"{(r.stderr or '').strip().splitlines()[-1:]}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    except Exception as e:
        print(f"  [!] Frame-count correction errored for {seg_path}: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def cut_segment(src, start, end, out_path, reencode, speed=1.0, encoder_mode="cpu",
                 vaapi_device="/dev/dri/renderD128", audio_stream_index=None, audio_channels=None,
                 burn_srt_path=None, target_fps=None, hw_decode=False, surround_fix=None,
                 crf=14, qsv_quality=16, vaapi_qp=16, nvenc_cq=16, video_codec="h264",
                 is_final_segment=False, source_fps_frac=None, debug_speed_overlay=False):
    duration = end - start
    map_args = ["-map", "0:v:0"]
    if audio_stream_index is not None:
        map_args += ["-map", f"0:{audio_stream_index}"]

    if not reencode:
        cmd = [
            "ffmpeg", "-nostdin", "-y", "-ss", f"{start}", "-t", f"{duration}", "-i", src,
        ] + map_args + [
            "-c", "copy", "-avoid_negative_ts", "make_zero", out_path
        ]
        r = run(cmd)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed on segment {start}-{end}:\n{r.stderr[-2000:]}")
        return

    # Frame-rate strategy: speeds are validated up front (validate_speed, in
    # process_one) to guarantee an EXACT small-integer relationship between
    # a segment's natural (source_fps * speed) rate and the target rate --
    # there's no non-clean/approximate case reaching this point. The
    # default speeds (2.5x/5x/1.25x on a 23.976fps source) all qualify, by
    # design of the NTSC math (target_fps = 2.5 * source_fps exactly, since
    # both share the same /1001 denominator, which cancels): 2.5x needs no
    # correction at all, 5x is an exact 2:1 frame drop, 1.25x is an exact
    # 1:2 frame duplication. These are implemented explicitly and
    # deterministically (frame-INDEX based for decimation, not a time
    # comparison), so there's no reliance on a resampler's internal
    # precision at all -- no -r/-fps_mode forcing anywhere in this function.
    strategy, strategy_n = resolve_frame_rate_strategy(source_fps_frac, speed, target_fps)

    if strategy == "decimate":
        # framestep=N, NOT a second `select` filter here. Confirmed by
        # direct testing: chaining a frame-index `select` (e.g.
        # select='not(mod(n\,2))') immediately after the boundary-trim
        # `select` above corrupts frame counting entirely -- in a
        # controlled test this kept 239 of 240 frames instead of the
        # correct 120, i.e. it barely decimated anything. framestep is
        # purpose-built for "keep every Nth frame" and doesn't have this
        # chaining problem.
        rate_prefix = f"framestep={strategy_n}"
        ve_speed = speed
    elif strategy == "duplicate":
        # `fps=` filter targeting DOUBLE the real target rate, then
        # framestep=2 back down -- all as filters, feeding ONE final
        # encode. This replaces an earlier two-pass approach (encode to
        # double rate via -r/-fps_mode cfr, decode, decimate, encode
        # again) that worked but cost a genuine second lossy generation.
        # `fps=` alone, targeting the real rate directly, was already
        # ruled out earlier for its own rounding imprecision (494 vs an
        # expected exact 480 in a controlled test) -- but at DOUBLE rate,
        # direct testing shows the DUPLICATION PATTERN itself comes out
        # perfectly clean (uniform pairs throughout, zero judder), with
        # only a small total-COUNT shortfall (122 vs 124 in the same
        # test) -- a fundamentally more benign kind of imprecision than
        # pattern corruption, and exactly what verify_and_fix_frame_count
        # (below) already exists to correct. framestep=2 then brings it
        # back down using the same frame-index mechanism already proven
        # artifact-free for the decimate strategy.
        double_rate_str = fps_arg_str(target_fps * 2) if target_fps else "0"
        rate_prefix = f"setpts={1/speed}*PTS,fps=fps={double_rate_str}:round=near,framestep=2"
        ve_speed = 1.0
    else:  # passthrough
        rate_prefix = ""
        ve_speed = speed

    effective_hw_decode = hw_decode and encoder_mode in ("qsv", "vaapi", "nvenc")
    global_args, vf, codec_args = video_encode_args(encoder_mode, ve_speed, burn_srt_path, vaapi_device,
                                                      hw_decode=effective_hw_decode,
                                                      crf=crf, qsv_quality=qsv_quality, vaapi_qp=vaapi_qp,
                                                      nvenc_cq=nvenc_cq, video_codec=video_codec)
    decode_args = hwaccel_decode_args(encoder_mode) if effective_hw_decode else []

    # Frame-exact trimming. Repeated attempts trusting ffmpeg's -ss/-t to
    # resolve to exactly the intended [start, end) window proved unreliable
    # in practice (confirmed by frame-diffing adjacent segment outputs --
    # see the extensive history above). Rather than continuing to guess at
    # -ss/-t's internal rounding behavior, -ss/-t here are now only a
    # coarse, generously-padded seek for DECODE EFFICIENCY -- landing
    # roughly in the right place so we're not decoding from the start of
    # the file every time. The actual inclusion decision is made
    # explicitly, by inspecting each decoded frame's own absolute
    # timestamp: video via the `select` filter (t >= start AND t < end,
    # strict on the upper bound so the frame AT `end` -- which belongs to
    # the NEXT segment -- is excluded by construction, not by hoping a
    # duration cutoff lands in the right place); audio via the analogous
    # `atrim` filter. -copyts keeps every frame's PTS as true absolute
    # source time (ffmpeg's default otherwise rebases PTS to ~0 after a
    # seek), so these absolute-time comparisons are meaningful against our
    # globally-computed, frame-grid-snapped start/end. setpts=PTS-STARTPTS
    # (asetpts for audio) then resets the segment's own output timeline to
    # start at 0, same as any normally-cut segment, before speed change and
    # everything else downstream.
    SEEK_PAD = 0.25
    ss_arg = max(0.0, start - SEEK_PAD)
    if is_final_segment:
        select_expr = f"select='gte(t\\,{start:.6f})'"
        atrim_expr = f"atrim=start={start:.6f}"
        t_args = []
    else:
        select_expr = f"select='gte(t\\,{start:.6f})*lt(t\\,{end:.6f})'"
        atrim_expr = f"atrim=start={start:.6f}:end={end:.6f}"
        read_duration = (end - ss_arg) + SEEK_PAD
        t_args = ["-t", f"{read_duration}"]

    # select_prefix (boundary trim) always runs first; rate_prefix (the
    # exact decimate/duplicate step, if any) runs next, BEFORE whatever
    # video_encode_args itself contributes (which may include its own
    # speed setpts for the decimate/passthrough/fallback cases, or nothing
    # further for duplicate, where the speed setpts was already folded into
    # rate_prefix above).
    #
    # settb=1/1000000, FIRST in the chain (before even the boundary
    # select), forces a fine internal timebase for the whole filter graph.
    # This turned out to be essential, not optional: the fix below
    # (setpts=N/(source_fps*TB), regenerating every frame's PTS from its
    # sequential index rather than the source's own decoded timestamps)
    # LOOKED right in isolation but measurably did NOT fix anything when
    # tested end-to-end -- confirmed directly: a real source's PTS showed
    # the classic MKV 1ms-timebase rounding (0.041s/0.042s alternating
    # instead of the true constant 0.041708s), and after the N/(fps*TB)
    # "fix" the output STILL showed that exact same alternation. The
    # regeneration formula was mathematically correct, but its RESULT was
    # then rounded right back onto the same coarse ~1ms timebase inherited
    # from the source, reproducing an equivalent jitter pattern regardless
    # of how precisely the formula computed it. Forcing microsecond
    # precision before any of this PTS math runs is what actually breaks
    # that cycle -- confirmed directly: with settb added, the same source
    # produces perfectly uniform 0.041708s/0.041709s spacing, matching the
    # true NTSC period to microsecond rounding only.
    #
    # regen_prefix (setpts=N/(source_fps*TB)) itself: REGENERATES every
    # frame's PTS purely from its sequential INDEX and the known-exact
    # source frame rate, discarding whatever the source's own actual
    # decoded timestamps were -- necessary because real source files can
    # have genuine timing irregularity in their stored timestamps, which
    # the decimate/duplicate math downstream assumes is perfectly uniform.
    # The boundary select still correctly uses the source's REAL absolute
    # timestamps to decide WHICH frames belong in this segment -- only the
    # frames' OWN reported timing, not their inclusion/exclusion, gets
    # discarded and regenerated.
    # common_tb: a timebase where BOTH the source's frame period AND the
    # target's frame period land on exact integer tick counts -- not just
    # "fine enough" (microsecond) precision, but genuinely exact, no
    # rounding at all. Needed after switching cut_segment's final step
    # from "-r target -fps_mode cfr" to "-fps_mode passthrough" (see
    # below): passthrough trusts whatever PTS this filter chain hands it,
    # with no CFR resampling left to paper over rounding drift. At plain
    # microsecond precision (settb=1/1000000, the previous fixed value),
    # 1001/60000s (the true NTSC target period) is 16683.3333...
    # microseconds -- NOT an integer -- so every single frame's PTS
    # carried a small, systematic rounding residue. Confirmed directly
    # this residue is what caused the deployed output's packet durations
    # to come out lumpy (four ~1-tick packets then one ~5001-tick packet,
    # repeating every 5 frames, at the muxer's 60000 timescale) even
    # though total duration and frame content were both already correct
    # by that point. cross-multiplying the two rates' numerators
    # (source_fps_frac.numerator * target's) guarantees both periods are
    # exact integers at this timebase, for ANY source/target rate pair,
    # not just the standard NTSC one -- the standard textbook trick for
    # finding a common denominator between two rates.
    if target_fps:
        _target_frac = target_fps_fraction(target_fps)
        common_tb = source_fps_frac.numerator * _target_frac.numerator
    else:
        common_tb = 1000000
    regen_prefix = f"setpts=N/({float(source_fps_frac)}*TB)"
    select_prefix = f"settb=1/{common_tb},{select_expr},setpts=PTS-STARTPTS,{regen_prefix}"
    prefix_parts = [select_prefix] + ([rate_prefix] if rate_prefix else [])
    prefix = ",".join(prefix_parts)
    if debug_speed_overlay:
        # Burns the assigned speed multiplier into a corner of the frame --
        # for visually confirming, while actually watching the output,
        # which segments got classified which way. MUST run before
        # video_encode_args' own vf contribution, not after: for QSV/VAAPI,
        # that contribution includes hwupload (moving frames onto the GPU
        # for hardware encoding), and drawtext is software-only -- a
        # software filter after a hardware upload breaks the filter graph
        # ("Conversion failed!" on every single segment, confirmed
        # directly). Appending to `prefix` (still CPU-side at this point,
        # before video_encode_args' vf is appended below) keeps this
        # correct regardless of encoder.
        label = f"{speed:.2f}x"
        overlay = (f"drawtext=text='{label}':fontcolor=white:fontsize=36:"
                   f"box=1:boxcolor=black@0.6:boxborderw=8:x=10:y=10")
        prefix = f"{prefix},{overlay}"

    vf = f"{prefix},{vf}" if vf else prefix

    # -fps_mode passthrough, NOT "-r <target> -fps_mode cfr" -- this
    # reverses an earlier decision (see git history), found wrong by
    # direct A/B testing against real source content, not just reasoning
    # about it, across three rounds of "fix one thing, discover the fix
    # exposed/caused another" -- each confirmed by frame-content diffing
    # cut_segment's own output against a ground-truth reference (every-Nth
    # source frame extracted independently, no speed/rate filtering at
    # all), not by reasoning about the filter graph in the abstract:
    # (1) With NO explicit -fps_mode, ffmpeg's default output frame-pacing
    #     silently DROPS the large majority of frames whenever setpts has
    #     altered their timing (confirmed: a plain setpts=0.2*PTS on its
    #     own dropped ~80% of frames in a controlled test) -- independent
    #     of whether the filter graph computed correct timestamps. Still
    #     true, still why -fps_mode is never omitted.
    # (2) "-r <target> -fps_mode cfr" -- the previous choice here -- was
    #     ALSO wrong, just less obviously: confirmed by diffing a long
    #     (566-frame) decimated segment's actual output against ground
    #     truth, frame by frame, that cfr silently drops/duplicates
    #     roughly 1 frame in every 5 THROUGHOUT the segment (not just at
    #     the tail) even though the upstream select/regen/framestep math
    #     puts every frame's PTS exactly on the target grid already (by
    #     construction -- see resolve_frame_rate_strategy). cfr's
    #     nearest-slot resampling algorithm is sensitive to sub-microsecond
    #     floating-point noise in those already-correct PTS values, and
    #     round trips some frames into the wrong output slot. This is
    #     silent: the TOTAL frame count still comes out exactly right
    #     (compensating drops with duplicates elsewhere), which is all
    #     verify_and_fix_frame_count (below) checks -- it's specifically
    #     invisible to a count-only check. Confirmed reproducible
    #     regardless of hardware vs software decode (rules out a decoder
    #     quirk) and confirmed present in this real code path, not just an
    #     isolated filter-string test.
    # (3) Switching straight to passthrough (no other change) traded that
    #     bug for two new ones, both confirmed on a real multi-segment
    #     run, not just an isolated single segment:
    #     (a) Concat corruption -- without -r, the mp4 muxer picked each
    #         segment's OWN container timescale heuristically from
    #         whatever its filter chain produced (confirmed via
    #         --dump-segments: 1/12000 for decimate segments, 1/60000 for
    #         duplicate segments, in the SAME run), and stream-copy concat
    #         doesn't reconcile differing input timescales -- splicing a
    #         1/12000 segment next to a 1/60000 one threw the concatenated
    #         file's overall duration off by roughly their ratio
    #         (confirmed: a real 1m38s-of-content run came out reporting
    #         3m50s; a full 48-minute episode came out 140 minutes). Fixed
    #         by -video_track_timescale, below, using the SAME common_tb
    #         (an exact, not just fine-grained, shared timebase --
    #         cross-multiplying the source and target rates' numerators
    #         so both periods land on whole ticks with zero rounding, for
    #         any source/target pair) on every segment, so concat always
    #         sees matching timescales.
    #     (b) Pacing corruption -- fixing (a) alone was NOT enough:
    #         packet-level inspection of a real deployed file (not just
    #         ffprobe's decoded-frame view, which can mask this) showed
    #         every 5th frame absorbing nearly the entire 5-frame span's
    #         duration while the other 4 shared its exact PTS (0 ticks
    #         apart) -- correct total timing and correct frame content/
    #         order, but any player honoring those timestamps would show
    #         4 frames flash by simultaneously then hold for 5 frame-
    #         periods, repeating throughout. This was NOT a timebase
    #         precision issue (reproduced identically even with common_tb
    #         giving exact, zero-rounding ticks) -- root cause is the
    #         ENCODER choosing its OWN internal timebase heuristically
    #         when none is given explicitly (historically -r's job),
    #         independent of what the filter graph or -video_track_
    #         timescale declare, and rebasing incoming frame PTS onto
    #         that mismatched grid. -enc_time_base -1, below, forces the
    #         encoder to use the filter graph's OWN timebase instead of
    #         guessing -- confirmed directly this alone fixes the
    #         clustering (perfectly uniform PTS afterward), independent
    #         of and in addition to the concat fix in (a). Re-verified
    #         content-clean against ground truth AND pacing-clean at the
    #         packet level after both fixes, for all three speed
    #         strategies and confirmed on the actual QSV hardware encoder
    #         used in production (not just the libx264 fallback).
    cmd = ["ffmpeg", "-nostdin", "-y", "-copyts"] + global_args + decode_args + [
        "-ss", f"{ss_arg}"
    ] + t_args + ["-i", src] + map_args
    if vf:
        cmd += ["-vf", vf]
    # No -r here: ffmpeg rejects -r together with a non-CFR -fps_mode
    # ("contradictory") -- passthrough already trusts the filter graph's
    # own (already-exact, see above) PTS, so there's nothing for -r to add.
    # -video_track_timescale uses the SAME common_tb as settb above (not
    # a separate hardcoded value) so every segment's container declares
    # an identical, exact timescale regardless of which strategy or
    # encoder produced it -- see (3)(a) above. -enc_time_base -1 forces
    # the ENCODER (a separate stage from the muxer) to also use the
    # filter graph's own timebase instead of guessing its own -- see
    # (3)(b) above; without it, -video_track_timescale alone is not
    # sufficient.
    cmd += ["-fps_mode", "passthrough", "-video_track_timescale", str(common_tb),
            "-enc_time_base", "-1"]
    expected_frames = None
    if not is_final_segment and source_fps_frac:
        # -fps_mode cfr, confirmed by direct testing, pads a couple of
        # extra duplicate frames at the tail beyond the filtered content's
        # actual end -- reproducible regardless of -ss/-t padding (tested
        # down to zero and even negative slack, same +2 every time), so
        # it's CFR's own stream-ending behavior, not a symptom of our seek
        # padding. Since segments are snapped to the native frame grid
        # (pairs, even), the exact correct output frame count is known
        # ahead of time and enforced directly with -frames:v, closing this
        # off regardless of CFR's internal cause. Skipped for the final
        # segment: it has no upper time bound by design (extends to the
        # true end of the source), so there's no independently-computable
        # expected count to enforce without risking truncating real content.
        n_source_frames = round((end - start) * float(source_fps_frac))
        if strategy == "decimate":
            expected_frames = n_source_frames // strategy_n
        elif strategy == "duplicate":
            expected_frames = n_source_frames * strategy_n
        else:
            expected_frames = n_source_frames
        cmd += ["-frames:v", str(expected_frames)]
    cmd += codec_args
    if audio_stream_index is not None:
        audio_filters = [atrim_expr, "asetpts=PTS-STARTPTS"]
        if surround_fix:
            audio_filters.append(surround_fix)
        audio_filters.append(atempo_chain(speed))
        cmd += ["-af", ",".join(audio_filters), "-c:a", "aac", "-b:a", audio_bitrate_for_channels(audio_channels)]
    cmd += [out_path]

    r = run(cmd)
    if r.returncode != 0:
        if _interrupted.is_set():
            raise RuntimeError(f"ffmpeg interrupted on segment {start}-{end}")
        if encoder_mode in ("qsv", "vaapi", "nvenc"):
            next_mode = _fallback_encoder_mode(encoder_mode, video_codec)
            stderr_tail = (r.stderr or "").strip().splitlines()
            hint = {
                "qsv": "Check Intel media driver / oneVPL runtime / /dev/dri access if this recurs often, "
                       "or GPU contention if --jobs is high.",
                "vaapi": "Check the VAAPI driver (intel-media-va-driver-non-free for Intel, mesa-libgallium "
                         "for AMD) / /dev/dri access if this recurs often.",
                "nvenc": "Check the NVIDIA Container Toolkit / NVIDIA_VISIBLE_DEVICES / host driver install "
                         "if this recurs often -- or the concurrent-session limit some consumer GPUs "
                         "enforce, if --jobs is high.",
            }[encoder_mode]
            print(f"  [!] {video_codec}_{encoder_mode} failed at runtime on segment {start:.2f}-{end:.2f}s, "
                  f"retrying with {next_mode}: {stderr_tail[-1] if stderr_tail else '(no stderr)'}")
            warn_once(encoder_mode, f"      (other segments still try {encoder_mode.upper()} first -- this "
                                     f"isn't a persistent downgrade for the rest of the run. {hint})")
            cut_segment(src, start, end, out_path, reencode=True, speed=speed, encoder_mode=next_mode,
                        vaapi_device=vaapi_device, audio_stream_index=audio_stream_index,
                        audio_channels=audio_channels, burn_srt_path=burn_srt_path,
                        target_fps=target_fps, hw_decode=hw_decode, surround_fix=surround_fix,
                        crf=crf, qsv_quality=qsv_quality, vaapi_qp=vaapi_qp, nvenc_cq=nvenc_cq,
                        video_codec=video_codec, is_final_segment=is_final_segment,
                        source_fps_frac=source_fps_frac, debug_speed_overlay=debug_speed_overlay)
            return
        raise RuntimeError(f"ffmpeg failed on segment {start}-{end}:\n{r.stderr[-2000:]}")

    if expected_frames is not None:
        verify_and_fix_frame_count(out_path, expected_frames, video_track_timescale=common_tb)


class TimeMapper:
    def __init__(self, segments):
        self.segments = segments
        self.orig_starts = []
        self.new_starts = []
        new_cursor = 0.0
        for (s, e, actual_new_duration) in segments:
            self.orig_starts.append(s)
            self.new_starts.append(new_cursor)
            new_cursor += actual_new_duration
        self.total_new = new_cursor

    def map(self, t):
        idx = bisect.bisect_right(self.orig_starts, t) - 1
        idx = max(0, min(idx, len(self.segments) - 1))
        s, e, actual_new_duration = self.segments[idx]
        t_clamped = min(max(t, s), e)
        orig_dur = e - s
        offset = (t_clamped - s) * (actual_new_duration / orig_dur) if orig_dur > 0 else 0.0
        return self.new_starts[idx] + offset


def _write_pidfile(tmpdir):
    with open(os.path.join(tmpdir, ".pid"), "w") as f:
        f.write(str(os.getpid()))


def _pid_is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def cleanup_stale_tempdirs():
    base = tempfile.gettempdir()
    try:
        entries = os.listdir(base)
    except OSError:
        return
    removed = []
    for name in entries:
        if not name.startswith("abridge_"):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        pidfile = os.path.join(path, ".pid")
        stale = True
        if os.path.exists(pidfile):
            try:
                with open(pidfile) as f:
                    pid = int(f.read().strip())
                stale = not _pid_is_alive(pid)
            except (ValueError, OSError):
                stale = True
        if stale:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name)
    if removed:
        print(f"Cleaned up {len(removed)} leftover temp dir(s) from a previous interrupted run: "
              f"{', '.join(removed)}")


def process_one_dialog_mode(input_path, output_path, args, encoder_mode, vaapi_device):
    """Handles --speed dialog: the ENTIRE file plays at a single
    uniform speed (the same pace normal dialog segments always get) --
    no subtitle-based dialog/gap detection, no segmentation, no
    duplicate/decimate frame math at all. This works because dialog
    speed is BY DEFINITION exactly (target_fps/source_fps)/2, so
    source_fps * dialog_speed always simplifies to exactly target_fps/2
    = 30000/1001 -- a FIXED constant, regardless of the source's actual
    native rate (confirmed directly for 24000/1001, the odd 500/21
    HDTV-capture rate found earlier in this project, and 25fps PAL: all
    three land on exactly 30000/1001). A plain 1:1 frame passthrough at
    that new rate -- no filter needed to duplicate or decimate anything
    -- IS the entire transformation, reusing cut_segment's existing,
    well-tested passthrough path rather than a new, less-tested one.
    Much simpler than the segmented high/low pipeline, and deliberately
    so: there is no action/calm distinction to make in this mode, so
    there's nothing for --debug-speed-overlay/--debug-speed-subtitle to
    usefully show, and they're not supported here."""
    try:
        print("Probing duration...")
        duration = get_duration(input_path)
        print(f"Duration: {duration:.2f}s")

        source_fps_frac = get_source_video_fps_fraction(input_path)
        target_frac = Fraction(60000, 1001)
        speed = float(target_frac / source_fps_frac / 2)
        new_target_frac = target_frac / 2  # always exactly 30000/1001
        new_target_fps = float(new_target_frac)
        print(f"Source fps: {float(source_fps_frac):.3f} ({source_fps_frac.numerator}/{source_fps_frac.denominator}).")
        print(f"--speed dialog: whole file at {speed:.4f}x -> {new_target_fps:.3f}fps "
              f"({new_target_frac.numerator}/{new_target_frac.denominator}), 1 source frame per output frame.")

        audio_tracks = list_audio_tracks(input_path)
        chosen_audio = choose_audio_track(audio_tracks, args.audio_track, lang=args.lang) if audio_tracks else None
        audio_abs_index = chosen_audio["abs_index"] if chosen_audio else None
        surround_fix = None
        channels_int = None
        if chosen_audio:
            print(f"Using audio track: stream #{chosen_audio['abs_index']} "
                  f"(lang={chosen_audio['lang']}, codec={chosen_audio['codec_name']}, "
                  f"channels={chosen_audio['channels']}).")
            channels_int = chosen_audio["channels"] if isinstance(chosen_audio["channels"], int) else None
            surround_fix = surround_layout_fix(channels_int, chosen_audio.get("channel_layout", ""))
        else:
            print("No audio track found in input -- output will be video-only.")

        hw_decode = not args.no_hw_decode and encoder_mode in ("qsv", "vaapi", "nvenc")
        if hw_decode:
            source_codec = get_source_video_codec(input_path)
            if source_codec in _HW_DECODE_UNRELIABLE_CODECS:
                print(f"[!] Source codec {source_codec!r} has an unreliable {encoder_mode.upper()} hardware "
                      f"decoder -- forcing software decode for this file (hardware encode is unaffected).")
                hw_decode = False

        tmpdir = tempfile.mkdtemp(prefix="abridge_")
        try:
            sub_entries = None
            sub_lang = None
            if args.embed_subs:
                tracks = list_subtitle_tracks(input_path)
                chosen_sub = choose_subtitle_track(tracks, args.sub_track, lang=args.lang) if tracks else None
                if chosen_sub and chosen_sub.get("needs_ocr"):
                    extracted_path = ocr_pgs_subtitle(input_path, chosen_sub["abs_index"], chosen_sub["lang"], tmpdir)
                    if extracted_path:
                        sub_entries = parse_subtitles(extracted_path)
                        sub_lang = chosen_sub["lang"]
                elif chosen_sub:
                    extracted_path = os.path.join(tmpdir, "embedded_subs.srt")
                    extract_embedded_subtitle(input_path, chosen_sub["abs_index"], extracted_path)
                    sub_entries = parse_subtitles(extracted_path)
                    sub_lang = chosen_sub["lang"]
                if sub_entries:
                    print(f"Loaded {len(sub_entries)} subtitle line(s); rescaling by /{speed:.4f} "
                          f"(uniform speed, no per-segment remapping needed).")

            video_out = output_path
            out_ext = os.path.splitext(output_path)[1] or ".mp4"
            # final_tmp lives in the SAME directory as the real output path
            # (not tmpdir, which is a different filesystem -- confirmed
            # directly: tmpdir resolves under /tmp, an overlayfs layer
            # inside the container, while output_path is the bind-mounted
            # host media directory, a separate zfs filesystem; os.replace()
            # across different filesystems fails with EXDEV, so the atomic
            # rename below only works if the temp file is already on the
            # same filesystem as the destination). Whatever the very last
            # ffmpeg step produces is written here, then moved into place
            # with a single os.replace() -- a same-filesystem rename is
            # atomic, so video_out never exists in a partial state: it's
            # either absent or already the complete file. Without this, a
            # process killed mid-write (container restart, host reboot)
            # mid-way through writing directly to video_out would leave a
            # truncated file there, which main()'s "skip if output exists"
            # check would then mistake for a finished one forever.
            # A leading dot on the FILENAME (not the extension -- e.g.
            # ".movie.mp4", not "movie.part.mp4") hides it from Plex's/
            # Jellyfin's library scanners, which both skip dot-prefixed
            # files by convention. This matters because it genuinely
            # bit us: abridged-movies/abridged-shows are real, live-
            # scanned library roots (confirmed in compose.yml), and a
            # non-hidden temp file sitting there mid-encode -- even named
            # "movie.part.mp4" -- still ends in a recognized media
            # extension, so a scanner can pick it up and try to play it
            # while it's still being written, which looks exactly like a
            # corrupt file (no moov atom yet) even though the real,
            # finished output was never actually broken. The real
            # extension is kept LAST (unlike an earlier ".part.mp4"
            # attempt that put ".part" before it) because ffmpeg can't
            # infer a muxer/container format otherwise -- confirmed
            # directly, it fails outright ("Unable to choose an output
            # format") if the real extension isn't the last thing in the
            # filename.
            _out_dir, _out_name = os.path.split(video_out)
            final_tmp = os.path.join(_out_dir, f".{_out_name}")
            target_video = os.path.join(tmpdir, "video_only" + out_ext) if sub_entries else final_tmp

            print("Encoding whole file (single pass, passthrough)...")
            cut_segment(input_path, 0.0, duration, target_video, reencode=True, speed=speed,
                        encoder_mode=encoder_mode, vaapi_device=vaapi_device,
                        audio_stream_index=audio_abs_index, audio_channels=channels_int, target_fps=new_target_fps,
                        hw_decode=hw_decode, surround_fix=surround_fix,
                        crf=args.crf, qsv_quality=args.qsv_quality, vaapi_qp=args.vaapi_qp,
                        nvenc_cq=args.nvenc_cq, video_codec=args.video_codec,
                        is_final_segment=True, source_fps_frac=source_fps_frac)

            if sub_entries:
                rescaled = [(s / speed, e / speed, text_lines) for s, e, text_lines in sub_entries]
                resynced_srt = os.path.join(tmpdir, "resynced.srt")
                write_srt(resynced_srt, rescaled)
                print("Muxing subtitles into output container...")
                sub_codec = "mov_text" if out_ext.lower() in (".mp4", ".mov", ".m4v") else "srt"
                cmd = [
                    "ffmpeg", "-nostdin", "-y", "-i", target_video, "-i", resynced_srt,
                    "-map", "0:v", "-map", "0:a", "-map", "1:0",
                    "-c:v", "copy", "-c:a", "copy", "-c:s", sub_codec,
                    "-metadata:s:s:0", f"language={sub_lang or 'und'}",
                    final_tmp
                ]
                r = run(cmd)
                if r.returncode != 0:
                    raise RuntimeError(f"Muxing subtitles failed:\n{r.stderr[-3000:]}")

            os.replace(final_tmp, video_out)
            print(f"Done. Output video: {video_out}")
            new_duration = duration / speed
            def fmt(secs):
                m, s = divmod(secs, 60)
                return f"{int(m)}m{s:04.1f}s" if m else f"{s:.1f}s"
            print("")
            print("=== Summary ===")
            print(f"Total: {fmt(duration)} -> {fmt(new_duration)}  "
                  f"(saved {fmt(max(0, duration - new_duration))}, "
                  f"{(1 - new_duration/duration)*100:.1f}% shorter)")
            return True
        finally:
            if not args.keep_temp:
                shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        print(f"[!] Failed to process {input_path}: {e}")
        return False


def process_one(input_path, output_path, args, encoder_mode, vaapi_device):
    try:
        print("Probing duration...")
        duration = get_duration(input_path)
        print(f"Duration: {duration:.2f}s")

        # Resolved early (before anything else needs it): the source's
        # native frame rate, probed directly -- not assumed. Confirmed
        # directly on a real file: ffprobe's r_frame_rate and
        # avg_frame_rate agreed exactly at 500/21 (23.810fps) -- not a
        # probing bug, not VFR (that would show the two disagreeing), a
        # genuinely different constant rate (this happens on some ATSC/HDTV
        # broadcast captures). Speeds are derived FROM this to hit exact
        # 1:1/2:1/1:2 ratios against the fixed standard 59.94 target (see
        # below), so trusting the probe here matters regardless of what
        # the source's true rate turns out to be.
        source_fps = get_source_video_fps(input_path)
        source_fps_frac = get_source_video_fps_fraction(input_path)
        print(f"Source fps: {source_fps:.3f} ({source_fps_frac.numerator}/{source_fps_frac.denominator}, "
              f"from ffprobe).")

        # Target is ALWAYS the standard rate (60000/1001 = 59.94), by
        # default -- not derived per-file. Instead, the SPEEDS are derived
        # from whatever this file's actual source rate is, to hit exactly
        # 1:1 (low speed-up), 2:1 (high speed-up), and 1:2 (dialog)
        # relative to that fixed standard target -- a consistent,
        # predictable output rate across every file matters more than the
        # speed multiplier being exactly 5.0/2.5/1.25 on the rare file
        # whose native rate isn't standard NTSC (like the confirmed
        # 500/21 = 23.810fps HDTV capture that started this). These are no
        # longer separately configurable -- --speed (below)
        # is the only remaining speed-related knob.
        target_frac = Fraction(60000, 1001)
        target_fps = float(target_frac)
        base_speed = float(target_frac / source_fps_frac)  # 1:1, "low" speed-up
        resolved_min_speed = base_speed          # 1:1
        resolved_max_speed = base_speed * 2      # 2:1
        resolved_dialog_speed = base_speed / 2   # 1:2
        print(f"Target fps: {target_fps:.3f}. Derived speeds for this source -- "
              f"low(1:1)={resolved_min_speed:.4f}x, high(2:1)={resolved_max_speed:.4f}x, "
              f"dialog(1:2)={resolved_dialog_speed:.4f}x")

        is_mkv_out = os.path.splitext(output_path)[1].lower() == ".mkv"
        warn_if_mkv_unsafe(target_fps, is_mkv_out)

        audio_tracks = list_audio_tracks(input_path)
        chosen_audio = choose_audio_track(audio_tracks, args.audio_track, lang=args.lang) if audio_tracks else None
        audio_abs_index = chosen_audio["abs_index"] if chosen_audio else None
        surround_fix = None
        channels_int = None
        if chosen_audio:
            print(f"Using audio track: stream #{chosen_audio['abs_index']} "
                  f"(lang={chosen_audio['lang']}, codec={chosen_audio['codec_name']}, channels={chosen_audio['channels']}, "
                  f"layout={chosen_audio.get('channel_layout') or 'unset'})")
            channels_int = chosen_audio["channels"] if isinstance(chosen_audio["channels"], int) else None
            surround_fix = surround_layout_fix(channels_int, chosen_audio.get("channel_layout", ""))
            if surround_fix:
                print(f"  ffmpeg's AAC encoder mis-maps this layout (known to sound like dialogue is coming "
                      f"from only one side) -- relabeling to {surround_fix.split('=')[-1]} before encoding "
                      f"(same channels, no downmix).")
        else:
            print("No audio track found in input -- output will be video-only.")

        if target_fps:
            print(f"Forcing constant {target_fps:.3f}fps output on every re-encoded segment (use --fps 0 to disable).")

        hw_decode = not args.no_hw_decode and encoder_mode in ("qsv", "vaapi", "nvenc")
        if hw_decode:
            source_codec = get_source_video_codec(input_path)
            if source_codec in _HW_DECODE_UNRELIABLE_CODECS:
                print(f"[!] Source codec {source_codec!r} has an unreliable {encoder_mode.upper()} hardware "
                      f"decoder -- forcing software decode for this file (hardware encode is unaffected).")
                hw_decode = False
        if hw_decode:
            print(f"Decoding on the same {encoder_mode.upper()} device used for encoding "
                  "(pass --no-hw-decode to force software decode instead).")

        tmpdir = tempfile.mkdtemp(prefix="abridge_")
        _write_pidfile(tmpdir)
        concat_list_path = os.path.join(tmpdir, "concat_list.txt")

        # Subtitles are now the ONLY dialog-detection source -- the earlier
        # silence-detection fallback was unexercised dead weight in
        # practice (every real file run through this tool has had a usable
        # embedded track), and the whole exact frame-pair architecture
        # (snap_segments_to_frame_grid, decimate/duplicate math) is built
        # around subtitle-derived dialog ranges specifically. A file with
        # no usable embedded subtitle track now errors out clearly instead
        # of silently falling back to a less precise heuristic.
        sub_entries = None
        sub_lang = None
        tracks = list_subtitle_tracks(input_path)
        chosen = choose_subtitle_track(tracks, args.sub_track, lang=args.lang) if tracks else None
        if chosen and chosen.get("needs_ocr"):
            extracted_path = ocr_pgs_subtitle(input_path, chosen["abs_index"], chosen["lang"], tmpdir)
            if extracted_path:
                sub_entries = parse_subtitles(extracted_path)
                sub_lang = chosen["lang"]
                print(f"Loaded {len(sub_entries)} subtitle line(s) from OCR'd bitmap track.")
        elif chosen:
            extracted_path = os.path.join(tmpdir, "embedded_subs.srt")
            print(f"Extracting embedded subtitle track (stream #{chosen['abs_index']}, "
                  f"lang={chosen['lang']}, codec={chosen['codec_name']})...")
            extract_embedded_subtitle(input_path, chosen["abs_index"], extracted_path)
            sub_entries = parse_subtitles(extracted_path)
            sub_lang = chosen["lang"]
            print(f"Loaded {len(sub_entries)} subtitle line(s) from embedded track.")

        if not sub_entries:
            raise RuntimeError(
                f"No usable subtitle track found in {input_path} (no embedded text-based "
                "subtitle track -- or bitmap track OCR wasn't available/consented to/produced "
                "no lines). Dialog detection requires subtitles -- there is no silence-detection "
                "fallback anymore.")

        # SDH sound-cue lines (e.g. '[door creaks]') are always filtered out
        # of dialog analysis -- was --keep-sdh to disable; never used in
        # practice, always left at the default (filtered).
        sub_entries, dropped = filter_sdh(sub_entries)
        if dropped:
            print(f"Filtered out {dropped} hearing-impaired sound-cue line(s) "
                  f"(e.g. '[door creaks]') from dialog analysis; {len(sub_entries)} line(s) remain.")
        if not sub_entries:
            raise RuntimeError(
                f"No usable subtitle lines remained in {input_path} after filtering out "
                "hearing-impaired sound-cue lines.")

        raw_ranges = [(s, e) for s, e, _ in sub_entries]

        if not args.no_cross_lang_dialog_detection:
            extra_ranges = gather_cross_lang_dialog_ranges(input_path, tracks, sub_lang, tmpdir)
            if extra_ranges:
                print(f"Found {len(extra_ranges)} additional dialog-range cue(s) from other-language "
                      f"subtitle tracks (on-screen text/signage the {sub_lang} track correctly doesn't "
                      f"translate) -- merging into dialog detection.")
                raw_ranges = raw_ranges + extra_ranges

        # No pre-merge gap threshold here (was MERGE_GAP=0.35) -- redundant
        # with --min-nondialog-duration (default 2.0s), which runs
        # afterward on the built non-dialog SEGMENTS and always subsumes
        # it: any gap small enough for a raw-range pre-merge to catch is
        # also small enough for --min-nondialog-duration to catch and
        # merge away, producing an IDENTICAL final segment structure
        # either way (enforce_min_nondialog_duration flips short gaps to
        # dialog and merges adjacent same-type segments, same net effect
        # as merging the raw ranges upfront). Keeping both was worse than
        # redundant: a nonzero pre-merge value would silently override
        # --min-nondialog-duration for any gap smaller than it, if someone
        # ever set --min-nondialog-duration below 0.35.
        # --min-nondialog-duration is now the single source of truth for this.
        keep_ranges = merge_and_pad(raw_ranges, args.pad, 0.0, duration)
        print(f"Merged into {len(keep_ranges)} dialog range(s).")

        segments = build_segments_from_ranges(duration, keep_ranges)
        if args.min_nondialog_duration > 0:
            before = len(segments)
            segments = enforce_min_nondialog_duration(segments, args.min_nondialog_duration)
            if len(segments) != before:
                print(f"Merged non-dialog segments shorter than {args.min_nondialog_duration}s into surrounding "
                      f"dialog: {before} -> {len(segments)} segment(s).")

        # Snap every segment boundary onto the source's own native frame
        # grid. Without this, segment boundaries are arbitrary floating-
        # point seconds derived from subtitle/silence timestamps, and each
        # segment is cut by its own independent ffmpeg -ss/-t call -- two
        # separate invocations resolving the SAME nominal boundary value to
        # an actual source frame can each round differently, and can end up
        # both including the same native frame at the shared boundary
        # (confirmed directly by frame-diffing two adjacent segments and
        # finding identical content at the splice). Rounding each boundary
        # to the nearest native frame INDEX here, once, fixes this at the
        # source: segment i's end and segment i+1's start are the same
        # float before this call, so they round to the identical frame
        # index -- not two independently-rounded approximations that two
        # separate ffmpeg processes might resolve inconsistently.
        # (source_fps/source_fps_frac were already resolved -- possibly
        # via --source-fps -- near the top of this function.)
        segments = snap_segments_to_frame_grid(segments, source_fps)
        print(f"Snapped {len(segments)} segment boundary/boundaries to the native frame grid.")

        if target_fps:
            for sp in sorted({resolved_dialog_speed, resolved_min_speed, resolved_max_speed}):
                validate_speed("speed", sp, source_fps_frac, target_fps)
                strat, n = resolve_frame_rate_strategy(source_fps_frac, sp, target_fps)
                label = {"passthrough": "exact, no correction needed",
                         "decimate": f"exact, keep 1 of every {n} frames",
                         "duplicate": f"exact, duplicate every frame {n}x"}[strat]
                print(f"  {sp}x -> {label}")

        print(f"Built {len(segments)} segment(s): "
              f"{sum(1 for s in segments if s[2])} gap/non-dialog, "
              f"{sum(1 for s in segments if not s[2])} dialog.")

        debug_reaches_true_end = True  # overridden below only when --debug-segments truncates before the real end
        if args.debug_segments:
            # Truncates the segment list itself, early, rather than
            # filtering scattered pieces later (motion scoring, cut plan,
            # concat, TimeMapper) -- those would risk misalignment bugs if
            # filtered independently (e.g. measured_segments ending up
            # shorter than segments and zip()ing against the wrong original
            # entries). Truncating here means everything downstream just
            # sees a normal, smaller, fully self-consistent movie -- no
            # other code needs to know debug mode exists. For fast
            # iteration when chasing a specific segment's behavior (e.g.
            # comparing --encoder cpu vs auto on one problem spot) without
            # paying for a full-file run every time.
            total = len(segments)
            if "-" in args.debug_segments:
                lo, hi = args.debug_segments.split("-", 1)
                lo, hi = int(lo), int(hi)
            else:
                n = int(args.debug_segments)
                lo = max(0, total // 2 - n // 2)
                hi = min(total - 1, lo + n - 1)
            lo = max(0, min(lo, total - 1))
            hi = max(lo, min(hi, total - 1))
            print(f"--debug-segments: keeping only original segment(s) {lo}-{hi} "
                  f"of {total} (renumbered from 0 below; the rest are skipped entirely, "
                  f"not just excluded from output -- no motion scoring or cutting cost "
                  f"paid for them).")
            # Whether this debug slice's last kept segment is ALSO the true
            # final segment of the whole movie -- if not, it must NOT be
            # treated as "final" downstream (see the is_final_segment fix
            # below): a real final segment deliberately has no upper time
            # bound (reads to true source EOF), which is correct for the
            # actual end of the movie but would be catastrophic here --
            # ffmpeg would read from wherever this debug slice ends all the
            # way to the TRUE end of the source file, silently doing
            # most/all of the full-file work debug mode exists to avoid.
            debug_reaches_true_end = (hi == total - 1)
            segments = segments[lo:hi + 1]

        # Every gap segment gets the SAME fixed speed, chosen once for the
        # whole file via --speed rather than classified per-segment.
        # Several per-segment classification approaches (visual scene-cut
        # detection, bitrate probing, optical-flow residual, audio volume
        # variability, audio-peak/gradient detection) were tried in turn
        # and each proved unreliable enough in practice to not be worth
        # the complexity -- this is a deliberate simplification, not a
        # placeholder: pick the right overall pace for this file's genre
        # and apply it uniformly.
        gap_indices = [i for i, (s, e, is_gap) in enumerate(segments) if is_gap]
        segment_speed = resolved_max_speed if args.speed == "high" else resolved_min_speed
        total_gap_duration = sum(segments[i][1] - segments[i][0] for i in gap_indices)
        print(f"All {len(gap_indices)} non-dialog segment(s) ({total_gap_duration:.1f}s) -> "
              f"{segment_speed:.4f}x ({args.speed} speed-up, --speed {args.speed}).")
        speed_segments = [
            (s, e, segment_speed if is_gap else resolved_dialog_speed)
            for i, (s, e, is_gap) in enumerate(segments)
        ]

        measured_segments = []

        if resolved_dialog_speed != 1.0:
            print(f"Dialog parts will also be sped up {resolved_dialog_speed:.4f}x (audio pitch preserved via atempo).")

        burn_subs = args.burn_subs
        if burn_subs and not sub_entries:
            print("[!] --burn-subs was requested but no text subtitles are available "
                  "(silence-detection mode has no subtitle text to burn). Ignoring --burn-subs.")
            burn_subs = False
        if burn_subs:
            print("Burning subtitles into each dialog segment at encode time "
                  "(re-timed relative to that segment, so sync is exact).")

        plan = []
        n_segments = len(speed_segments)
        for i, (start, end, speed_factor) in enumerate(speed_segments):
            is_gap = segments[i][2]
            seg_path = os.path.join(tmpdir, f"seg_{i:05d}.mp4")
            burn_path = None
            if burn_subs and not is_gap:
                candidate = os.path.join(tmpdir, f"seg_{i:05d}_burn.srt")
                if build_segment_srt(sub_entries, start, end, candidate, speed_factor=speed_factor):
                    burn_path = candidate
            # `start`/`end` are used as-is: segments are built on the native
            # frame grid (snap_segments_to_frame_grid, above), and cut_segment
            # now enforces the exact [start, end) inclusion itself via the
            # `select`/`atrim` filters on each frame/sample's own absolute
            # timestamp -- no separate margin or cutoff math needed here.
            is_final_segment = (i == n_segments - 1) and debug_reaches_true_end
            plan.append((i, start, end, speed_factor, is_gap, seg_path, burn_path, is_final_segment))

        def cut_one(item):
            i, start, end, speed_factor, is_gap, seg_path, burn_path, is_final_segment = item
            if _interrupted.is_set():
                raise RuntimeError("interrupted")
            # NOTE: the exact [start, end) boundary is now enforced inside
            # cut_segment itself via select/atrim filters gated on each
            # frame/sample's own absolute timestamp -- see cut_segment's
            # docstring/comments. --fast-copy (stream copy, below) can't use
            # that mechanism since -c copy runs no filters at all; it keeps
            # its existing, separately-documented keyframe-precision caveat.
            if burn_path:
                cut_segment(input_path, start, end, seg_path, reencode=True, speed=speed_factor,
                            encoder_mode=encoder_mode, vaapi_device=vaapi_device,
                            audio_stream_index=audio_abs_index, audio_channels=channels_int,
                            burn_srt_path=burn_path, target_fps=target_fps,
                            hw_decode=hw_decode, surround_fix=surround_fix,
                            crf=args.crf, qsv_quality=args.qsv_quality, vaapi_qp=args.vaapi_qp,
                            nvenc_cq=args.nvenc_cq, video_codec=args.video_codec,
                            is_final_segment=is_final_segment, source_fps_frac=source_fps_frac,
                            debug_speed_overlay=args.debug_speed_overlay)
            elif args.fast_copy and speed_factor == 1.0 and not target_fps:
                try:
                    cut_segment(input_path, start, end, seg_path, reencode=False,
                                audio_stream_index=audio_abs_index)
                except RuntimeError:
                    if _interrupted.is_set():
                        raise
                    cut_segment(input_path, start, end, seg_path, reencode=True, speed=speed_factor,
                                encoder_mode=encoder_mode, vaapi_device=vaapi_device, audio_stream_index=audio_abs_index,
                                audio_channels=channels_int,
                                target_fps=target_fps, hw_decode=hw_decode, surround_fix=surround_fix,
                                crf=args.crf, qsv_quality=args.qsv_quality, vaapi_qp=args.vaapi_qp,
                                nvenc_cq=args.nvenc_cq, video_codec=args.video_codec,
                                is_final_segment=is_final_segment, source_fps_frac=source_fps_frac,
                                debug_speed_overlay=args.debug_speed_overlay)
            else:
                cut_segment(input_path, start, end, seg_path, reencode=True, speed=speed_factor,
                            encoder_mode=encoder_mode, vaapi_device=vaapi_device, audio_stream_index=audio_abs_index,
                            audio_channels=channels_int,
                            target_fps=target_fps, hw_decode=hw_decode, surround_fix=surround_fix,
                            crf=args.crf, qsv_quality=args.qsv_quality, vaapi_qp=args.vaapi_qp,
                            nvenc_cq=args.nvenc_cq, video_codec=args.video_codec,
                            is_final_segment=is_final_segment, source_fps_frac=source_fps_frac,
                            debug_speed_overlay=args.debug_speed_overlay)
            return i, get_duration(seg_path)

        try:
            durations = [None] * len(plan)
            total = len(plan)
            completed = 0
            executor = ThreadPoolExecutor(max_workers=max(1, args.jobs))
            futures = {executor.submit(cut_one, item): item for item in plan}
            try:
                for future in as_completed(futures):
                    item = futures[future]
                    i, dur = future.result()
                    durations[i] = dur
                    completed += 1
                    _, start, end, speed_factor, is_gap, _, burn_path, _ = item
                    tag = ("gap" if is_gap else "dialog")
                    if speed_factor != 1.0:
                        tag += f" x{speed_factor:.2f}"
                    if burn_path:
                        tag += " +burned subs"
                    print(f"  segment {i+1}/{total} done ({completed}/{total} complete) ({tag}, {end-start:.2f}s)")
                executor.shutdown(wait=True)
            except KeyboardInterrupt:
                print("\n[!] Interrupted -- stopping in-flight ffmpeg processes...")
                _interrupted.set()
                for f in futures:
                    f.cancel()
                _kill_active_procs()
                executor.shutdown(wait=True)
                raise

            with open(concat_list_path, "w") as listfile:
                for item, dur in zip(plan, durations):
                    _, start, end, speed_factor, is_gap, seg_path, burn_path, _ = item
                    listfile.write(f"file '{seg_path}'\n")
                    measured_segments.append((start, end, dur))

            debug_srt_path = None
            if args.debug_speed_subtitle:
                # Soft subtitle track with one entry per OUTPUT segment,
                # showing its index/kind/speed (and action score, for gap
                # segments) -- an alternative to --debug-speed-overlay that
                # sidesteps burning into the frames entirely (no filter-
                # graph interaction with hardware encoding to worry about,
                # confirmed the overlay approach has -- toggleable in the
                # player, and can carry more detail than a small on-screen
                # corner allows). Built from the SAME plan/durations this
                # concat step already has, walking an output-timeline
                # cursor forward by each segment's actual measured
                # duration -- not from TimeMapper, since that's built
                # FROM this same data and isn't ready yet at this point.
                print("Building debug speed subtitle track...")
                debug_entries = []
                cursor = 0.0
                for item, dur in zip(plan, durations):
                    i, start, end, speed_factor, is_gap, seg_path, burn_path, _ = item
                    kind = "gap" if is_gap else "dialog"
                    text = f"seg {i+1}/{len(plan)}: {kind} x{speed_factor:.2f}"
                    debug_entries.append((cursor, cursor + dur, [text]))
                    cursor += dur
                debug_srt_path = os.path.join(tmpdir, "debug_speed.srt")
                write_srt(debug_srt_path, debug_entries)

            if args.dump_segments is not None:
                # Copies the pre-concat segment files somewhere the person
                # can actually get to (tmpdir gets wiped, and --keep-temp's
                # location is a system temp path, not convenient for
                # side-by-side comparison). Naming encodes index/timerange/
                # speed/gap-vs-dialog so it's clear which segment is which
                # when spot-checking in VLC for whether corruption already
                # exists in an individual segment (encode-side bug) vs. only
                # appears after concatenation (splice-side bug).
                dump_dir = args.dump_segments if args.dump_segments else (
                    os.path.splitext(output_path)[0] + "_segments"
                )
                os.makedirs(dump_dir, exist_ok=True)
                print(f"Copying {len(plan)} segment file(s) to {dump_dir} for inspection...")
                for item in plan:
                    i, start, end, speed_factor, is_gap, seg_path, burn_path, _ = item
                    tag = "gap" if is_gap else "dialog"
                    ext = os.path.splitext(seg_path)[1]
                    dst_name = f"seg_{i:05d}_{tag}_{fmt_hms(start)}-{fmt_hms(end)}_x{speed_factor:.2f}{ext}"
                    shutil.copy2(seg_path, os.path.join(dump_dir, dst_name))
                print(f"  Wrote {len(plan)} segment file(s) to {dump_dir}")

            mapper = TimeMapper(measured_segments) if sub_entries else None

            video_out = output_path
            # final_tmp lives in the SAME directory as the real output path
            # (not tmpdir, which is a different filesystem -- confirmed
            # directly: tmpdir resolves under /tmp, an overlayfs layer
            # inside the container, while output_path is the bind-mounted
            # host media directory, a separate zfs filesystem; os.replace()
            # across different filesystems fails with EXDEV, so the atomic
            # rename at the end of this function only works if the temp
            # file is already on the same filesystem as the destination).
            # Whatever the very last step below produces (concat directly,
            # or the subtitle mux after it) is written here, then moved
            # into place with a single os.replace() -- a same-filesystem
            # rename is atomic, so video_out never exists in a partial
            # state: it's either absent or already the complete file.
            # Without this, a process killed mid-write (container restart,
            # host reboot) mid-way through writing directly to video_out
            # would leave a truncated file there, which main()'s "skip if
            # output exists" check would then mistake for a finished one
            # forever.
            # A leading dot on the FILENAME (not the extension -- e.g.
            # ".movie.mp4", not "movie.part.mp4") hides it from Plex's/
            # Jellyfin's library scanners, which both skip dot-prefixed
            # files by convention. This matters because it genuinely
            # bit us: abridged-movies/abridged-shows are real, live-
            # scanned library roots (confirmed in compose.yml), and a
            # non-hidden temp file sitting there mid-encode -- even named
            # "movie.part.mp4" -- still ends in a recognized media
            # extension, so a scanner can pick it up and try to play it
            # while it's still being written, which looks exactly like a
            # corrupt file (no moov atom yet) even though the real,
            # finished output was never actually broken. The real
            # extension is kept LAST (unlike an earlier ".part.mp4"
            # attempt that put ".part" before it) because ffmpeg can't
            # infer a muxer/container format otherwise -- confirmed
            # directly, it fails outright ("Unable to choose an output
            # format") if the real extension isn't the last thing in the
            # filename.
            _out_dir, _out_name = os.path.split(video_out)
            final_tmp = os.path.join(_out_dir, f".{_out_name}")
            resynced_srt = None
            if sub_entries:
                resynced_srt = os.path.join(tmpdir, "resynced.srt")

            if (sub_entries and args.embed_subs) or args.debug_speed_subtitle:
                # Falls back to .mp4 if output_path somehow has no
                # extension -- confirmed directly: ffmpeg can't infer a
                # muxer for an extensionless path ("Unable to choose an
                # output format"), which is exactly what happened when
                # output_path was a bare directory path (now handled
                # upstream in main(), but this is real defense-in-depth
                # against the same class of failure recurring).
                #
                # Also covers the debug-only-track case: without this,
                # target_video would equal final_tmp and the later mux
                # step would try to read and write the SAME file in one
                # ffmpeg invocation, which doesn't work reliably.
                out_ext = os.path.splitext(output_path)[1] or ".mp4"
                video_tmp = os.path.join(tmpdir, "video_only" + out_ext)
                target_video = video_tmp
            else:
                target_video = final_tmp

            # Stream-copy concat (fast, lossless -- no second encode
            # generation). Correct in both CONTENT (segment boundaries are
            # exact -- see cut_segment) and RATE (speeds are validated
            # up front to be exact small-integer frame-rate ratios, so
            # every segment already lands exactly on the target rate by
            # construction -- there's no per-segment rounding left to
            # accumulate across segments, unlike the earlier generic
            # -r/-fps_mode cfr approach this replaced).
            print("Concatenating segments...")
            cmd = ["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
                   "-c", "copy", target_video]
            r = run(cmd)
            if r.returncode != 0:
                print("Stream-copy concat failed, re-encoding on concat (slower)...")
                concat_mode = encoder_mode
                global_args, vf, codec_args = video_encode_args(concat_mode, speed=1.0, vaapi_device=vaapi_device,
                                                                  crf=args.crf, qsv_quality=args.qsv_quality, vaapi_qp=args.vaapi_qp,
                                                                  nvenc_cq=args.nvenc_cq, video_codec=args.video_codec)
                cmd = ["ffmpeg", "-nostdin", "-y"] + global_args + ["-f", "concat", "-safe", "0", "-i", concat_list_path]
                if vf:
                    cmd += ["-vf", vf]
                cmd += codec_args + ["-c:a", "aac", "-b:a", audio_bitrate_for_channels(channels_int), target_video]
                r = run(cmd)
                while r.returncode != 0 and concat_mode != "cpu":
                    concat_mode = _fallback_encoder_mode(concat_mode, args.video_codec)
                    print(f"  [!] Concat re-encode failed, retrying with {concat_mode}...")
                    global_args, vf, codec_args = video_encode_args(concat_mode, speed=1.0, vaapi_device=vaapi_device,
                                                                      crf=args.crf, qsv_quality=args.qsv_quality, vaapi_qp=args.vaapi_qp,
                                                                      nvenc_cq=args.nvenc_cq, video_codec=args.video_codec)
                    cmd = ["ffmpeg", "-nostdin", "-y"] + global_args + ["-f", "concat", "-safe", "0", "-i", concat_list_path]
                    if vf:
                        cmd += ["-vf", vf]
                    cmd += codec_args + ["-c:a", "aac", "-b:a", audio_bitrate_for_channels(channels_int), target_video]
                    r = run(cmd)
                if r.returncode != 0:
                    raise RuntimeError(f"Concat failed:\n{r.stderr[-3000:]}")

            if sub_entries:
                print("Remapping subtitle timestamps to new timeline...")
                new_entries = []
                for (s, e, text_lines) in sub_entries:
                    new_entries.append((mapper.map(s), mapper.map(e), text_lines))
                write_srt(resynced_srt, new_entries)

            # Collects (srt_path, language, title) for every subtitle
            # track to embed -- the real resynced subs (if --embed-subs),
            # the debug speed track (if --debug-speed-subtitle),
            # independently of each other -- and muxes however many of
            # them there are (0, 1, or 2) in ONE pass, rather than
            # special-casing each combination separately.
            tracks_to_embed = []
            if sub_entries and args.embed_subs:
                tracks_to_embed.append((resynced_srt, sub_lang or "und", None))
            if debug_srt_path:
                tracks_to_embed.append((debug_srt_path, "und", "debug speed"))
            if tracks_to_embed:
                print(f"Muxing {len(tracks_to_embed)} subtitle track(s) into output container...")
                ext = os.path.splitext(video_out)[1].lower()
                sub_codec = "mov_text" if ext in (".mp4", ".mov", ".m4v") else "srt"
                cmd = ["ffmpeg", "-nostdin", "-y", "-i", target_video]
                for srt_path, _, _ in tracks_to_embed:
                    cmd += ["-i", srt_path]
                cmd += ["-map", "0:v", "-map", "0:a"]
                for n in range(len(tracks_to_embed)):
                    cmd += ["-map", f"{n+1}:0"]
                cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", sub_codec]
                for n, (_, lang, title) in enumerate(tracks_to_embed):
                    cmd += [f"-metadata:s:s:{n}", f"language={lang}"]
                    if title:
                        cmd += [f"-metadata:s:s:{n}", f"title={title}"]
                cmd += [final_tmp]
                r = run(cmd)
                if r.returncode != 0:
                    raise RuntimeError(f"Muxing subtitles failed:\n{r.stderr[-3000:]}")

            os.replace(final_tmp, video_out)
            print(f"Done. Output video: {video_out}")
            if burn_subs:
                print("Subtitles were burned into the video frames (hardcoded, always visible).")
            if sub_entries and args.embed_subs:
                print("Resynced subtitles were embedded into the output container.")
            if debug_srt_path:
                print("Debug speed subtitle track was embedded into the output container.")

            orig_dialog = sum(e - s for (s, e, is_gap) in segments if not is_gap)
            orig_gap = sum(e - s for (s, e, is_gap) in segments if is_gap)
            new_dialog = sum(dur for (s, e, dur), (_, _, is_gap) in zip(measured_segments, segments) if not is_gap)
            new_gap = sum(dur for (s, e, dur), (_, _, is_gap) in zip(measured_segments, segments) if is_gap)
            try:
                final_total = get_duration(video_out)
            except Exception:
                final_total = new_dialog + new_gap
            orig_total = orig_dialog + orig_gap

            def fmt(secs):
                m, s = divmod(secs, 60)
                return f"{int(m)}m{s:04.1f}s" if m else f"{s:.1f}s"

            print("")
            print("=== Summary ===")
            if orig_dialog > 0:
                print(f"Dialog:     {fmt(orig_dialog)} -> {fmt(new_dialog)}  (saved {fmt(max(0, orig_dialog - new_dialog))})")
            if orig_gap > 0:
                print(f"Non-dialog: {fmt(orig_gap)} -> {fmt(new_gap)}  (saved {fmt(max(0, orig_gap - new_gap))})")
            pct = (1 - final_total / orig_total) * 100 if orig_total > 0 else 0
            print(f"Total:      {fmt(orig_total)} -> {fmt(final_total)}  (saved {fmt(max(0, orig_total - final_total))}, {pct:.1f}% shorter)")

            return True

        finally:
            if not args.keep_temp:
                shutil.rmtree(tmpdir, ignore_errors=True)
            else:
                print(f"Temp files kept at: {tmpdir}")

    except Exception as e:
        print(f"[!] Failed to process {input_path}: {e}")
        return False


def warn_if_mkv_unsafe(target_fps, is_mkv_output):
    """ffmpeg's mkv muxer only has 1ms timestamp precision. A target rate
    whose frame period doesn't divide evenly into that (the classic
    example: 59.94fps, 16.683ms/frame) causes real, verified audio/video
    drift over a long file. Warns (doesn't block) since the person may
    have deliberately chosen the rate."""
    if not is_mkv_output or target_fps <= 0:
        return
    frame_ms = 1000.0 / target_fps
    if abs(frame_ms - round(frame_ms)) >= 1e-6:
        print(f"[!] Warning: {target_fps:.3f}fps doesn't divide evenly into .mkv's 1ms timestamp "
              f"precision -- this WILL cause measurable audio/video drift over a long file (verified). "
              f"Consider --fps 50 (exactly 20ms/frame, mkv-safe) or .mp4 output instead.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Input video file, or a directory for batch mode (processes every .mkv file in it)")
    ap.add_argument("output", help="Output video file, or a directory for batch mode (matches input)")
    ap.add_argument("--extract-raw", default=None, metavar="START:DURATION",
                     help="Diagnostic/support mode: extract a small, LOSSLESS (stream-copy, no re-encoding "
                          "or filtering at all) clip from INPUT covering START to START+DURATION seconds, "
                          "write it to OUTPUT, and exit immediately -- none of the rest of the pipeline runs. "
                          "For pulling a small untouched sample out of a large source file (e.g. 'give me the "
                          "raw few seconds around 6:43' out of a 6GB file) without constructing an ffmpeg "
                          "command by hand or transferring the whole file. Example: --extract-raw 400:5")
    ap.add_argument("--speed", choices=["high", "low", "dialog"], default="high",
                     help="Single fixed speed-up applied to ALL non-dialog (gap) segments across the whole "
                          "file -- 'high' (default) uses the more aggressive 2:1 rate, 'low' uses the "
                          "gentler 1:1 rate. No per-segment action/calm classification -- several "
                          "approaches (visual scene-cut detection, bitrate probing, optical-flow residual, "
                          "audio volume variability, audio-peak/gradient detection) were tried in turn and "
                          "each proved unreliable enough in practice to not be worth the complexity. Pick "
                          "based on the file's own genre/pace -- 'low' for something genuinely action-heavy "
                          "throughout, 'high' otherwise. The actual multipliers are always derived from the "
                          "source's real frame rate (see the startup 'Target fps' line), not separately "
                          "configurable. Dialog is always kept at exactly 1:2 (a mild, fixed dialog speed "
                          "that stays intelligible, audio pitch preserved via atempo) regardless of this "
                          "flag -- see the 'Dialog parts will also be sped up...' startup line. "
                          "'dialog' is a different, whole-file MODE, not a gap setting: the ENTIRE file "
                          "(not just gap segments) plays at that same dialog pace, uniformly -- since that "
                          "speed is by definition exactly (target_fps/source_fps)/2, this is always a "
                          "straight 1:1 frame passthrough to a new fixed rate of exactly 30000/1001, "
                          "regardless of the source's native fps -- no subtitle-based dialog/gap detection, "
                          "no segmentation, no duplicate/decimate math, and --debug-speed-overlay/"
                          "--debug-speed-subtitle aren't supported in this mode (nothing to usefully show "
                          "with only one speed).")
    ap.add_argument("--sub-track", type=int, default=None, help="Index (as shown by --list-subs) of an embedded subtitle track in the input to use, when not passing --subtitles")
    ap.add_argument("--list-subs", action="store_true", help="List embedded subtitle tracks in the input and exit (single-file mode only)")
    ap.add_argument("--pad", type=float, default=0.5, help="Padding (sec) added around each subtitle so words aren't clipped (default 0.5)")
    ap.add_argument("--min-nondialog-duration", type=float, default=2.0,
                     help="Minimum duration (sec) for a non-dialog segment to remain its own segment; "
                          "anything shorter gets merged into surrounding dialog instead. Default 2.0; "
                          "pass 0 to disable.")
    ap.add_argument("--no-cross-lang-dialog-detection", action="store_true",
                     help="By default, one other subtitle language in the file (besides the one "
                          "chosen for --lang, and limited to English/Spanish/French/German/Italian/"
                          "Portuguese -- see CROSS_LANG_OCR_LANGS) is also extracted (OCR'd if it's "
                          "a bitmap-only track) and SDH-filtered, purely to widen dialog-range "
                          "detection -- never for the embedded output subtitle text, which always "
                          "comes only from the chosen "
                          "language's own track. This recovers on-screen text (title cards, signage) "
                          "that the chosen language's own track correctly doesn't translate (nothing "
                          "to translate for a reader who can already read it on screen) but another "
                          "language's track has to -- confirmed directly against a real title that "
                          "this is exactly why such gaps exist, not a captioning oversight. Pass this "
                          "to skip it entirely.")
    ap.add_argument("--embed-subs", action="store_true", help="Mux the resynced subtitles into the output video as a soft subtitle track")
    ap.add_argument("--burn-subs", action="store_true", help="Burn subtitles directly into the video frames during each segment's own encode pass -- most accurate sync option, since there's no separate timestamp-remap step")
    ap.add_argument("--audio-track", type=int, default=None, help="Index (as shown by --list-audio) of the audio track to use, if the input has more than one")
    ap.add_argument("--list-audio", action="store_true", help="List audio tracks in the input and exit (single-file mode only)")
    ap.add_argument("--lang", default=None, help="Language code (e.g. 'eng', 'spa', matching the track's language tag) to auto-select the audio and/or subtitle track, when exactly one track of that language exists -- skips the interactive prompt/error that would otherwise happen with multiple tracks. Explicit --audio-track/--sub-track still take priority if given. Especially useful in batch mode, where track indices may not be consistent across files but language tags usually are.")
    ap.add_argument("--fast-copy", action="store_true", help="Stream-copy dialog segments instead of re-encoding them (faster/lighter, but cut points can drift to the nearest source keyframe; ignored for segments using --burn-subs, which always re-encodes)")
    ap.add_argument("--video-codec", choices=["h264", "hevc"], default="h264",
                     help="Output video codec (default h264). Orthogonal to --encoder: this picks WHAT to "
                          "encode to, --encoder picks WHO does it (Intel/AMD/NVIDIA/CPU) -- e.g. "
                          "--video-codec hevc --encoder vaapi encodes hevc_vaapi. Every vendor's hardware "
                          "fallback chain (see --encoder) keeps the codec fixed and only changes vendor -- "
                          "a hevc request never silently downgrades to h264 output.")
    ap.add_argument("--encoder", choices=["auto", "qsv", "vaapi", "nvenc", "cpu"], default="auto",
                     help="Video encoder VENDOR: 'qsv' for Intel Quick Sync, 'vaapi' for Intel VAAPI "
                          "(often works when QSV doesn't, e.g. missing oneVPL/libmfx runtime) -- also the "
                          "path AMD GPUs use, since VAAPI is vendor-neutral (Mesa's radeonsi driver backs "
                          "the same /dev/dri device on AMD, no separate 'amd' setting exists or is needed), "
                          "'nvenc' for NVIDIA (requires the NVIDIA Container Toolkit on the host -- see the "
                          "compose file), 'cpu' for software libx264/libx265, 'auto' (default) tries qsv, "
                          "then vaapi, then nvenc, then cpu, falling back per-segment at runtime if a "
                          "hardware encoder fails (see --video-codec for why the chosen codec never changes "
                          "mid-fallback, only the vendor does)")
    ap.add_argument("--vaapi-device", default="/dev/dri/renderD128", help="VAAPI render device path (default /dev/dri/renderD128; check `ls /dev/dri` if you have multiple GPUs)")
    ap.add_argument("--jobs", type=int, default=4, help="Number of segments to cut/encode in parallel within a single file (default 4). Each segment's decode+filter stage is CPU-bound and independent of the others, so this parallelizes well across cores; hardware encode (QSV/VAAPI) also handles a queue of concurrent requests fine since Intel doesn't cap concurrent sessions the way consumer GPUs traditionally have. Lower it (e.g. 1-2) with --encoder nvenc if you hit a concurrent-session limit, or with --encoder cpu on a low core count.")
    ap.add_argument("--no-hw-decode", action="store_true", help="Disable hardware-accelerated decode. By default, whenever QSV/VAAPI/NVENC encoding is used, decode also runs on the same device so frames never leave the GPU (except around --burn-subs, which needs software frames for subtitle rendering); a decode failure at runtime falls back the same way an encode failure does (qsv -> vaapi -> cpu; nvenc -> cpu). Pass this to force software decode instead.")

    ap.add_argument("--crf", type=int, default=20,
                     help="[CPU: libx264/libx265, see --video-codec] CRF used for every re-encode -- "
                          "per-segment cuts and the concat fallback re-encode alike (default 20). Lower = "
                          "higher quality/larger file.")
    ap.add_argument("--qsv-quality", type=int, default=23,
                     help="[QSV] global_quality used for every re-encode (default 23, roughly comparable "
                          "scale to --crf). Lower = higher quality.")
    ap.add_argument("--vaapi-qp", type=int, default=23,
                     help="[VAAPI, Intel or AMD] qp used for every re-encode (default 23, roughly comparable "
                          "scale to --crf). Lower = higher quality.")
    ap.add_argument("--nvenc-cq", type=int, default=23,
                     help="[NVENC] constant-quality target (-rc vbr -cq) used for every re-encode (default "
                          "23, roughly comparable scale to --crf). Lower = higher quality.")
    ap.add_argument("--dump-segments", nargs="?", const="", default=None, metavar="DIR",
                     help="Copy each pre-concat segment file to DIR (created if needed) for standalone "
                          "inspection -- e.g. to check whether a glitch is already present in an individual "
                          "segment (encode-side bug) or only shows up after concatenation (splice-side bug). "
                          "If DIR is omitted, defaults to '<output>_segments' next to the output file. "
                          "Independent of --keep-temp (which keeps the FULL system temp working dir, including "
                          "subtitle intermediates and the concat list, not just the segment files).")
    ap.add_argument("--debug-speed-overlay", action="store_true",
                     help="Burn the assigned speed multiplier (e.g. '5.00x') into the top-left corner of "
                          "every segment, for visually confirming while watching the output which segments "
                          "got classified which way -- e.g. spotting a pan that's incorrectly running at "
                          "5x instead of 2.5x. Debug/tuning aid, not meant for a final deliverable -- burns "
                          "into the actual frames (not a removable soft overlay).")
    ap.add_argument("--debug-speed-subtitle", action="store_true",
                     help="Adds a soft (toggleable) subtitle track with one entry per output segment, "
                          "showing its index, gap/dialog kind, speed multiplier, and (for gap segments) "
                          "its action-classification score. An alternative to --debug-speed-overlay that "
                          "doesn't burn into the frames -- no filter-graph interaction with hardware "
                          "encoding to worry about, can be turned on/off in the player, and can show more "
                          "detail than a small on-screen corner allows. Can be combined with --debug-speed-"
                          "overlay, or with real embedded subtitles (--embed-subs) -- shows up as a "
                          "separate track either way.")
    ap.add_argument("--debug-segments", default=None, metavar="N|LO-HI",
                     help="Process only a small slice of the full segment list, for fast "
                          "iteration when testing/debugging (e.g. comparing --encoder cpu vs "
                          "auto on one problem spot) without paying for a full-file run every "
                          "time. Pass a plain count (e.g. '10') to grab that many segments from "
                          "the MIDDLE of the movie, or an explicit inclusive range of original "
                          "0-indexed segment numbers (e.g. '10-20', matching the indices you'd "
                          "see printed during a normal run or in --dump-segments filenames). "
                          "Segments outside the kept range are skipped entirely -- no motion "
                          "scoring or cutting cost paid for them, not just excluded from the "
                          "final output. Kept segments are renumbered from 0 in the output.")
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg/ffprobe not found on PATH.")
    if not os.path.exists(args.input):
        sys.exit(f"Input not found: {args.input}")

    if args.extract_raw:
        # Standalone diagnostic mode -- runs and exits before anything else,
        # deliberately independent of every other flag/pipeline stage.
        try:
            start_str, dur_str = args.extract_raw.split(":", 1)
            start, dur = float(start_str), float(dur_str)
        except ValueError:
            sys.exit(f"--extract-raw expects START:DURATION (e.g. 400:5), got {args.extract_raw!r}")
        cmd = ["ffmpeg", "-nostdin", "-y", "-ss", str(start), "-t", str(dur),
               "-i", args.input, "-c", "copy", args.output]
        print(f"Extracting {dur}s from {start}s (lossless stream copy, no filtering)...")
        r = run(cmd)
        if r.returncode != 0:
            sys.exit(f"Extraction failed:\n{r.stderr[-2000:]}")
        print(f"Wrote {args.output}")
        return

    cleanup_stale_tempdirs()

    batch_mode = os.path.isdir(args.input)

    if batch_mode:
        if args.list_subs or args.list_audio:
            sys.exit("--list-subs/--list-audio need a single input file, not a directory. "
                      "Run them against one file from the folder first.")
        if os.path.exists(args.output) and not os.path.isdir(args.output):
            sys.exit(f"{args.output} exists and is not a directory (input is a directory, so output must be too).")
        os.makedirs(args.output, exist_ok=True)

        # Always recursive -- every .mkv anywhere under the input folder,
        # subfolders included, gets processed; there's no longer a
        # --recursive flag to opt in/out of this.
        input_files = sorted(
            p for p in Path(args.input).glob("**/*.mkv") if p.suffix.lower() == ".mkv"
        )
        if not input_files:
            sys.exit(f"No .mkv files found in {args.input} (recursively). "
                      "(Only .mkv inputs are supported in batch mode.)")

        print(f"Batch mode: found {len(input_files)} .mkv file(s) in {args.input} (including subfolders)")
        encoder_mode = resolve_encoder(args.encoder, args.video_codec)
        vaapi_device = args.vaapi_device

        results = []
        for i, in_path in enumerate(input_files, 1):
            # Mirrors the input file's subfolder structure (relative to
            # the input root) under the output folder, rather than
            # flattening everything into one directory -- e.g.
            # <input>/Season 1/ep1.mkv -> <output>/Season 1/ep1-....mp4.
            rel_dir = in_path.parent.relative_to(args.input)
            out_dir = os.path.join(args.output, rel_dir) if str(rel_dir) != "." else args.output
            os.makedirs(out_dir, exist_ok=True)
            print("")
            print(f"##### [{i}/{len(input_files)}] {in_path.name} #####")
            try:
                out_path = build_abridged_output_path(str(in_path), out_dir, args.speed)
            except Exception as e:
                print(f"  [!] Couldn't probe {in_path.name} to build output filename, skipping: {e}")
                results.append((in_path.name, False, False))
                continue
            if os.path.exists(out_path):
                print(f"  Output already exists at {out_path} -- skipping.")
                results.append((in_path.name, True, True))
                continue
            fn = process_one_dialog_mode if args.speed == "dialog" else process_one
            ok = fn(str(in_path), out_path, args, encoder_mode, vaapi_device)
            results.append((in_path.name, ok, False))

        print("")
        print("=== Batch complete ===")
        for name, ok, skipped in results:
            status = "SKIP" if skipped else ("OK  " if ok else "FAIL")
            print(f"  {status}  {name}")
        failed = sum(1 for _, ok, skipped in results if not ok and not skipped)
        if failed:
            print(f"{failed}/{len(results)} file(s) failed -- see errors above.")
        return

    if args.list_subs:
        tracks = list_subtitle_tracks(args.input)
        if not tracks:
            print("No subtitle tracks found in the input.")
        else:
            print(f"Subtitle tracks in {args.input}:")
            for i, t in enumerate(tracks):
                print(describe_track(i, t))
        return

    if args.list_audio:
        tracks = list_audio_tracks(args.input)
        if not tracks:
            print("No audio tracks found in the input.")
        else:
            print(f"Audio tracks in {args.input}:")
            for i, t in enumerate(tracks):
                print(describe_audio_track(i, t))
        return

    encoder_mode = resolve_encoder(args.encoder, args.video_codec)
    vaapi_device = args.vaapi_device

    # Output is always treated as a destination FOLDER, never a specific
    # filename -- created if it doesn't exist yet. The filename itself is
    # always derived: "<stem>-ABRIDGED<X>X.mp4", X being the actual
    # resolved speed (see build_abridged_output_path/--speed).
    if os.path.exists(args.output) and not os.path.isdir(args.output):
        sys.exit(f"{args.output} exists and is not a directory -- output is always a destination folder now, "
                  f"not a specific filename.")
    os.makedirs(args.output, exist_ok=True)
    output_path = build_abridged_output_path(args.input, args.output, args.speed)
    print(f"Output: {output_path}")
    if os.path.exists(output_path):
        print(f"Output already exists at {output_path} -- skipping (matches batch mode's behavior; "
              f"delete it first if you want to re-process).")
        return

    fn = process_one_dialog_mode if args.speed == "dialog" else process_one
    fn(args.input, output_path, args, encoder_mode, vaapi_device)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)