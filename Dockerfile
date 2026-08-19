FROM ubuntu:26.04

# ffmpeg/ffprobe: the core dependency, everything runs through it.
# tesseract-ocr-eng/spa/deu/fra/ita/por: OCR support for image-based
#   (PGS/Blu-ray) subtitle tracks, via pgsrip + Tesseract (confirmed
#   working via `python3 -m pgsrip` -- see abridge.py's ocr_pgs_subtitle
#   for why it's invoked that way rather than a bare `pgsrip` command).
#   Two distinct uses share this same language set: OCRing the MAIN
#   subtitle track itself (target language comes dynamically from
#   Radarr/Sonarr's originalLanguage, per title -- see watch.py's
#   resolve_arr_overrides), and OCRing ONE other-language track for
#   cross-language dialog detection (gather_cross_lang_dialog_ranges in
#   abridge.py, whose CROSS_LANG_OCR_LANGS constant must match this list),
#   which recovers on-screen text (signage, billboards, phone screens,
#   title cards) that the main track correctly doesn't translate but a
#   foreign-language track has to. Previously installed via the
#   tesseract-ocr-all meta-package (all ~125 script/language packs,
#   ~667MB of tessdata, confirmed via `du -sh /usr/share/tesseract-ocr/`)
#   so no language was ever missed, but that meant OCRing every foreign-
#   language track in a file, however many there were -- too much
#   processing time. Capped to these 6 instead: whichever is this title's
#   actual main language gets used for the primary subtitle OCR, and
#   gather_cross_lang_dialog_ranges -- already best-effort per language (a
#   track that fails to extract/OCR is just skipped, see its docstring)
#   -- tries the remaining ones from this list until one works, instead of
#   every language present in the file. A title whose main language isn't
#   one of these 6 still gets normal (non-OCR) subtitle handling if its
#   track is text-based; only bitmap/PGS tracks in a language outside this
#   set lose OCR coverage entirely. Tesseract's package names use ISO
#   639-2's TERMINOLOGY codes, while ffprobe/MKV report the BIBLIOGRAPHIC
#   codes -- confirmed directly they differ for French and German
#   specifically (package tesseract-ocr-fra/tesseract-ocr-deu, vs. the
#   fre/ger tags you'll actually see in this project's own --list-subs
#   output); Italian and Portuguese don't have this split (ita/por are
#   identical under both systems). No regional variants exist (e.g. no
#   separate Spain-vs-Latin-America spa, or Portugal-vs-Brazil por,
#   package) -- Tesseract OCR only cares about script/character set, not
#   dialect.
# python3-pip: for installing pgsrip itself.
# libgl1 + libglib2.0-0t64: pgsrip depends on full opencv-python (not the
#   -headless variant), which needs OpenGL/glib shared libraries at
#   import time -- NOT present on a minimal Ubuntu base by default, and
#   a very commonly-reported Docker gotcha for opencv-python specifically
#   ("ImportError: libGL.so.1: cannot open shared object file"). Included
#   here proactively rather than risking it on first build. Package is
#   libglib2.0-0t64 (not the older libglib2.0-0 name) -- confirmed
#   directly that the un-suffixed transitional package Ubuntu 24.04 still
#   carried for the 64-bit time_t ABI migration is gone entirely on
#   26.04, apt just reports "Candidate: (none)" for it now.
# tzdata + gosu: support running as an arbitrary PUID/PGID/TZ the same way
#   linuxserver.io images do (see entrypoint.sh) -- tzdata for the zoneinfo
#   database TZ maps into, gosu to drop from root to the runtime user right
#   before launching watch.py.
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        intel-media-va-driver-non-free \
        vainfo \
        ffmpeg \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-spa \
        tesseract-ocr-deu \
        tesseract-ocr-fra \
        tesseract-ocr-ita \
        tesseract-ocr-por \
        python3 \
        python3-pip \
        libgl1 \
        libglib2.0-0t64 \
        libvpl2 \
        libmfx-gen1.2 \
        tzdata \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu's base images (still true as of 26.04) ship a built-in "ubuntu" user at uid/gid
# 1000 -- exactly the PUID/PGID this stack's containers conventionally use
# -- which would collide with a freshly-created service user pinned to the
# same id. Removed rather than reused so entrypoint.sh's usermod/groupmod
# always has a clean, dedicated "abc" account to retarget at whatever
# PUID/PGID is actually requested at runtime.
RUN userdel -r ubuntu 2>/dev/null || true \
    && groupdel ubuntu 2>/dev/null || true \
    && groupadd abc \
    && useradd -M -s /usr/sbin/nologin -g abc abc

# --break-system-packages: this Ubuntu's python3-pip refuses a plain
# system-wide install otherwise (an "externally managed environment"
# safety check) -- confirmed necessary in this exact environment during
# development of abridge.py's OCR support.
# watchdog: inotify-based filesystem event watching for watch.py -- see
#   its module docstring for why events are a trigger to re-scan sooner,
#   not a substitute for the stability check that actually decides when
#   a file is safe to process.
RUN pip install --break-system-packages --no-cache-dir pgsrip watchdog

WORKDIR /app
COPY app/abridge.py app/watch.py /app/

# /config/config.json (your input/output pairs and settings) is mounted
# as a single read-only file at runtime. watch.py keeps no local
# persistence at all -- failures are tagged directly on the Radarr/Sonarr
# entry (abridgerr-failed) instead of a state file, so there's nothing
# here that needs to survive a restart.
RUN mkdir -p /config && chown abc:abc /config

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "/app/watch.py"]