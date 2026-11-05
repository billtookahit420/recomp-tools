#!/usr/bin/env python3
"""Recomp Tools - conversion engine.

The pipeline that turns a PS1 disc image (.cue + .bin tracks) into a
packaged native PC recomp, following the house rules:

  scaffold -> probe -> patch CMake -> wire RetroAchievements -> build
  emitters -> generate -> build Linux/Windows -> verify -> package

This module is UI-free: the Qt app (main.py) drives it through a log
callback, and it can also run headless:

  python3 engine.py probe  --cue GAME.cue [--template DIR]
  python3 engine.py run    --job job.json
  python3 engine.py toolbox

A job JSON matches JobSettings + {"cues": [...], "name": "CamelName",
"title": "Display Title", "work_dir": "/path/to/projects"}.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BUNDLE_RA = next((p for p in (APP_DIR / "bundle" / "ra",
                              APP_DIR.parent / "bundle" / "ra")
                  if p.is_dir()), APP_DIR / "bundle" / "ra")

# ---------------------------------------------------------------- toolbox

TOOLS = {
    "python3": "Python 3",
    "cmake": "CMake",
    "ninja": "Ninja",
    "gcc": "GCC (Linux builds)",
    "g++": "G++ (Linux builds)",
    "x86_64-w64-mingw32-gcc": "MinGW-w64 GCC (Windows builds)",
    "nm": "nm (verification)",
    "x86_64-w64-mingw32-nm": "MinGW nm (verification)",
    "zip": "zip (packaging)",
}


def toolbox_check():
    """Return {tool: path-or-None} for every tool the pipeline needs."""
    return {t: shutil.which(t) for t in TOOLS}


# System packages the Linux first-run setup installs, in one pkexec
# password prompt. Single source of truth: the Help tab describes this
# list and the toolbox Install button runs it. libvulkan-dev + glslc
# are the Vulkan SDK pieces the framework needs at build time to
# compile the REAL Vulkan renderer (glslang-tools alone does NOT
# provide glslc - learned on the build VM, 2026-11-01).
LINUX_SYSTEM_PACKAGES = [
    "build-essential", "cmake", "ninja-build", "zip", "binutils",
    "gcc-mingw-w64-x86-64", "g++-mingw-w64-x86-64", "libgl-dev",
    "libssl-dev", "git", "python3-venv", "libfreetype-dev",
    "libharfbuzz-dev", "libxtst-dev", "libasound2-dev",
    "libpulse-dev", "libudev-dev", "libxext-dev", "libxrandr-dev",
    "libxcursor-dev", "libxi-dev", "libxss-dev", "libxkbcommon-dev",
    "libdrm-dev", "libgbm-dev", "libegl-dev", "libdbus-1-dev",
    "libvulkan-dev", "glslang-tools", "glslc",
]


def system_install_command():
    """One pkexec prompt installs every system package (Linux)."""
    pkgs = " ".join(LINUX_SYSTEM_PACKAGES)
    return ["pkexec", "sh", "-c",
            f"apt-get update -qq && apt-get install -y {pkgs}"]


FRAMEWORK_URLS = {
    "psxrecomp": "https://github.com/mstan/psxrecomp.git",
    "recomp-ui": "https://github.com/RetroPortingToolKit/recomp-ui.git",
}


def framework_cache():
    """App-managed framework download, used on machines with no finished
    recomp project to borrow tools from (a fresh install)."""
    return Path(__file__).resolve().parent.parent / "framework-cache"


def _cache_ok(p):
    return ((p / "psxrecomp" / "tools" / "new_project_layout").is_dir()
            and (p / "recomp-ui").is_dir())


def ensure_framework_cache(log):
    """Download the framework once into framework-cache/ and return it
    as a template. Returns None when git is missing or a clone fails."""
    cache = framework_cache()
    if _cache_ok(cache):
        return cache
    if not shutil.which("git"):
        log("framework download needs git, which is not installed")
        return None
    cache.mkdir(parents=True, exist_ok=True)
    for name, url in FRAMEWORK_URLS.items():
        dst = cache / name
        if dst.is_dir() and any(dst.iterdir()):
            continue
        log(f"downloading framework: {url}")
        res = subprocess.run(["git", "clone", "--depth", "1", url,
                              str(dst)], capture_output=True, text=True)
        if res.returncode != 0:
            log("clone failed: " + (res.stderr or "")[-300:])
            return None
    return cache if _cache_ok(cache) else None


def find_template(override=""):
    """A template project is any finished recomp whose psxrecomp/ tree we
    can copy when the framework clone fails (it hangs often)."""
    if override and (Path(override) / "psxrecomp").is_dir():
        return Path(override)
    if _cache_ok(framework_cache()):
        return framework_cache()
    ws = Path.home() / "workspace"
    cands = []
    for d in sorted((p for p in ws.glob("*-recomp/*") if p.is_dir()),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        if (d / "psxrecomp" / "tools" / "new_project_layout").is_dir():
            cands.append(d)
    return cands[0] if cands else None


# ---------------------------------------------------------------- helpers

class StageError(Exception):
    pass


class Runner:
    def __init__(self, log):
        self.log = log

    def run(self, cmd, cwd=None, timeout=None, env=None):
        cmd = [str(c) for c in cmd]
        self.log("$ " + " ".join(cmd) + (f"   [cwd: {cwd}]" if cwd else ""))
        e = dict(os.environ)
        if env:
            e.update(env)
        try:
            p = subprocess.Popen(cmd, cwd=cwd, env=e,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 errors="replace", bufsize=1)
        except FileNotFoundError as ex:
            raise StageError(f"tool not found: {cmd[0]} ({ex})")
        try:
            for line in p.stdout:
                self.log(line.rstrip("\n"))
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            raise StageError(f"timed out after {timeout}s: {cmd[0]}")
        self.log(f"-> exit {rc}")
        return rc

    def run_ok(self, cmd, cwd=None, timeout=None, env=None):
        rc = self.run(cmd, cwd=cwd, timeout=timeout, env=env)
        if rc != 0:
            raise StageError(f"command failed ({rc}): {' '.join(map(str, cmd))}")
        return rc


def compact_name(title):
    return re.sub(r"[^A-Za-z0-9]", "", title)


def cue_title(cue):
    """'South Park Rally (USA).cue' -> 'South Park Rally'.

    Works on bare folder names too ('Final Fantasy IX (USA) (Disc 1)
    (v1.1)' -> 'Final Fantasy IX'): Path.stem cannot be used here - it
    cuts '(v1.1)' at its inner dot, leaving '(v1' glued to the title,
    which broke set grouping for versioned redump folder names."""
    stem = Path(cue).name
    if stem.lower().endswith((".cue", ".bin", ".iso", ".img")):
        stem = stem.rsplit(".", 1)[0]
    stem = re.sub(r"\s*\((USA|EUR|JPN|Disc \d[^)]*|v[\d.]+|Rev[^)]*)\)", "",
                  stem).strip()
    stem = re.sub(r"\s+", " ", stem)
    return stem or "Game"


# ---------------------------------------------------------------- settings

@dataclass
class JobSettings:
    players: int = 0                 # 0 = use probe/setup default
    launcher_ui: bool = True
    ra_enabled: bool = True
    ra_popups: bool = True           # False -> RA_DISABLE_POPUPS build
    ra_toml_path: str = ""           # filled toml to ship ("" = example only)
    renderer: str = "opengl"
    aspect_ratio: str = "4:3"
    controller_mode: str = "digital"
    build_linux: bool = True
    build_windows: bool = True
    jobs: int = 2
    save_states: bool = False        # False -> ship config.ini unbinding F7/F8
    bios_path: str = ""              # test runs only; never packaged
    template_dir: str = ""


@dataclass
class Job:
    cues: list
    name: str                        # CamelCase project/binary name
    title: str                       # display title
    work_dir: str                    # parent dir; project root = work_dir/<slug>-recomp
    settings: JobSettings = field(default_factory=JobSettings)
    slug_override: str = ""
    art: str = ""                    # cover art file to bundle (GUI-fetched)
    probes: list = field(default_factory=list)   # probe JSON dicts, per cue
    set_verdict: dict = field(default_factory=dict)
    vulkan_backend: dict = field(default_factory=dict)
    # target ("linux"/"windows") -> True when that target's build tree
    # proved a real Vulkan backend (vk_shaders_spv.h). Filled by
    # stage_build, read by stage_package: the package offers Vulkan
    # for exactly the targets that truly contain it.

    @property
    def slug(self):
        if self.slug_override:
            return self.slug_override
        return re.sub(r"(?<!^)(?=[A-Z])", "-", self.name).lower()

    @property
    def root(self):
        return Path(self.work_dir) / f"{self.slug}-recomp"

    @property
    def repo(self):
        return self.root / self.name


# ---------------------------------------------------------------- probing

def patch_probe_subdir_boot(template, log=print):
    """Teach the framework probe to follow SYSTEM.CNF boot paths that
    point INTO a subdirectory (Tekken 3: BOOT = cdrom:\\TEKKEN3\\SLUS_004.02;1
    — the stock probe only searches the root directory and fails those
    discs with 'boot EXE not on disc'). Idempotent text patch, applied
    to the template / framework cache and to scaffolded projects."""
    try:
        probe = Path(template) / "psxrecomp" / "tools" / \
            "new_project_layout" / "probe_disc.py"
        if not probe.exists():
            return
        text = probe.read_text()
        if "boot_path_components" in text:
            return
        helper = '''

def boot_path_components(cnf: bytes) -> list[str]:
    """SYSTEM.CNF BOOT path split into components, e.g.
    cdrom:\\TEKKEN3\\SLUS_004.02;1 -> ["TEKKEN3", "SLUS_004.02"]."""
    for line in cnf.decode("ascii", "replace").splitlines():
        if line.strip().upper().startswith("BOOT"):
            rhs = line.split("=", 1)[1].strip().split(";")[0].strip()
            if rhs.lower().startswith("cdrom:"):
                rhs = rhs[6:]
            parts = [p for p in re.split(r"[\\\\/]+", rhs.strip("\\\\/ ")) if p]
            return [p.upper() for p in parts]
    return []

'''
        anchor = "\ndef normalize_serial("
        if anchor not in text:
            return
        text = text.replace(anchor, helper + "def normalize_serial(", 1)
        old_raise = """            else:
                raise SystemExit(
                    f"boot EXE {boot_token!r} not on disc "
                    f"(found {sorted(entries)[:24]})"
                )
"""
        new_raise = """            else:
                # Subdirectory boot: walk the BOOT path's directories
                # and register the EXE under its basename so the
                # reads below work unchanged.
                parts = boot_path_components(cnf)
                resolved = None
                if len(parts) > 1:
                    cur = entries
                    ok = True
                    for comp in parts[:-1]:
                        hit = next((k for k in cur if k.upper() == comp),
                                   None)
                        if hit is None:
                            ok = False
                            break
                        dext, dsize = cur[hit]
                        dbytes = b"".join(
                            read_user(data, dext + i)
                            for i in range((dsize + USER - 1) // USER))
                        cur = parse_root_entries(dbytes[:dsize])
                    if ok:
                        hit = next((k for k in cur
                                    if k.upper() == parts[-1]), None)
                        if hit is not None:
                            resolved = cur[hit]
                if resolved is not None:
                    entries[disc_boot] = resolved
                else:
                    raise SystemExit(
                        f"boot EXE {boot_token!r} not on disc "
                        f"(found {sorted(entries)[:24]})"
                    )
"""
        if old_raise not in text:
            return
        text = text.replace(old_raise, new_raise, 1)
        probe.write_text(text)
        log("patched probe_disc.py: subdirectory boot paths (Tekken 3)")
    except Exception as e:  # a patch failure must never break a run
        log(f"probe subdir patch skipped: {e}")


def patch_overlay_draw_chain(template, log=print):
    """Let the in-game menu and the RetroAchievements pop-ups share the
    host overlay draw slot. Upstream host_overlay.c keeps ONE draw
    callback: the menu provider registers at session setup, the RA module
    registers when it starts and REPLACES it, so the menu opens
    invisibly (F10 works, nothing paints - field-found on Evil Dead's
    disc swap, 2026-11-02). This patch keeps up to two callbacks keyed
    by function pointer, drawn last-registered first so the menu paints
    on top; a NULL registration still clears all (teardown semantics).
    Idempotent text patch on the template / framework cache and on
    scaffolded projects."""
    try:
        src = Path(template) / "psxrecomp" / "runtime" / "src" / \
            "host_overlay.c"
        if not src.exists():
            return
        text = src.read_text()
        if "PSX_HOST_OVERLAY_DRAW_MAX" in text:
            return
        start = "/* ---- P2: overlay draw callback"
        end = "/* ---- Game HUD layer"
        if start not in text or end not in text:
            return
        i = text.index(start)
        j = text.index(end)
        text = text[:i] + OVERLAY_DRAW_CHAIN_SPAN + text[j:]
        src.write_text(text)
        log("patched host_overlay.c: menu + RA share the overlay draw")
    except Exception as e:  # a patch failure must never break a run
        log(f"overlay draw-chain patch skipped: {e}")


OVERLAY_DRAW_CHAIN_SPAN = "/* ---- P2: overlay draw callback -------------------------------------------- */\n/* Registered on the main thread, read on the render / present thread. The\n * function and its context are published together as one immutable record\n * behind an atomic pointer, so a reader never pairs a live function with a\n * stale context.\n *\n * Lifetime: every call runs inside an in-flight count. Replacing or removing\n * the callback swaps the record and then waits until no call that could have\n * read the old record is still running, so when psx_host_overlay_set_draw_cb\n * returns, the previous fn will not run again and its ctx (and the GL objects\n * it draws with) may be freed by the caller. The old record is freed there\n * too. A change made from inside the callback itself does not wait (it would\n * wait for itself); that caller's own frame is the only one still using the\n * old ctx and it returns before the next frame reads the new record. */\n/* Up to two draw consumers share this layer: the in-game menu provider\n * registers first (at session setup) and feature overlays such as the\n * RetroAchievements pop-ups register afterwards. A one-callback record let\n * the later registration silently replace the menu's draw, leaving an open\n * but invisible menu, so the record now keeps both, keyed by function\n * pointer. Entries draw last-registered first, so the first registrant\n * (the menu) paints on top. A NULL fn keeps its historic meaning: the\n * caller is tearing the layer down and every entry is removed. */\n#define PSX_HOST_OVERLAY_DRAW_MAX 2\ntypedef struct {\n    PsxHostOverlayDrawFn fn;\n    void *ctx;\n} PsxHostOverlayDrawEntry;\ntypedef struct {\n    PsxHostOverlayDrawEntry entries[PSX_HOST_OVERLAY_DRAW_MAX];\n    int count;\n} PsxHostOverlayDraw;\n\nstatic _Atomic(PsxHostOverlayDraw *) s_draw = NULL;\nstatic atomic_flag s_draw_set_lock = ATOMIC_FLAG_INIT;\nstatic atomic_int s_draw_inflight = 0;\n#if defined(_MSC_VER)\nstatic __declspec(thread) int s_in_draw = 0;\n#else\nstatic _Thread_local int s_in_draw = 0;\n#endif\n\nstatic void draw_yield(void) {\n#if defined(_WIN32)\n    Sleep(0);\n#else\n    sched_yield();\n#endif\n}\n\nvoid psx_host_overlay_set_draw_cb(PsxHostOverlayDrawFn fn, void *ctx) {\n    PsxHostOverlayDraw *rec = NULL, *old;\n    /* Compose the successor from the current record under a lock so two\n     * registering threads cannot lose each other's entry. Readers never\n     * take this lock, so composing cannot deadlock a draw in flight; the\n     * in-flight wait + free below keep the lifetime contract above. */\n    while (atomic_flag_test_and_set(&s_draw_set_lock)) draw_yield();\n    old = atomic_load(&s_draw);\n    if (fn) {\n        int found = -1;\n        rec = (PsxHostOverlayDraw *)malloc(sizeof(*rec));\n        if (!rec) {\n            atomic_flag_clear(&s_draw_set_lock);\n            return;\n        }\n        rec->count = 0;\n        if (old) {\n            for (int i = 0; i < old->count; ++i) {\n                if (old->entries[i].fn == fn) found = rec->count;\n                rec->entries[rec->count++] = old->entries[i];\n            }\n        }\n        if (found >= 0) {\n            rec->entries[found].ctx = ctx; /* re-registering keeps its slot */\n        } else {\n            if (rec->count >= PSX_HOST_OVERLAY_DRAW_MAX)\n                rec->count = PSX_HOST_OVERLAY_DRAW_MAX - 1; /* evict newest other */\n            rec->entries[rec->count].fn = fn;\n            rec->entries[rec->count].ctx = ctx;\n            rec->count++;\n        }\n    }\n    atomic_store(&s_draw, rec); /* seq_cst: ordered with the readers' count */\n    atomic_flag_clear(&s_draw_set_lock);\n    if (s_in_draw) return; /* from inside the callback: see above; old stays (rare) */\n    while (atomic_load(&s_draw_inflight) > 0) draw_yield();\n    free(old);\n}\n\nint psx_host_overlay_has_draw(void) {\n    return atomic_load_explicit(&s_draw, memory_order_acquire) != NULL;\n}\n\nvoid psx_host_overlay_draw(int width, int height) {\n    PsxHostOverlayDraw *rec;\n    if (width <= 0 || height <= 0) return;\n    atomic_fetch_add(&s_draw_inflight, 1); /* seq_cst, before reading the record */\n    rec = atomic_load(&s_draw);\n    if (rec) {\n        s_in_draw = 1;\n        for (int i = rec->count - 1; i >= 0; --i)\n            if (rec->entries[i].fn)\n                rec->entries[i].fn(width, height, rec->entries[i].ctx);\n        s_in_draw = 0;\n    }\n    atomic_fetch_sub(&s_draw_inflight, 1);\n}\n\n"

def patch_cdrom_getlocp(template, log=print):
    """Fix the runtime's CD GetlocP answer for positions inside a CD-DA
    track. Upstream cdrom.c's not-playing branch hardcoded track 1 /
    track_lba 0, so after a SeekP into a music track the sub-Q answer
    described an impossible position (track 1 at an absolute MSF past
    track 1's end). Games that poll GetlocP to confirm the seek landed
    in the target track before starting the level's music/load
    sequence (Tomb Raider 1 + 2 do exactly this) never got their
    confirmation and hung on a black screen, no audio, forever.
    The patch mirrors the CD-DA playing branch: report the track that
    actually contains the cursor, and give hardware-correct pregap
    sub-Q (index 0, relative MSF counting DOWN to the track start) —
    a seek to the GetTD minute:second lands up to 74 sectors early,
    in the pregap. Idempotent text patch on the template / framework
    cache and on scaffolded projects (found 2026-11-05, TR1 + TR2
    verified into gameplay with it)."""
    try:
        src = Path(template) / "psxrecomp" / "runtime" / "src" / \
            "cdrom.c"
        if not src.exists():
            return
        text = src.read_text()
        if "cdda_track_for_lba((uint32_t)lba)" in text:
            return
        old_a = """            lba = msf_to_lba(read_min, read_sec, read_sect);
        }
        if (subq_replacements_active) update_last_valid_subq((uint32_t)lba);"""
        new_a = """            lba = msf_to_lba(read_min, read_sec, read_sect);
            /* The sub-Q track/index must describe the track that CONTAINS
             * the cursor, as on hardware — not a constant track 1. After a
             * SeekP into a CD-DA track, guests poll GetlocP to confirm the
             * seek landed in the target track before proceeding (Tomb
             * Raider 1/2 wait for the music track after their level-load
             * seek; with track hardcoded to 1 the confirmation never
             * arrives and the load state machine stalls on a black screen
             * forever). Mirror the playing branch: real containing track,
             * relative MSF from that track's INDEX 01 start. */
            track = cdda_track_for_lba((uint32_t)lba);
            if (track < 1) track = 1;
            track_lba = (int)iso_cdda_track_start_lba(iso_handle, track);
        }
        if (subq_replacements_active) update_last_valid_subq((uint32_t)lba);"""
        old_b = """            lba_to_msf(lba - track_lba, 0, &rm, &rs, &rf);"""
        new_b = """            /* Sub-Q index/relative: inside INDEX 01 the relative MSF counts
             * up from the track start; in the pregap (position before the
             * track's INDEX 01, e.g. a seek to the GetTD minute:second —
             * which carries no frame part — lands up to 74 sectors early)
             * the index is 0 and the relative MSF counts DOWN to the track
             * start, as the Q channel reports it on hardware. */
            int index = 1;
            int rel = lba - track_lba;
            if (rel < 0) { index = 0; rel = -rel; }
            lba_to_msf(rel, 0, &rm, &rs, &rf);"""
        old_c = """            response_push(bin_to_bcd(track));
            response_push(0x01);"""
        new_c = """            response_push(bin_to_bcd(track));
            response_push(bin_to_bcd(index));"""
        if old_a not in text or old_b not in text or old_c not in text:
            return
        text = text.replace(old_a, new_a, 1)
        text = text.replace(old_b, new_b, 1)
        text = text.replace(old_c, new_c, 1)
        src.write_text(text)
        log("patched cdrom.c: GetlocP reports the real track + pregap "
            "sub-Q (Tomb Raider 1/2 black-screen fix)")
    except Exception as e:  # a patch failure must never break a run
        log(f"cdrom GetlocP patch skipped: {e}")


def probe_cue(runner, cue, template, players=1, json_out=None):
    patch_probe_subdir_boot(template,
                            getattr(runner, "log", print))
    probe = template / "psxrecomp" / "tools" / "new_project_layout" / "probe_disc.py"
    out = json_out or (Path(cue).parent / (Path(cue).stem + ".probe.json"))
    runner.run_ok(["python3", probe, "--json-out", out, "--players", players, cue],
                  timeout=300)
    return json.loads(Path(out).read_text())


def check_disc_set(runner, probe_json_paths, template):
    tool = template / "psxrecomp" / "tools" / "new_project_layout" / "verify_disc_set.py"
    out = Path(probe_json_paths[0]).parent / "disc_set.json"
    rc = runner.run(["python3", tool, "--json-out", out, *probe_json_paths],
                    timeout=120)
    verdict = {}
    if out.exists():
        verdict = json.loads(out.read_text())
    verdict["_rc"] = rc
    return verdict


def classify_set(verdict):
    """Route a disc-set verdict: "same" (every disc boots ONE
    identical program -> one build, one package), "split" (the discs
    provably boot DIFFERENT programs -> one build per disc), or
    "review" (anything unclear -> never guessed). verify_disc_set.py
    emits verdict "data-only" for the one-program case and
    "n-programs" when the program fields differ."""
    v = verdict.get("verdict")
    differing = verdict.get("differing_program_fields")
    if (v in ("data-only", "same_program") and not differing) or \
            verdict.get("same_program") or verdict.get("buildable") is True:
        return "same"
    if v == "n-programs" and differing:
        return "split"
    return "review"


REDUMP_DAT_URL = ("https://raw.githubusercontent.com/libretro/"
                  "libretro-database/master/metadat/redump/"
                  "Sony%20-%20PlayStation.dat")


def redump_name_for_serial(serial, cache_dir):
    """Look a game's Redump name up by its disc serial (SLUS-00331 ->
    'Final Doom (USA)') in libretro-database's Redump DAT, downloaded
    once and cached. Returns None when the serial is unknown (homebrew,
    prototypes, unlisted revisions)."""
    if not serial:
        return None
    dat = Path(cache_dir) / "redump-psx.dat"
    try:
        if not dat.exists() or dat.stat().st_size < 100000:
            req = urllib.request.Request(
                REDUMP_DAT_URL, headers={"User-Agent": "RecompTools/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            dat.parent.mkdir(parents=True, exist_ok=True)
            dat.write_bytes(data)
        text = dat.read_text(errors="replace")
    except Exception:
        return None
    for block in text.split("\ngame ("):
        if f'\tserial "{serial}"' in block:
            m = re.search(r'\tname "([^"]+)"', block)
            if m:
                return m.group(1)
    return None


LIBRETRO_BOXART_HOSTS = (
    # Same Named_Boxarts data on two hosts: the GitHub mirror first
    # (the canonical thumbnails host has been seen refusing every
    # request for hours at a time), then the canonical host itself.
    "https://raw.githubusercontent.com/libretro-thumbnails/"
    "Sony_-_PlayStation/master/Named_Boxarts/{}.png",
    "https://thumbnails.libretro.com/Sony%20-%20PlayStation/"
    "Named_Boxarts/{}.png",
)


def _fetch_bytes_retry(url, timeout=25, attempts=3):
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "RecompTools/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if i + 1 < attempts:
                time.sleep(2 * (i + 1))
    return None


def _valid_cover(data):
    if not data or len(data) < 8000:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24:
            return False
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w >= 200 and h >= 200
    return data[:3] == b"\xff\xd8\xff"


def fetch_cover(cue, dest_dir, serial=""):
    """Fetch Named_Boxarts art from libretro-thumbnails, trying both
    hosts that serve it and retrying each name. Prefer the game's
    Redump name resolved from its disc serial (the probe reads the
    serial off the disc, so the cue's filename does not matter); try
    the name without its leading 'ABBR - ' prefix too, then fall back
    to the cue filename. Returns the PNG path or None."""
    names = []
    by_serial = redump_name_for_serial(serial, dest_dir)
    if by_serial:
        names.append(by_serial)
        if " - " in by_serial:
            names.append(by_serial.split(" - ", 1)[1])
    stem = Path(cue).stem
    if stem not in names:
        names.append(stem)
    dest = Path(dest_dir) / (stem + ".png")
    for template in LIBRETRO_BOXART_HOSTS:
        for nm in names:
            name = nm
            for ch in '&*/:<>?\\|"':
                name = name.replace(ch, "_")
            data = _fetch_bytes_retry(
                template.format(urllib.parse.quote(name)))
            if _valid_cover(data):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                return dest
    return None


# ---------------------------------------------------------------- stages

def stage_scaffold(r, job, template):
    root, repo = job.root, job.repo
    # setup_project.sh refuses to create into an existing target dir,
    # so never pre-create the repo; clear a previous partial attempt
    # (ours - it has no CMakeLists.txt yet) instead.
    if repo.exists() and not (repo / "CMakeLists.txt").exists():
        shutil.rmtree(repo)
    root.mkdir(parents=True, exist_ok=True)
    layout = template / "psxrecomp" / "tools" / "new_project_layout"
    cue = job.cues[0]
    # setup_project.sh builds the skeleton and tries to clone the
    # framework; the clone hangs on some networks, so cap it and fall
    # back to copying the template's framework trees.
    try:
        prefix = ["timeout", "180"] if shutil.which("timeout") else []
        r.run([*prefix, layout / "setup_project.sh", "--disc", cue,
               "--dir", root, "--name", job.name,
               "--players", job.settings.players or 1,
               "--enable-recomp-ui", "--yes"], cwd=layout)
    except StageError as ex:
        r.log(f"scaffold script issue ({ex}); continuing with local copy")
    if not (repo / "psxrecomp" / "CMakeLists.txt").exists():
        r.log("framework missing after scaffold - copying from template")
        for tree in ("psxrecomp", "recomp-ui"):
            src, dst = template / tree, repo / tree
            if src.is_dir() and not dst.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
    if not (repo / "CMakeLists.txt").exists():
        raise StageError("scaffold produced no CMakeLists.txt")
    (root / "game").mkdir(exist_ok=True)


def stage_probe(r, job, template):
    repo = job.repo
    json_paths = []
    job.probes = []
    for i, cue in enumerate(job.cues):
        jp = repo / f"disc_probe{'' if i == 0 else f'_{i+1}'}.json"
        args = ["python3",
                template / "psxrecomp" / "tools" / "new_project_layout" / "probe_disc.py",
                "--json-out", jp, "--players", job.settings.players or 1, cue]
        if i == 0:
            args[2:2] = ["--write-game-toml", repo / "game.toml",
                         "--write-seeds", repo / "seeds" / "ghidra_funcs.txt",
                         "--write-catalog", repo / "catalog_identity.json"]
        r.run_ok(args, timeout=300)
        job.probes.append(json.loads(jp.read_text()))
        json_paths.append(jp)
    if len(job.cues) > 1:
        job.set_verdict = check_disc_set(r, json_paths, template)
        # run_job's pre-pass routes different-program sets to per-disc
        # builds before this stage ever runs, so anything reaching
        # here that is not one program is a genuine surprise.
        if classify_set(job.set_verdict) != "same":
            raise StageError(
                "multi-disc set is NOT one program - needs review "
                f"(verdict: {json.dumps(job.set_verdict)[:300]})")
        r.log("disc set verified: one program, one build")


def _patch_first(path, subs):
    txt = path.read_text()
    orig = txt
    for pat, rep in subs:
        txt = re.sub(pat, rep, txt, count=1)
    if txt != orig:
        path.write_text(txt)
    return txt != orig


def stage_patch_cmake(r, job):
    repo = job.repo
    # For a verified set, the config/generate disc is the FIRST cue:
    # the scaffold writes game.toml from it and Generate names its
    # files after its boot EXE. (probes[-1] here named Driver 2's
    # SLUS_013.18 while the generated files were SLUS_011.61_* — the
    # glob matched nothing and the build linked zero game code.)
    probe = job.probes[0]
    stem = probe["boot_exe"]                       # e.g. SLUS_009.84
    cm = repo / "CMakeLists.txt"
    players = job.settings.players or 1
    changed = _patch_first(cm, [
        (r'GEN_MARKER\s+"[^"]*"', f'GEN_MARKER "generated/{stem}_dispatch.c"'),
        (r'GEN_FULL_GLOB\s+"[^"]*"', f'GEN_FULL_GLOB "generated/{stem}_full_*.c"'),
        (r'WINDOW_TITLE\s+"[^"]*"', f'WINDOW_TITLE "{job.name}"'),
        (r'MAX_PLAYERS\s+\d+', f'MAX_PLAYERS {players}'),
    ])
    r.log(f"CMakeLists patched for {stem} (players {players}): {changed}")
    if f"generated/{stem}_dispatch.c" not in cm.read_text():
        raise StageError("GEN_MARKER patch did not land - check CMakeLists")


RA_CMAKE_BLOCK = '''
# Drop-in RetroAchievements module in src_ra/ (bundled with Recomp Tools;
# includes the login toast, the callback-polarity fix, and the in-game
# pop-up toast overlay). It self-registers via constructor, so no
# framework file is edited except the mod_runtime_disc_path() accessor.
# Excludes rc_libretro.c (needs libretro.h, not shipped) and
# rc_client_raintegration.c (RAIntegration toolkit, not the standalone client).
file(GLOB_RECURSE RCHEEVOS_SOURCES
    "${CMAKE_CURRENT_SOURCE_DIR}/ra-rcheevos/src/*.c")
list(FILTER RCHEEVOS_SOURCES EXCLUDE REGEX "rc_libretro\\\\.c$")
list(FILTER RCHEEVOS_SOURCES EXCLUDE REGEX "rc_client_raintegration\\\\.c$")
target_sources(psx-runtime PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src_ra/ra_client.cpp"
    "${CMAKE_CURRENT_SOURCE_DIR}/src_ra/ra_http.c"
    ${RCHEEVOS_SOURCES})
target_include_directories(psx-runtime PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/ra-rcheevos/include"
    "${CMAKE_CURRENT_SOURCE_DIR}/src_ra"
    "${CMAKE_CURRENT_SOURCE_DIR}/recomp-ui/src"
    "${PSXRECOMP_ROOT}/runtime/include")
if(WIN32)
    target_link_libraries(psx-runtime PRIVATE winhttp)
else()
    find_package(OpenSSL REQUIRED)
    target_link_libraries(psx-runtime PRIVATE OpenSSL::SSL OpenSSL::Crypto)
endif()
@POPUPS_OFF@
'''


def stage_ra(r, job):
    if not job.settings.ra_enabled:
        r.log("RetroAchievements disabled in settings - skipping")
        return
    repo = job.repo
    for item in ("src_ra", "ra-rcheevos", "retroachievements.toml.example"):
        src, dst = BUNDLE_RA / item, repo / item
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    # Framework accessor: mod_runtime_disc_path()
    hdr = repo / "psxrecomp" / "runtime" / "include" / "mod_runtime.h"
    cpp = repo / "psxrecomp" / "runtime" / "src" / "mod_runtime.cpp"
    if "mod_runtime_disc_path" not in hdr.read_text():
        ok = _patch_first(hdr, [(r'(mod_runtime_effective_disc_path\(\);)',
                                 r'\1\nconst std::filesystem::path& mod_runtime_disc_path();')])
        if not ok:
            raise StageError("could not add accessor declaration to mod_runtime.h")
    if "mod_runtime_disc_path" not in cpp.read_text():
        # Insert right after the effective_disc_path definition: newer
        # framework revisions keep state() inside a namespace that is
        # closed by end-of-file, so appending at EOF does not compile.
        ok = _patch_first(cpp, [(
            r'(const std::filesystem::path& mod_runtime_effective_disc_path\(\) \{\n.*?\n\}\n)',
            r'\1\nconst std::filesystem::path& mod_runtime_disc_path() {\n'
            r'    return state().disc_path;\n}\n')])
        if not ok:
            raise StageError("could not add accessor impl to mod_runtime.cpp")
    r.log("RA accessor present")
    cm = repo / "CMakeLists.txt"
    txt = cm.read_text()
    if "RCHEEVOS_SOURCES" not in txt:
        popups = "" if job.settings.ra_popups else \
            'target_compile_definitions(psx-runtime PRIVATE RA_DISABLE_POPUPS=1)'
        block = RA_CMAKE_BLOCK.replace("@POPUPS_OFF@", popups)
        lines = txt.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if "Stage runtime configs" in line:
                lines.insert(i, block + "\n")
                break
        else:
            raise StageError("anchor '# Stage runtime configs' not found in CMakeLists")
        cm.write_text("".join(lines))
    r.log("RA CMake block present"
          + ("" if job.settings.ra_popups else " (pop-ups DISABLED)"))


def stage_emitters(r, job):
    repo = job.repo
    r.run_ok(["cmake", "-S", repo / "psxrecomp" / "recompiler",
              "-B", repo / "build-recompiler", "-G", "Ninja",
              "-DCMAKE_BUILD_TYPE=Release"], timeout=600)
    r.run_ok(["cmake", "--build", repo / "build-recompiler",
              "--target", "psxrecomp-game", "psxrecomp-bios",
              "-j", job.settings.jobs], timeout=1800)


def stage_generate(r, job):
    repo = job.repo
    # Generate against the FIRST cue: the scaffold writes game.toml's
    # [prepare_disc] known digests from the first cue, and generate's
    # disc check rejects any other disc of the set (Driver 2, 2026-11-01).
    # For a verified one-program set the boot EXE is identical on every
    # disc, so the generated code is the same either way.
    cue = job.cues[0]
    r.run_ok(["python3", repo / "psxrecomp" / "psxrecomp_cli.py", "generate",
              "--config", "game.toml", "--project-root", ".",
              "--disc", cue, "--no-toolchain-download"],
             cwd=repo, timeout=3600)
    gen = repo / "generated"
    n = len(list(gen.glob("*.c"))) if gen.is_dir() else 0
    if n == 0:
        raise StageError("Generate produced no C files")
    r.log(f"generated/ holds {n} C files")


def vulkan_sdk_status():
    """(headers_dir, glslc) — either may be None. The framework builds
    the REAL Vulkan renderer only when Vulkan headers + glslc exist at
    build time; without them gpu_vk_renderer.c compiles to an inert
    stub that reports itself 'unavailable' at runtime."""
    sdk = os.environ.get("VULKAN_SDK")
    cands = []
    if sdk:
        cands += [Path(sdk) / "Include", Path(sdk) / "include"]
    cands += [Path("/usr/include"), Path("/usr/local/include")]
    headers = next((c for c in cands
                    if (c / "vulkan" / "vulkan.h").exists()), None)
    glslc = shutil.which("glslc")
    if not glslc and sdk:
        for sub in ("Bin", "bin"):
            p = Path(sdk) / sub / "glslc"
            if p.exists():
                glslc = str(p)
    return headers, glslc


def stage_build(r, job, target):
    repo = job.repo
    s = job.settings
    if target == "linux":
        # SDL_X11_XTEST is only SDL's test-automation hook; requiring
        # libxtst-dev makes stock desktops fail configure for nothing.
        bdir, extra = repo / "build-release", ["-DSDL_X11_XTEST=OFF"]
    else:
        bdir = repo / "build-windows"
        extra = ["-DCMAKE_TOOLCHAIN_FILE=psxrecomp/cmake/toolchain-mingw-w64.cmake"]
    if s.renderer == "vulkan":
        # Never ship a fake Vulkan: without the SDK the framework
        # silently builds a stub, so refuse up front with the fix.
        headers, glslc = vulkan_sdk_status()
        if not headers or not glslc:
            raise StageError(
                "Vulkan renderer selected but the Vulkan SDK is not "
                "installed on this machine (need Vulkan headers + "
                "glslc). Install with the toolbox Install button, or: "
                "sudo apt install -y libvulkan-dev glslc — or choose "
                "OpenGL or "
                "Software. (The build refuses to ship a stub that only "
                "pretends to be Vulkan.)")
        extra.append("-DPSX_ENABLE_VULKAN=ON")
        r.log(f"Vulkan SDK found: headers {headers}, glslc {glslc}")
    env = None
    if target != "linux" and not os.environ.get("VULKAN_SDK"):
        # Cross-builds (MinGW) cannot find_path the host's Vulkan
        # headers, so their Vulkan backend silently builds as the
        # stub even on SDK machines. Point VULKAN_SDK at a small
        # shim inside the project that symlinks the host's headers
        # and glslc into the SDK layout the framework checks first.
        headers, glslc_path = vulkan_sdk_status()
        if headers and glslc_path:
            shim = repo / ".vulkan-sdk-shim"
            (shim / "bin").mkdir(parents=True, exist_ok=True)
            inc_link = shim / "include"
            if not inc_link.exists():
                inc_link.symlink_to(headers, target_is_directory=True)
            glslc_link = shim / "bin" / "glslc"
            if not glslc_link.exists():
                glslc_link.symlink_to(glslc_path)
            env = {"VULKAN_SDK": str(shim)}
            r.log("Cross-build: VULKAN_SDK pointed at a host shim "
                  "so the Windows build finds the Vulkan SDK too")
    r.run_ok(["cmake", "-S", repo, "-B", bdir, "-G", "Ninja",
              "-DCMAKE_BUILD_TYPE=Release",
              f"-DPSX_RECOMP_UI={'ON' if s.launcher_ui else 'OFF'}",
              *extra], timeout=900, env=env)
    r.run_ok(["cmake", "--build", bdir, "--target", "psx-runtime",
              "-j", s.jobs], timeout=7200, env=env)
    exe = bdir / (job.name if target == "linux" else job.name + ".exe")
    if not exe.exists() or exe.stat().st_size == 0:
        raise StageError(f"{target} build finished but {exe} is missing/empty")
    # The real backend embeds SPIR-V shaders into vk_shaders_spv.h;
    # the stub build never generates it. Record the truth per target
    # for EVERY renderer (not just Vulkan jobs): packaging offers
    # Vulkan for exactly the targets whose build contains it.
    vk_real = bool(list(bdir.rglob("vk_shaders_spv.h")))
    if s.renderer == "vulkan" and not vk_real:
        raise StageError(
            f"{target} build claimed Vulkan but vk_shaders_spv.h "
            "was not generated — the inert stub was built instead. "
            "Check the Vulkan SDK installation.")
    job.vulkan_backend[target] = vk_real
    if vk_real:
        r.log(f"Vulkan backend in the {target} build "
              "(vk_shaders_spv.h present) - package will offer Vulkan")
    elif s.renderer != "vulkan":
        r.log(f"No Vulkan backend in the {target} build (Vulkan SDK "
              "not found at build time) - package will not offer it")
    r.log(f"{target} binary: {exe} ({exe.stat().st_size} bytes)")


def stage_verify(r, job):
    repo = job.repo
    probe = job.probes[0]
    entry = ("func_" + probe["entry_pc"].replace("0x", "")).lower()
    checks = []
    if job.settings.build_linux:
        checks.append((repo / "build-release" / job.name, "nm"))
    if job.settings.build_windows:
        checks.append((repo / "build-windows" / (job.name + ".exe"),
                       "x86_64-w64-mingw32-nm"))
    for exe, nm in checks:
        p = subprocess.run([nm, str(exe)], capture_output=True, text=True)
        syms = p.stdout.lower()
        count = len(re.findall(r"\bfunc_[0-9a-f]{8}\b", syms))
        if count == 0 or entry not in syms:
            raise StageError(
                f"verify failed for {exe.name}: {count} game functions, "
                f"entry {entry} {'present' if entry in syms else 'MISSING'}")
        r.log(f"verify OK: {exe.name} has {count} game functions, entry present")


# ---------------------------------------------------------------- packaging

def disc_id(probe):
    """Identity for a disc in the packaged game.toml. Retail discs
    carry a serial; prototypes like Resident Evil 1.5 (MZD) have an
    EMPTY serial, and shipping id = "" would leave the launcher
    roster keyed on nothing. Fall back to the volume id, then the
    boot EXE stem."""
    for v in (probe.get("serial"), probe.get("volume_id")):
        if v:
            return v
    return Path(probe.get("boot_exe", "UNKNOWN")).stem or "UNKNOWN"


def package_game_toml(job, offer_vulkan=False):
    probe = job.probes[0]
    s = job.settings
    players = s.players or 1
    lines = [
        f"# {job.title} (PS1) - recompiled for PC.",
        "# No disc is included in this package. Use your own legally dumped",
        "# disc image: pick the .cue in the launcher (Browse For Disc) or run",
        "# with --disc <your .cue>.",
        "",
        "[game]",
        f'name = "{job.title}"',
        f'id = "{disc_id(probe)}"',
        f"players = {players}",
        f'exe = "disc/{probe["boot_exe"]}"',
        f'load_address = "{probe["load_address"]}"',
        f'entry_pc = "{probe["entry_pc"]}"',
        f'text_size = "{probe["text_size"]}"',
        f'stack_base = "{probe["stack_base"]}"',
    ]
    if len(job.cues) > 1:
        discs = ",\n    ".join(f'"{Path(c).name}"' for c in job.cues)
        serials = ",\n    ".join(f'"{disc_id(p)}"' for p in job.probes)
        lines += [f"discs = [\n    {discs},\n]",
                  f"disc_serials = [\n    {serials},\n]"]
    lines += [
        "",
        "[recompiler]",
        'seeds = "seeds/ghidra_funcs.txt"',
        'out_dir = "generated"',
        "strict = true",
        "",
        "[runtime]",
        f'window_title = "{job.title} Recompiled"',
        'memcard_dir = "saves"',
        "overlay_cache = true",
        "",
        "[video]",
        f'renderer = "{s.renderer}"',
        # The launcher only OFFERS Vulkan when the game opts in via
        # offer_vulkan (runtime main.cpp: "settings requested Vulkan,
        # but this game does not offer Vulkan in the launcher; using
        # OpenGL"). Without this line a Vulkan build silently runs
        # OpenGL - caught on the Tekken 3 test build, 2026-11-01.
        # Since v1.0.13 the offer follows the BUILD, not the job's
        # default renderer: it is written for every target whose
        # build tree proved a real Vulkan backend (stage_build
        # records it in job.vulkan_backend), so any package built on
        # a machine with the Vulkan SDK is switchable between
        # renderers in the game's launcher. The default renderer
        # above stays the job's choice (OpenGL unless set otherwise).
        *( ['offer_vulkan = true']
           if (offer_vulkan or s.renderer == "vulkan") else [] ),
        f'aspect_ratio = "{s.aspect_ratio}"',
        "",
        "[controller]",
        f'default_mode = "{s.controller_mode}"',
        "",
    ]
    return "\n".join(lines)


def package_readme(job, offer_vulkan=False):
    probe = job.probes[0]
    s = job.settings
    multi = ""
    if probe.get("track_count", 1) > 1:
        multi = (f"\n## IMPORTANT - multi-track disc\n\nThis game ships on a "
                 f"**multi-track disc ({probe['track_count']} tracks)**. Your dump "
                 "must be the `.cue` file **together with all of its track "
                 "`.bin` files** in the same folder - the music lives on the "
                 "audio tracks.\n")
    ra = ""
    if s.ra_enabled:
        ra = ("\n## RetroAchievements\n\n"
              + ("A filled `retroachievements.toml` is included next to the "
                 "program - see `retroachievements.toml.example` for the "
                 "format. NOTE: it contains a personal API token - keep it "
                 "to yourself and do not share the zip.\n"
                 if s.ra_toml_path else
                 "Copy `retroachievements.toml.example` to "
                 "`retroachievements.toml`, fill in your username and token, "
                 "and restart the game.\n")
              + "When an achievement unlocks, an in-game pop-up appears "
                "top-center (OpenGL renderer only)."
              + ("" if s.ra_popups else
                 " NOTE: pop-ups were disabled for this build in Recomp "
                 "Tools (the login toast is hidden too - same draw path).")
              + "\nRetroAchievements currently classifies this client as an "
                "unknown emulator, so unlocks count as Casual (softcore) "
                "even with hardcore on. Login and achievements work.\n")
    swap = ""
    if len(job.cues) > 1:
        swap = ("\n## Changing discs mid-game\n\n"
                "This game spans more than one disc, and this build "
                "supports swapping while the game keeps running:\n\n"
                "- When the game asks for another disc, open the in-game "
                "menu: **F10** (or **Ctrl+L**) on the keyboard, or "
                "**Select/Back + L3** (left stick click) together on the "
                "controller.\n"
                "- Go to the **Disc** section, set **Selected disc** to "
                "the disc the game wants, then choose **Insert disc and "
                "resume**.\n\n"
                "Games that only swap at a save point work either way: "
                "the menu above, or save, quit, and relaunch picking the "
                "other disc in the launcher.\n")
    ss = ""
    if not s.save_states:
        ss = ("\n## Save states\n\nSave-state hotkeys (F7/F8) are unbound in "
              "the included `config.ini` (Recomp Tools setting). Delete that "
              "file or rebind the keys in the launcher to use save states.\n")
    vk = ""
    if offer_vulkan and s.renderer != "vulkan":
        vk = ("\n## Renderer\n\n"
              "This build also contains the Vulkan renderer: switch "
              "between OpenGL and Vulkan in the game's launcher "
              "(Settings). The default is OpenGL. RetroAchievements "
              "pop-ups draw only under OpenGL.\n")
    return f"""# {job.title} - Recompiled for PC

{job.title} (PS1, {disc_id(probe)}), recompiled to native PC.
{job.settings.players or 1} player(s). No disc is included - use your own
legally dumped disc image.
{multi}
## Running it

- **Launcher (recommended):** run the program with no arguments, pick your
  disc with Browse For Disc, set up controllers on the PLAYER tabs, PLAY.
- **Direct:** `./{job.name} --disc "/path/to/game.cue" --no-launcher`
  (Windows: `{job.name}.exe` with the same arguments.)

## Controls

Keyboard works out of the box. For controllers, connect them BEFORE
launching, then use the launcher's PLAYER tabs.
{swap}{ra}{vk}{ss}
## BIOS

Uses the built-in OpenBIOS - no BIOS file needed.

## Saves

Memory cards live in `saves/` (card1.mcd, card2.mcd).

## Caveats

- The Windows build is cross-compiled and has not yet been run on real
  Windows.
- Packaged by Recomp Tools. Boot/gameplay verification status is in the
  conversion log kept with the project.

## Credits

Built with PSXRecomp, the PS1 static-recompilation framework created by
Matthew Stanley (mstan), now part of the RetroPortingToolkit
(retroportingtoolkit.com). Launcher: recomp-ui (same team).
"""


def _job_artwork(r, job):
    """Cover art to bundle into packages: the GUI-fetched file when the
    job carries one, else fetch by serial into the app art cache (CLI
    parity). Returns a Path or None."""
    if job.art and Path(job.art).exists():
        return Path(job.art)
    serial = job.probes[0].get("serial") if job.probes else ""
    if serial and job.cues:
        try:
            cache = Path(__file__).resolve().parent.parent / "art-cache"
            got = fetch_cover(job.cues[0], cache, serial)
            if got:
                return Path(got)
        except Exception as e:
            r.log(f"cover art fetch skipped: {e}")
    return None


def stage_package(r, job):
    repo, s = job.repo, job.settings
    name = job.name
    made = []
    targets = []
    if s.build_linux:
        targets.append(("linux", repo / "build-release", name))
    if s.build_windows:
        targets.append(("windows", repo / "build-windows", name + ".exe"))
    for osname, bdir, exefile in targets:
        stage = repo / f"pkg-{osname}" / name
        if stage.parent.exists():
            shutil.rmtree(stage.parent)
        stage.mkdir(parents=True)
        shutil.copy2(bdir / exefile, stage / exefile)
        offer = bool(job.vulkan_backend.get(osname))
        (stage / "game.toml").write_text(package_game_toml(job, offer))
        shutil.copy2(bdir / "game_options.toml", stage / "game_options.toml")
        (stage / "README.md").write_text(package_readme(job, offer))
        shutil.copytree(bdir / "bios", stage / "bios")
        shutil.copytree(bdir / "assets", stage / "assets")
        # The game launcher shows cover art from artwork/<serial>/
        # next to the exe (recomp-ui launcher_artwork.h; "front" ranks
        # first). One folder per disc serial in the set.
        art_src = _job_artwork(r, job)
        if art_src:
            for p in job.probes:
                serial = p.get("serial")
                if serial:
                    adir = stage / "artwork" / serial
                    adir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(art_src,
                                 adir / f"front{art_src.suffix.lower()}")
            r.log(f"bundled cover art: {art_src.name}")
        shutil.copytree(bdir / "mods", stage / "mods")
        st = stage / "mods" / "state.toml"
        if st.exists():
            st.unlink()
        if s.ra_enabled:
            shutil.copy2(repo / "retroachievements.toml.example", stage)
            if s.ra_toml_path and Path(s.ra_toml_path).exists():
                shutil.copy2(s.ra_toml_path, stage / "retroachievements.toml")
        if not s.save_states:
            (stage / "config.ini").write_text(
                "[KeyMap]\nSaveStateMenu =\nRewind =\n")
        zip_path = job.root / f"{name}-{osname.capitalize()}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(stage.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(stage.parent))
        made.append(zip_path)
        r.log(f"packaged {zip_path} ({zip_path.stat().st_size} bytes)")
    return made


# ---------------------------------------------------------------- driver

STAGES = ["scaffold", "probe", "patch", "ra", "emitters", "generate",
          "build", "verify", "package"]


def run_job(job, log, stop_after=None):
    r = Runner(log)
    template = find_template(job.settings.template_dir)
    if template is None:
        log("no template project - downloading the framework "
            "(one-time, needs git + a connection)")
        template = ensure_framework_cache(log)
    if template is None:
        raise StageError("no template project found and the framework "
                         "download failed (check your connection and "
                         "that git is installed)")
    log(f"template project: {template}")
    patch_probe_subdir_boot(template, log)
    patch_overlay_draw_chain(template, log)
    patch_cdrom_getlocp(template, log)
    if len(job.cues) > 1:
        # Disc-set routing BEFORE any building: probe every disc and
        # check the set. One program -> fall through and build ONE
        # package. Different programs -> one full build per disc
        # (Resident Evil 2 pattern). Unclear -> needs review.
        tmp = Path(job.cues[0]).parent
        paths = []
        for i, cue in enumerate(job.cues):
            jp = tmp / f".recomp-tools-setcheck-{i}.json"
            probe_cue(r, cue, template, job.settings.players or 1,
                      json_out=jp)
            paths.append(jp)
        verdict = check_disc_set(r, paths, template)
        job.set_verdict = verdict
        route = classify_set(verdict)
        if route == "review":
            raise StageError(
                "multi-disc set could not be classified - needs review "
                f"(verdict: {json.dumps(verdict)[:300]})")
        if route == "split":
            log("disc set: the discs boot DIFFERENT programs - "
                "converting each disc as its own game")
            for i, cue in enumerate(job.cues):
                sub = Job(cues=[cue],
                          name=f"{job.name}Disc{i + 1}",
                          title=f"{job.title} (Disc {i + 1})",
                          work_dir=job.work_dir, settings=job.settings)
                log(f"=== disc {i + 1} of {len(job.cues)}: "
                    f"{sub.title} ===")
                run_job(sub, log)
            return "done"
        log("disc set verified: one program, one build")
    plan = []
    if job.settings.build_linux:
        plan.append("linux")
    if job.settings.build_windows:
        plan.append("windows")
    steps = [
        ("scaffold", lambda: (stage_scaffold(r, job, template),
                              patch_probe_subdir_boot(job.repo, log),
                              patch_overlay_draw_chain(job.repo, log),
                              patch_cdrom_getlocp(job.repo, log))),
        ("probe", lambda: stage_probe(r, job, template)),
        ("patch", lambda: stage_patch_cmake(r, job)),
        ("ra", lambda: stage_ra(r, job)),
        ("emitters", lambda: stage_emitters(r, job)),
        ("generate", lambda: stage_generate(r, job)),
        ("build", lambda: [stage_build(r, job, t) for t in plan]),
        ("verify", lambda: stage_verify(r, job)),
        ("package", lambda: stage_package(r, job)),
    ]
    for name, fn in steps:
        log(f"=== STAGE {name} ===")
        fn()
        if stop_after == name:
            log(f"stopped after {name} (requested)")
            return name
    return "done"


def job_from_dict(d):
    s = JobSettings(**d.get("settings", {}))
    return Job(cues=d["cues"], name=d["name"], title=d["title"],
               work_dir=d["work_dir"], settings=s,
               slug_override=d.get("slug", ""), art=d.get("art", ""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--cue", required=True)
    p.add_argument("--template", default="")
    p.add_argument("--players", type=int, default=1)
    p = sub.add_parser("run")
    p.add_argument("--job", required=True)
    p.add_argument("--stop-after", default=None)
    sub.add_parser("toolbox")
    a = ap.parse_args(argv)

    if a.cmd == "toolbox":
        print(json.dumps(toolbox_check(), indent=2))
        headers, glslc = vulkan_sdk_status()
        print(f"vulkan sdk (only needed for Vulkan builds): "
              f"headers={headers} glslc={glslc}")
        return 0
    if a.cmd == "probe":
        template = find_template(a.template)
        if template is None:
            print("no template project found", file=sys.stderr)
            return 2
        data = probe_cue(Runner(print), a.cue, template, a.players)
        print(json.dumps(data, indent=2))
        return 0
    if a.cmd == "run":
        job = job_from_dict(json.loads(Path(a.job).read_text()))
        logpath = job.root / "logs-engine.txt"
        job.root.mkdir(parents=True, exist_ok=True)

        def log(msg):
            print(msg, flush=True)
            with logpath.open("a") as f:
                f.write(str(msg) + "\n")

        try:
            result = run_job(job, log, a.stop_after)
            log(f"RESULT: {result}")
            return 0
        except StageError as ex:
            log(f"FAILED: {ex}")
            return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
