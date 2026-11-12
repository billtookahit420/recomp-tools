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
import signal
import subprocess
import sys
import threading
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
    "xdotool": "xdotool (overlay warm run input)",
    "Xvfb": "Xvfb (overlay warm run display)",
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
    "xdotool", "xvfb",
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
        try:
            res = subprocess.run(["git", "clone", "--depth", "1",
                                  "--recurse-submodules", url,
                                  str(dst)], capture_output=True,
                                 text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            log("framework clone timed out after 1800s: " + url)
            return None
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
                                 errors="replace", bufsize=1,
                                 start_new_session=True)
        except FileNotFoundError as ex:
            raise StageError(f"tool not found: {cmd[0]} ({ex})")
        # The deadline is enforced against the PROCESS, not the
        # output stream. The old loop read p.stdout to EOF and only
        # then called p.wait(timeout=...): a child that prints
        # nothing while it spins - or a dead child whose grandchild
        # inherited the pipe - kept the stream open forever and the
        # timeout could never fire (the GB-family silent-child
        # defect, fixed there in v1.0.1; this engine had the same
        # shape and was not in that audit). Output is now drained
        # on a thread; on timeout the child's whole process group
        # is killed, so nothing outlives the deadline.
        def _drain():
            try:
                for line in p.stdout:
                    self.log(line.rstrip("\n"))
            except (ValueError, OSError):
                pass

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                p.kill()
            p.wait()
            reader.join(timeout=5)
            raise StageError(f"timed out after {timeout}s: {cmd[0]}")
        reader.join(timeout=5)
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
    overlay_info: dict = field(default_factory=dict)
    # target ("linux"/"windows") -> {"shards": n, "tag": cg-tag,
    # "cache": bool} when the package carries the staged overlay
    # toolchain (+ pre-warmed shard cache). Filled by stage_overlay,
    # read by package_readme. Empty for games that load no code at
    # runtime - their packages are unchanged.
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

def patch_probe_subdir_boot(template, log=print, fail_loud=False):
    """Teach the framework probe to follow SYSTEM.CNF boot paths that
    point INTO a subdirectory (Tekken 3: BOOT = cdrom:\\TEKKEN3\\SLUS_004.02;1
    — the stock probe only searches the root directory and fails those
    discs with 'boot EXE not on disc'). Idempotent text patch, applied
    to the template / framework cache and to scaffolded projects."""
    try:
        probe = Path(template) / "psxrecomp" / "tools" / \
            "new_project_layout" / "probe_disc.py"
        if not probe.exists():
            if fail_loud:
                raise StageError(
                    f"probe_disc.py missing under {template} - cannot "
                    f"apply the subdirectory-boot probe patch")
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
            if fail_loud:
                raise StageError(
                    f"probe_disc.py in {template} does NOT take the "
                    f"subdirectory-boot patch: insertion anchor "
                    f"'def normalize_serial(' not found - this probe "
                    f"variant is not covered by this tool version, and "
                    f"discs whose SYSTEM.CNF boot path points into a "
                    f"subdirectory (e.g. Tekken 3) would be probed "
                    f"wrong. Failing the job instead of shipping it "
                    f"broken; update the tool or the template.")
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
            if fail_loud:
                raise StageError(
                    f"probe_disc.py in {template} does NOT take the "
                    f"subdirectory-boot patch: the stock 'boot EXE not "
                    f"on disc' raise was not found - this probe variant "
                    f"is not covered by this tool version, and discs "
                    f"whose SYSTEM.CNF boot path points into a "
                    f"subdirectory (e.g. Tekken 3) would be probed "
                    f"wrong. Failing the job instead of shipping it "
                    f"broken; update the tool or the template.")
            return
        text = text.replace(old_raise, new_raise, 1)
        probe.write_text(text)
        log("patched probe_disc.py: subdirectory boot paths (Tekken 3)")
    except StageError:
        raise
    except Exception as e:  # a patch failure must never break a run
        if fail_loud:
            raise StageError(
                f"subdirectory-boot probe patch failed on "
                f"{template}: {e}") from e
        log(f"probe subdir patch skipped: {e}")


PREFLIGHT_MARKER = "def build_preflight"

PREFLIGHT_OLD_0 = "from the boot EXE text.\n"
PREFLIGHT_NEW_0 = "from the boot EXE text.\n\nPre-flight: every probe carries a `preflight` section classifying the\ndisc (or, with --disc-set-cue / --disc-set-list, the whole set) against\nthe failure classes that used to surface only mid-build -- shell-hostile\ndisplay names, padded / oversize boot EXEs, multi-program disc sets.\nAdditive only: no existing field changes meaning.\n"

PREFLIGHT_OLD_1 = "SYNC = bytes([0x00] + [0xFF] * 10 + [0x00])\n\n\n"
PREFLIGHT_NEW_1 = "SYNC = bytes([0x00] + [0xFF] * 10 + [0x00])\n\n# ---------------------------------------------------------------------------\n# Pre-flight classification.\n#\n# Every probe result carries a `preflight` section: a one-line\n# classification plus named flags for the failure classes that used to be\n# discovered only mid-build. All of it derives from data the probe already\n# reads (display name, boot EXE file size vs its PS-X header, cue track\n# layout, and -- for a set -- each disc's boot-program hash).\n#\n#   classification          meaning\n#   CLEAN                   nothing flagged\n#   NEEDS-CLEAN-PATH        flag path-hazard: the display name contains a\n#                           shell-hostile character. The apostrophe is the\n#                           known killer (Tony Hawk's ..., Director's Cut):\n#                           it breaks the quoting of the wayland-scanner\n#                           commands in the Linux runtime build. Build\n#                           under an apostrophe-free path.\n#   PADDED-EXE              flag exe-padded: the boot EXE file is larger\n#                           than its header total (2048 + t_size) and every\n#                           overshoot byte is zero -- mastered-in padding\n#                           (Twisted Metal III, both revisions). The\n#                           converter EXE parser needs the tolerance\n#                           route for this class.\n#   EXE-OVERSIZE-CAUTION    flag exe-oversize-nonzero-tail: overshoot with\n#                           non-zero bytes. NOT assumed to be padding.\n#   EXE-UNDERSIZE-CAUTION   flag exe-undersize: file smaller than its\n#                           header total; truncated/corrupt until proven\n#                           otherwise.\n#   MULTI-EXE-UNSUPPORTED   flag multi-exe-set: the set's discs boot\n#                           different programs (the Xenogears class). The\n#                           framework cannot build N-program sets yet\n#                           (docs/MULTI_DISC.md P2); verify_disc_set.py\n#                           refuses them by name downstream.\n#\n# Track layout is reported informationally under `track_layout`: a\n# single-track cue is how some discs were mastered (the reference archive\n# matches Redump), so it is NOT a failure flag.\n\nSHELL_HOSTILE_CHARS = \"'\\\"`$!;&|<>\\\\\"\n\n_FLAG_TO_CLASSIFICATION = (\n    (\"multi-exe-set\", \"MULTI-EXE-UNSUPPORTED\"),\n    (\"exe-oversize-nonzero-tail\", \"EXE-OVERSIZE-CAUTION\"),\n    (\"exe-undersize\", \"EXE-UNDERSIZE-CAUTION\"),\n    (\"exe-padded\", \"PADDED-EXE\"),\n    (\"path-hazard\", \"NEEDS-CLEAN-PATH\"),\n)\n\n\ndef classify_preflight(flags: list[str]) -> str:\n    for flag, classification in _FLAG_TO_CLASSIFICATION:\n        if flag in flags:\n            return classification\n    return \"CLEAN\"\n\n\ndef path_hazard_chars(name: str) -> list[str]:\n    return sorted({c for c in name if c in SHELL_HOSTILE_CHARS or ord(c) < 0x20})\n\n\ndef build_preflight(*, display_name: str, track_count: int,\n                    exe_file_size: int, exe_header_total: int,\n                    tail_all_zero: bool | None) -> dict:\n    flags: list[str] = []\n    hazard = path_hazard_chars(display_name)\n    if hazard:\n        flags.append(\"path-hazard\")\n    overshoot = exe_file_size - exe_header_total\n    if overshoot > 0:\n        flags.append(\"exe-padded\" if tail_all_zero\n                     else \"exe-oversize-nonzero-tail\")\n    elif overshoot < 0:\n        flags.append(\"exe-undersize\")\n    pf: dict = {\n        \"classification\": classify_preflight(flags),\n        \"flags\": flags,\n        \"exe\": {\n            \"file_size\": exe_file_size,\n            \"header_total\": exe_header_total,\n            \"overshoot_bytes\": overshoot,\n            \"tail_all_zero\": tail_all_zero,\n        },\n        \"track_layout\": {\n            \"track_count\": track_count,\n            \"single_track\": track_count == 1,\n            \"note\": \"as-mastered layout, informational only -- \"\n                    \"not a failure flag\",\n        },\n        \"disc_set\": None,\n    }\n    if hazard:\n        pf[\"path_hazard_chars\"] = hazard\n    return pf\n\n\ndef refresh_path_hazard(preflight: dict, display_name: str) -> None:\n    \"\"\"Recompute the path-hazard flag after a --display-name override.\"\"\"\n    flags = [f for f in preflight.get(\"flags\", []) if f != \"path-hazard\"]\n    hazard = path_hazard_chars(display_name)\n    if hazard:\n        flags.append(\"path-hazard\")\n        preflight[\"path_hazard_chars\"] = hazard\n    else:\n        preflight.pop(\"path_hazard_chars\", None)\n    preflight[\"flags\"] = flags\n    preflight[\"classification\"] = classify_preflight(flags)\n\n\n# Program-identity fields, mirrored from verify_disc_set.py: if these\n# agree across a set's discs, one recompiled program covers the set.\nSET_PROGRAM_FIELDS = (\n    \"boot_exe_sha256\",\n    \"entry_pc\",\n    \"load_address\",\n    \"text_size\",\n    \"stack_base\",\n)\n\n\ndef apply_disc_set_preflight(p: \"DiscProbe\", extra_cues: list[Path]) -> None:\n    \"\"\"Identity-probe the other discs of a set and record in\n    p.preflight[\"disc_set\"] whether the set boots one program or several.\"\"\"\n    members = [p]\n    for cue in extra_cues:\n        members.append(probe(cue, identity_only=True))\n\n    def program_key(m: \"DiscProbe\") -> tuple:\n        return tuple(getattr(m, f) for f in SET_PROGRAM_FIELDS)\n\n    multi = len({program_key(m) for m in members}) > 1\n    p.preflight[\"disc_set\"] = {\n        \"disc_count\": len(members),\n        \"multi_exe\": multi,\n        \"programs\": [\n            {\n                \"cue_name\": m.cue_name,\n                \"serial\": m.serial,\n                \"boot_exe\": m.boot_exe,\n                \"boot_exe_sha256\": m.boot_exe_sha256,\n            }\n            for m in members\n        ],\n    }\n    if multi and \"multi-exe-set\" not in p.preflight[\"flags\"]:\n        p.preflight[\"flags\"].append(\"multi-exe-set\")\n        p.preflight[\"classification\"] = classify_preflight(\n            p.preflight[\"flags\"])\n\n\n"

PREFLIGHT_OLD_2 = "    notes: list[str] = field(default_factory=list)\n"
PREFLIGHT_NEW_2 = "    notes: list[str] = field(default_factory=list)\n    preflight: dict = field(default_factory=dict)\n"

PREFLIGHT_OLD_3 = "\n    return DiscProbe(\n"
PREFLIGHT_NEW_3 = "\n    header_total = EXE_HDR + t_size\n    overshoot = bsize - header_total\n    tail_all_zero: bool | None = None\n    if overshoot > 0:\n        tail_all_zero = not any(exe[header_total:])\n    preflight = build_preflight(\n        display_name=display,\n        track_count=len(tracks),\n        exe_file_size=bsize,\n        exe_header_total=header_total,\n        tail_all_zero=tail_all_zero,\n    )\n\n    return DiscProbe(\n"

PREFLIGHT_OLD_4 = "        notes=notes,\n"
PREFLIGHT_NEW_4 = "        notes=notes,\n        preflight=preflight,\n"

PREFLIGHT_OLD_5 = "    ap.add_argument(\n        \"--out-dir\",\n"
PREFLIGHT_NEW_5 = "    ap.add_argument(\n        \"--disc-set-cue\",\n        action=\"append\",\n        default=[],\n        help=\"another disc of the same set (its .cue), repeatable, in disc \"\n             \"order after the boot disc. Each is identity-probed and the \"\n             \"boot programs are compared into preflight.disc_set: if the \"\n             \"discs boot different programs the set classifies \"\n             \"MULTI-EXE-UNSUPPORTED (docs/MULTI_DISC.md).\",\n    )\n    ap.add_argument(\n        \"--disc-set-list\",\n        default=\"\",\n        help=\"file of additional set-disc cue paths, one per line, in disc \"\n             \"order. Preferred over repeated --disc-set-cue from a shell: \"\n             \"PS1 dump filenames contain spaces and parentheses.\",\n    )\n    ap.add_argument(\n        \"--out-dir\",\n"

PREFLIGHT_OLD_6 = "    if args.display_name:\n        p.display_name = args.display_name\n\n"
PREFLIGHT_NEW_6 = "    if args.display_name:\n        p.display_name = args.display_name\n        refresh_path_hazard(p.preflight, p.display_name)\n\n    set_cues = [c for c in (args.disc_set_cue or []) if c.strip()]\n    if args.disc_set_list:\n        lp = Path(args.disc_set_list)\n        if lp.is_file():\n            set_cues += [ln.strip()\n                         for ln in lp.read_text(encoding=\"utf-8\").splitlines()\n                         if ln.strip()]\n    if set_cues:\n        apply_disc_set_preflight(\n            p, [Path(c).expanduser() for c in set_cues]\n        )\n\n"

PREFLIGHT_OLD_7 = "\n    fp_note = f\" disc_fp="
PREFLIGHT_NEW_7 = "\n    pf = p.preflight or {}\n    pf_flags = pf.get(\"flags\") or []\n    print(\n        f\"  preflight: {pf.get('classification', '?')}\"\n        + (f\" flags={','.join(pf_flags)}\" if pf_flags else \"\"),\n        file=sys.stderr,\n    )\n\n    fp_note = f\" disc_fp="

def patch_probe_preflight(template, log=print, fail_loud=False):
    """Give every probe a `preflight` section classifying the disc (or,
    with the probe's --disc-set-cue / --disc-set-list options, the whole
    set) against the failure classes that used to surface only
    mid-build: shell-hostile display names (path-hazard ->
    NEEDS-CLEAN-PATH), a boot EXE larger than its PS-X header total
    with an all-zero tail (exe-padded -> PADDED-EXE; a non-zero tail is
    EXE-OVERSIZE-CAUTION, a short file EXE-UNDERSIZE-CAUTION), and
    multi-disc sets whose discs boot different programs (multi-exe-set
    -> MULTI-EXE-UNSUPPORTED). Track layout rides along as information
    only - a single-track cue is how some discs were mastered, never a
    failure flag. Additive only: no existing probe field changes.
    Idempotent text patch on the template / framework cache and on
    scaffolded projects (added 2026-11-12)."""
    try:
        probe = Path(template) / "psxrecomp" / "tools" / \
            "new_project_layout" / "probe_disc.py"
        if not probe.exists():
            if fail_loud:
                raise StageError(
                    f"probe_disc.py missing under {template} - cannot "
                    f"apply the pre-flight classification patch")
            return
        text = probe.read_text()
        if PREFLIGHT_MARKER in text:
            return
        pairs = [(PREFLIGHT_OLD_0, PREFLIGHT_NEW_0), (PREFLIGHT_OLD_1, PREFLIGHT_NEW_1), (PREFLIGHT_OLD_2, PREFLIGHT_NEW_2), (PREFLIGHT_OLD_3, PREFLIGHT_NEW_3), (PREFLIGHT_OLD_4, PREFLIGHT_NEW_4), (PREFLIGHT_OLD_5, PREFLIGHT_NEW_5), (PREFLIGHT_OLD_6, PREFLIGHT_NEW_6), (PREFLIGHT_OLD_7, PREFLIGHT_NEW_7)]
        missing = [i for i, (old, _n) in enumerate(pairs)
                   if old not in text]
        if missing:
            if fail_loud:
                raise StageError(
                    f"probe_disc.py in {template} does NOT take "
                    f"the pre-flight classification patch: "
                    f"{len(missing)} of {len(pairs)} anchors missing "
                    f"{missing} - this probe variant is not covered "
                    f"by this tool version, and its probes would carry "
                    f"no preflight section. Failing the job instead of "
                    f"shipping it broken; update the tool or the "
                    f"template.")
            return
        for old, new in pairs:
            text = text.replace(old, new, 1)
        probe.write_text(text)
        log("patched probe_disc.py: pre-flight classification "
            "(CLEAN / NEEDS-CLEAN-PATH / PADDED-EXE / "
            "EXE-OVERSIZE-CAUTION / EXE-UNDERSIZE-CAUTION / "
            "MULTI-EXE-UNSUPPORTED)")
    except StageError:
        raise
    except Exception as e:  # a patch failure must never break a run
        if fail_loud:
            raise StageError(
                f"pre-flight probe patch failed on "
                f"{template}: {e}") from e
        log(f"preflight probe patch skipped: {e}")


def patch_overlay_draw_chain(template, log=print, fail_loud=False):
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
            if fail_loud:
                raise StageError(
                    f"host_overlay.c missing under {template} - "
                    f"cannot apply the overlay draw-chain patch")
            return
        text = src.read_text()
        if "PSX_HOST_OVERLAY_DRAW_MAX" in text:
            return
        start = "/* ---- P2: overlay draw callback"
        end = "/* ---- Game HUD layer"
        if start not in text or end not in text:
            if fail_loud:
                raise StageError(
                    f"host_overlay.c in {template} does NOT take the "
                    f"overlay draw-chain patch: section anchors not "
                    f"found - this runtime variant is not covered by "
                    f"this tool version, and the game would ship with "
                    f"the in-game menu opening invisibly while "
                    f"RetroAchievements pop-ups replace its draw. "
                    f"Failing the job instead of shipping it broken; "
                    f"update the tool or the template.")
            return
        i = text.index(start)
        j = text.index(end)
        text = text[:i] + OVERLAY_DRAW_CHAIN_SPAN + text[j:]
        src.write_text(text)
        log("patched host_overlay.c: menu + RA share the overlay draw")
    except StageError:
        raise
    except Exception as e:  # a patch failure must never break a run
        if fail_loud:
            raise StageError(
                f"overlay draw-chain patch failed on {template}: "
                f"{e}") from e
        log(f"overlay draw-chain patch skipped: {e}")


OVERLAY_DRAW_CHAIN_SPAN = "/* ---- P2: overlay draw callback -------------------------------------------- */\n/* Registered on the main thread, read on the render / present thread. The\n * function and its context are published together as one immutable record\n * behind an atomic pointer, so a reader never pairs a live function with a\n * stale context.\n *\n * Lifetime: every call runs inside an in-flight count. Replacing or removing\n * the callback swaps the record and then waits until no call that could have\n * read the old record is still running, so when psx_host_overlay_set_draw_cb\n * returns, the previous fn will not run again and its ctx (and the GL objects\n * it draws with) may be freed by the caller. The old record is freed there\n * too. A change made from inside the callback itself does not wait (it would\n * wait for itself); that caller's own frame is the only one still using the\n * old ctx and it returns before the next frame reads the new record. */\n/* Up to two draw consumers share this layer: the in-game menu provider\n * registers first (at session setup) and feature overlays such as the\n * RetroAchievements pop-ups register afterwards. A one-callback record let\n * the later registration silently replace the menu's draw, leaving an open\n * but invisible menu, so the record now keeps both, keyed by function\n * pointer. Entries draw last-registered first, so the first registrant\n * (the menu) paints on top. A NULL fn keeps its historic meaning: the\n * caller is tearing the layer down and every entry is removed. */\n#define PSX_HOST_OVERLAY_DRAW_MAX 2\ntypedef struct {\n    PsxHostOverlayDrawFn fn;\n    void *ctx;\n} PsxHostOverlayDrawEntry;\ntypedef struct {\n    PsxHostOverlayDrawEntry entries[PSX_HOST_OVERLAY_DRAW_MAX];\n    int count;\n} PsxHostOverlayDraw;\n\nstatic _Atomic(PsxHostOverlayDraw *) s_draw = NULL;\nstatic atomic_flag s_draw_set_lock = ATOMIC_FLAG_INIT;\nstatic atomic_int s_draw_inflight = 0;\n#if defined(_MSC_VER)\nstatic __declspec(thread) int s_in_draw = 0;\n#else\nstatic _Thread_local int s_in_draw = 0;\n#endif\n\nstatic void draw_yield(void) {\n#if defined(_WIN32)\n    Sleep(0);\n#else\n    sched_yield();\n#endif\n}\n\nvoid psx_host_overlay_set_draw_cb(PsxHostOverlayDrawFn fn, void *ctx) {\n    PsxHostOverlayDraw *rec = NULL, *old;\n    /* Compose the successor from the current record under a lock so two\n     * registering threads cannot lose each other's entry. Readers never\n     * take this lock, so composing cannot deadlock a draw in flight; the\n     * in-flight wait + free below keep the lifetime contract above. */\n    while (atomic_flag_test_and_set(&s_draw_set_lock)) draw_yield();\n    old = atomic_load(&s_draw);\n    if (fn) {\n        int found = -1;\n        rec = (PsxHostOverlayDraw *)malloc(sizeof(*rec));\n        if (!rec) {\n            atomic_flag_clear(&s_draw_set_lock);\n            return;\n        }\n        rec->count = 0;\n        if (old) {\n            for (int i = 0; i < old->count; ++i) {\n                if (old->entries[i].fn == fn) found = rec->count;\n                rec->entries[rec->count++] = old->entries[i];\n            }\n        }\n        if (found >= 0) {\n            rec->entries[found].ctx = ctx; /* re-registering keeps its slot */\n        } else {\n            if (rec->count >= PSX_HOST_OVERLAY_DRAW_MAX)\n                rec->count = PSX_HOST_OVERLAY_DRAW_MAX - 1; /* evict newest other */\n            rec->entries[rec->count].fn = fn;\n            rec->entries[rec->count].ctx = ctx;\n            rec->count++;\n        }\n    }\n    atomic_store(&s_draw, rec); /* seq_cst: ordered with the readers' count */\n    atomic_flag_clear(&s_draw_set_lock);\n    if (s_in_draw) return; /* from inside the callback: see above; old stays (rare) */\n    while (atomic_load(&s_draw_inflight) > 0) draw_yield();\n    free(old);\n}\n\nint psx_host_overlay_has_draw(void) {\n    return atomic_load_explicit(&s_draw, memory_order_acquire) != NULL;\n}\n\nvoid psx_host_overlay_draw(int width, int height) {\n    PsxHostOverlayDraw *rec;\n    if (width <= 0 || height <= 0) return;\n    atomic_fetch_add(&s_draw_inflight, 1); /* seq_cst, before reading the record */\n    rec = atomic_load(&s_draw);\n    if (rec) {\n        s_in_draw = 1;\n        for (int i = rec->count - 1; i >= 0; --i)\n            if (rec->entries[i].fn)\n                rec->entries[i].fn(width, height, rec->entries[i].ctx);\n        s_in_draw = 0;\n    }\n    atomic_fetch_sub(&s_draw_inflight, 1);\n}\n\n"

def patch_cdrom_getlocp(template, log=print, fail_loud=False):
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
            if fail_loud:
                raise StageError(
                    f"cdrom.c missing under {template} - cannot "
                    f"apply the GetlocP patch")
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
            if fail_loud:
                raise StageError(
                    f"cdrom.c in {template} does NOT take the "
                    f"GetlocP patch: anchors not found - this runtime "
                    f"variant is not covered by this tool version, "
                    f"and games that poll GetlocP to confirm a seek "
                    f"into a CD music track (Tomb Raider 1/2) would "
                    f"ship hanging on a black screen. Failing the "
                    f"job instead of shipping it broken; update the "
                    f"tool or the template.")
            return
        text = text.replace(old_a, new_a, 1)
        text = text.replace(old_b, new_b, 1)
        text = text.replace(old_c, new_c, 1)
        src.write_text(text)
        log("patched cdrom.c: GetlocP reports the real track + pregap "
            "sub-Q (Tomb Raider 1/2 black-screen fix)")
    except StageError:
        raise
    except Exception as e:  # a patch failure must never break a run
        if fail_loud:
            raise StageError(
                f"cdrom GetlocP patch failed on {template}: "
                f"{e}") from e
        log(f"cdrom GetlocP patch skipped: {e}")


GPU_DEPTH24_OLD_A = "/* Depth24: note/query/reset the CPU→VRAM upload span tracked above. Used to\n * hide trailing RGB columns when a movie blit doesn't fill the full CRTC\n * width — MotK's Star Wars crawl leaves ~8px of stale VRAM on the right.\n * Only FB-class A0s (w >= 256 halfwords) grow the span; texture uploads must\n * not collapse it. During present-hold, ignore updates entirely. */\nstatic void depth24_note_upload(uint32_t x, uint32_t w) {\n    if (!(display_depth & 1u) || w < 256u) return;\n"

GPU_DEPTH24_NEW_A = '/* Depth24: note/query/reset the CPU→VRAM upload span tracked above. Used to\n * hide trailing RGB columns when a movie blit doesn\'t fill the full CRTC\n * width — MotK\'s Star Wars crawl leaves ~8px of stale VRAM on the right.\n * Every A0 counts, whatever its width: real STR players upload a frame as\n * many narrow strips (RE: Director\'s Cut blits its 320-px movies as 20\n * strips of 24 halfwords x 240), so a "FB-class only" (w >= 256) filter\n * never sees the movie\'s own coverage — the span then reflects any stray\n * wide upload instead (a 320-halfword 15-bit still shown between movies\n * capped RE\'s span at 320 halfwords = 213 RGB px, and the present blacked\n * out the right third of every subsequent movie frame). The span is a\n * max-extent, so stray uploads can only be outgrown by the movie\'s own\n * blits, which follow within a frame. During present-hold, ignore updates\n * entirely. */\nstatic void depth24_note_upload(uint32_t x, uint32_t w) {\n    if (!(display_depth & 1u)) return;\n'

GPU_DEPTH24_OLD_B = '    if (rows == 0u || w < 8u || margin == 0u || margin >= w)\n        return rows;\n    uint32_t start = w - margin;\n    uint32_t lim = gpu_depth24_rgb_limit(di->display_x, w);\n    if (lim > 0u && lim < w && lim < start)\n        start = lim;\n'

GPU_DEPTH24_NEW_B = "    if (rows == 0u || w < 8u)\n        return rows;\n    uint32_t lim = gpu_depth24_rgb_limit(di->display_x, w);\n    uint32_t start;\n    if (lim >= w) {\n        /* The guest's uploads cover the full CRTC width: no column is\n         * stale, so blank nothing. (Previously the margin fallback still\n         * clipped the last `margin` real columns of a fully covered\n         * frame — RE: Director's Cut's movies lost their right 8 px.) */\n        return rows;\n    } else if (lim > 0u) {\n        /* Blank exactly the uncovered tail past the covered span. */\n        start = lim;\n    } else {\n        /* No coverage information yet: legacy margin-only blanking. */\n        if (margin == 0u || margin >= w)\n            return rows;\n        start = w - margin;\n    }\n"
def patch_gpu_depth24_span(template, log=print, fail_loud=False):
    """Fix the runtime's depth24 (24-bit movie) upload-span tracker.
    Upstream gpu.c's tracker only counted CPU->VRAM uploads at least
    256 halfwords wide ("FB-class"), but real STR players upload each
    movie frame as many narrow vertical strips (Resident Evil:
    Director's Cut blits its 320-px movies as 20 strips of 24x240), so
    the movie's own coverage never registered - the span instead
    reflected a stray wide still shown between movies (320 halfwords =
    213 RGB px), and the depth24 staging pass blacked out every movie
    frame past that limit: the right third of RE DC's live-action
    intro, and the same crop in Mega Man 8 (same Capcom strip-upload
    pattern). The staging pass also clipped the last `margin` columns
    of even a fully covered frame via its no-info fallback (RE DC's
    logo sequence lost its right 8 px). The patch counts every upload
    while depth24 is on (the span is a max-extent, so the movie's own
    strips outgrow any stray still within one frame) and makes the
    staging blank nothing at full coverage, otherwise exactly the
    uncovered tail - the narrow-blit case the fill was built for (the
    framework's MotK crawl) still blanks its stale tail.
    Idempotent text patch on the template / framework cache and on
    scaffolded projects (found 2026-11-05, RE DC verified on camera:
    intro + logo + story FMVs full width, OpenGL and Software)."""
    try:
        src = Path(template) / "psxrecomp" / "runtime" / "src" / \
            "gpu.c"
        if not src.exists():
            if fail_loud:
                raise StageError(
                    f"gpu.c missing under {template} - cannot "
                    f"apply the depth24 span patch")
            return
        text = src.read_text()
        if "Every A0 counts, whatever its width" in text:
            return
        if GPU_DEPTH24_OLD_A not in text or GPU_DEPTH24_OLD_B not in text:
            if fail_loud:
                raise StageError(
                    f"gpu.c in {template} does NOT take the depth24 "
                    f"span patch: anchors not found - this runtime "
                    f"variant is not covered by this tool version, "
                    f"and 24-bit movies uploaded as narrow strips "
                    f"(Resident Evil: Director's Cut, Mega Man 8) "
                    f"would ship cropped. Failing the job instead "
                    f"of shipping it broken; update the tool or the "
                    f"template.")
            return
        text = text.replace(GPU_DEPTH24_OLD_A, GPU_DEPTH24_NEW_A, 1)
        text = text.replace(GPU_DEPTH24_OLD_B, GPU_DEPTH24_NEW_B, 1)
        src.write_text(text)
        log("patched gpu.c: depth24 span counts every upload; staging "
            "blanks only the uncovered tail (RE DC / Mega Man 8 FMV "
            "crop fix)")
    except StageError:
        raise
    except Exception as e:  # a patch failure must never break a run
        if fail_loud:
            raise StageError(
                f"gpu depth24 span patch failed on {template}: "
                f"{e}") from e
        log(f"gpu depth24 span patch skipped: {e}")


SW_GRID_MARKER = "Guest-grid hi-res triangle fill"

SW_GRID_OLD_0 = '/* Color modulation for textured primitives */\nstatic uint8_t g_mod_r, g_mod_g, g_mod_b;\nstatic int g_raw_texture; /* 1 = skip modulation */\n\n/* Texture filtering: 0 = nearest (native PSX), 1 = bilinear. */\nstatic int g_texture_filter = 0;\n'

SW_GRID_NEW_0 = '/* Color modulation for textured primitives */\nstatic uint8_t g_mod_r, g_mod_g, g_mod_b;\nstatic int g_raw_texture; /* 1 = skip modulation */\n\n/* Coverage visualization (diagnostic only, PSX_COV_VIZ=1): in hi-res\n * targets, textured triangles are filled with a flat colour hashed from\n * the primitive\'s draw index instead of their texture, so a capture of\n * the hi-res buffer shows exactly which primitive covered each fragment\n * (per-fragment prim-id map). Native VRAM rendering is untouched. */\nstatic uint32_t g_cov_prim = 0;\nstatic int cov_viz_on(void) {\n    static int v = -1;\n    if (v < 0) { const char *e = getenv("PSX_COV_VIZ"); v = (e && e[0] && e[0] != \'0\') ? 1 : 0; }\n    return v;\n}\nstatic uint16_t cov_colour(uint32_t id) {\n    uint32_t h = id * 0x9E3779B1u;\n    h ^= h >> 15; h *= 0x85EBCA6Bu; h ^= h >> 13;\n    int r = 12 + (int)(h & 0x13u);\n    int g = 12 + (int)((h >> 5) & 0x13u);\n    int b = 12 + (int)((h >> 10) & 0x13u);\n    return (uint16_t)(r | (g << 5) | (b << 10));\n}\n\n/* Texture filtering: 0 = nearest (native PSX), 1 = bilinear. */\nstatic int g_texture_filter = 0;\n'

SW_GRID_OLD_1 = '}\n\n/* ------------------------------------------------------------------ */\n/* Flat-shaded triangle (scanline rasterization)                      */\n/* ------------------------------------------------------------------ */\n\n'

SW_GRID_NEW_1 = "}\n\n/* ------------------------------------------------------------------ */\n/* Guest-grid hi-res triangle fill                                    */\n/*                                                                    */\n/* Coverage is decided in GUEST space with the same biased edge DDA   */\n/* the native pass uses (psx_triangle_edges_at_y): the guest pixels   */\n/* a primitive owns in a hi-res/wide target are exactly the ones it   */\n/* fills in native VRAM, so abutting spans tile with no gaps and no   */\n/* double-claims at any internal scale. Each owned guest pixel's SxS  */\n/* block is coloured by sampling the primitive's barycentric          */\n/* attribute field at the sub-pixel centres — hi-res sampling inside  */\n/* the guest pixel — so texture detail is preserved while coverage    */\n/* agrees with the guest grid the geometry was authored for.          */\n/*                                                                    */\n/* Eligible only when the primitive's geometry IS the guest grid:     */\n/* no precise (PGXP) vertices, not in faithful-authority mode, and    */\n/* target scale > 1 (at 1x the native pass already presents). The     */\n/* legacy scaled-vertex path serves every other case unchanged.       */\n/* ------------------------------------------------------------------ */\n\nenum { GG_FLAT = 0, GG_GOURAUD = 1, GG_TEX = 2, GG_SHADED_TEX = 3 };\n\ntypedef struct {\n    int   x[3], y[3];        /* guest-space vertices (wide: x has +dx) */\n    float u[3], v[3];        /* texture coords (q-weighted if perspective) */\n    float q[3];              /* perspective weights (1.0f when unused) */\n    float r[3], g[3], b[3];  /* per-vertex colours, 5-bit space */\n    int   kind;\n    uint16_t flat_color;\n    uint16_t clut_x, clut_y, texpage;\n    int   raw_texture;\n    int   perspective;\n} GridTri;\n\nstatic int grid_eligible(const RTarget *t) {\n    return t->s > 1 && !g_precise_valid && !g_faithful_authority;\n}\n\nstatic void grid_raster_triangle(const RTarget *t, const GridTri *gin) {\n    const int S = t->s;\n    GridTri g = *gin;\n\n    /* Sort by Y, permuting every attribute array in sync (same scheme\n     * as the scanline rasterizers). */\n    static const int pairs[3][2] = { {0, 1}, {0, 2}, {1, 2} };\n    for (int p = 0; p < 3; p++) {\n        int a = pairs[p][0], b = pairs[p][1];\n        if (g.y[a] > g.y[b]) {\n            int ti; float tf;\n            ti = g.x[a]; g.x[a] = g.x[b]; g.x[b] = ti;\n            ti = g.y[a]; g.y[a] = g.y[b]; g.y[b] = ti;\n            tf = g.u[a]; g.u[a] = g.u[b]; g.u[b] = tf;\n            tf = g.v[a]; g.v[a] = g.v[b]; g.v[b] = tf;\n            tf = g.q[a]; g.q[a] = g.q[b]; g.q[b] = tf;\n            tf = g.r[a]; g.r[a] = g.r[b]; g.r[b] = tf;\n            tf = g.g[a]; g.g[a] = g.g[b]; g.g[b] = tf;\n            tf = g.b[a]; g.b[a] = g.b[b]; g.b[b] = tf;\n        }\n    }\n    const int x0 = g.x[0], y0 = g.y[0];\n    const int x1 = g.x[1], y1 = g.y[1];\n    const int x2 = g.x[2], y2 = g.y[2];\n\n    if (y2 - y0 == 0) return;\n\n    /* Barycentric denominator over the guest-space triangle. */\n    const float denom = (float)(y1 - y2) * (float)(x0 - x2)\n                      + (float)(x2 - x1) * (float)(y0 - y2);\n    if (denom == 0.0f) return;\n    const float inv_denom = 1.0f / denom;\n    const float e1x = (float)(y1 - y2), e1y = (float)(x2 - x1);\n    const float e2x = (float)(y2 - y0), e2y = (float)(x0 - x2);\n\n    /* Guest-space clip rect (target clip is in buffer space; for both\n     * hi-res and wide targets it is the guest clip scaled by S). */\n    const int gcx1 = t->cx1 / S, gcx2 = t->cx2 / S;\n    const int gcy1 = t->cy1 / S, gcy2 = t->cy2 / S;\n\n    const int viz = cov_viz_on();\n\n    for (int y = y0; y < y2; y++) {\n        if (y < gcy1 || y > gcy2) continue;\n\n        int xa, xb;\n        psx_triangle_edges_at_y(x0, y0, x1, y1, x2, y2, y, &xa, &xb);\n        if (xa > xb) { int tt = xa; xa = xb; xb = tt; }\n\n        const int sx = max_i(xa, gcx1);\n        const int ex = min_i(xb, gcx2 + 1);\n\n        for (int gx = sx; gx < ex; gx++) {\n            for (int dy = 0; dy < S; dy++) {\n                const int Y = y * S + dy;\n                const float py = (float)y + ((float)dy + 0.5f) / (float)S;\n                for (int dx = 0; dx < S; dx++) {\n                    const int X = gx * S + dx;\n                    if (viz) {\n                        put_opaque(t, X, Y, cov_colour(g_cov_prim));\n                        continue;\n                    }\n                    if (g.kind == GG_FLAT) {\n                        put_opaque(t, X, Y, g.flat_color);\n                        continue;\n                    }\n                    const float px = (float)gx + ((float)dx + 0.5f) / (float)S;\n                    const float l0 = (e1x * (px - (float)x2)\n                                    + e1y * (py - (float)y2)) * inv_denom;\n                    const float l1 = (e2x * (px - (float)x2)\n                                    + e2y * (py - (float)y2)) * inv_denom;\n                    const float l2 = 1.0f - l0 - l1;\n\n                    if (g.kind == GG_GOURAUD) {\n                        int r = (int)(l0 * g.r[0] + l1 * g.r[1] + l2 * g.r[2]);\n                        int gg = (int)(l0 * g.g[0] + l1 * g.g[1] + l2 * g.g[2]);\n                        int b = (int)(l0 * g.b[0] + l1 * g.b[1] + l2 * g.b[2]);\n                        if (r < 0) r = 0; if (r > 31) r = 31;\n                        if (gg < 0) gg = 0; if (gg > 31) gg = 31;\n                        if (b < 0) b = 0; if (b > 31) b = 31;\n                        put_opaque(t, X, Y,\n                                   (uint16_t)(r | (gg << 5) | (b << 10)));\n                        continue;\n                    }\n\n                    /* Textured kinds. */\n                    float fu, fv;\n                    if (g.perspective) {\n                        const float q = l0 * g.q[0] + l1 * g.q[1] + l2 * g.q[2];\n                        if (q > 1.0e-12f) {\n                            fu = (l0 * g.u[0] + l1 * g.u[1] + l2 * g.u[2]) / q;\n                            fv = (l0 * g.v[0] + l1 * g.v[1] + l2 * g.v[2]) / q;\n                        } else {\n                            fu = l0 * (g.u[0] / g.q[0]) + l1 * (g.u[1] / g.q[1])\n                               + l2 * (g.u[2] / g.q[2]);\n                            fv = l0 * (g.v[0] / g.q[0]) + l1 * (g.v[1] / g.q[1])\n                               + l2 * (g.v[2] / g.q[2]);\n                        }\n                    } else {\n                        fu = l0 * g.u[0] + l1 * g.u[1] + l2 * g.u[2];\n                        fv = l0 * g.v[0] + l1 * g.v[1] + l2 * g.v[2];\n                    }\n                    int mr = g_mod_r, mg = g_mod_g, mb = g_mod_b;\n                    if (g.kind == GG_SHADED_TEX) {\n                        mr = (int)(l0 * g.r[0] + l1 * g.r[1] + l2 * g.r[2]);\n                        mg = (int)(l0 * g.g[0] + l1 * g.g[1] + l2 * g.g[2]);\n                        mb = (int)(l0 * g.b[0] + l1 * g.b[1] + l2 * g.b[2]);\n                        if (mr < 0) mr = 0; if (mr > 31) mr = 31;\n                        if (mg < 0) mg = 0; if (mg > 31) mg = 31;\n                        if (mb < 0) mb = 0; if (mb > 31) mb = 31;\n                    }\n                    if (g_texture_filter) {\n                        const float sh = bilinear_center_shift_for_target(t);\n                        uint16_t texel = texel_fetch_bilinear(fu + sh, fv + sh,\n                                                              g.texpage,\n                                                              g.clut_x, g.clut_y);\n                        put_textured(t, X, Y, texel, mr, mg, mb, g.raw_texture);\n                    } else {\n                        uint16_t texel = texel_fetch((int)fu & 0xFF, (int)fv & 0xFF,\n                                                     g.texpage, g.clut_x, g.clut_y);\n                        put_textured(t, X, Y, texel, mr, mg, mb, g.raw_texture);\n                    }\n                }\n            }\n        }\n    }\n}\n\n/* ------------------------------------------------------------------ */\n/* Flat-shaded triangle (scanline rasterization)                      */\n/* ------------------------------------------------------------------ */\n\n"

SW_GRID_OLD_2 = '    if (g_hr) {\n        int s = g_scale;\n        RTarget hr = rt_hires();\n        raster_flat_triangle(&hr,\n            precise_scaled(0,0,x0,s), precise_scaled(1,0,y0,s),\n            precise_scaled(0,1,x1,s), precise_scaled(1,1,y1,s),\n            precise_scaled(0,2,x2,s), precise_scaled(1,2,y2,s), color);\n    }\n    if (g_wide_cur) {\n        int s = g_scale, dx = wide_dx();\n        RTarget wt = rt_wide();\n        WideBd bd = wide_bd_get();\n        raster_flat_triangle(&wt,\n            precise_wide_x(0,x0,s,dx,&bd), precise_scaled(1,0,y0,s),\n            precise_wide_x(1,x1,s,dx,&bd), precise_scaled(1,1,y1,s),\n            precise_wide_x(2,x2,s,dx,&bd), precise_scaled(1,2,y2,s), color);\n    }\n    precise_consumed();\n}\n'

SW_GRID_NEW_2 = '    if (g_hr) {\n        int s = g_scale;\n        RTarget hr = rt_hires();\n        if (grid_eligible(&hr)) {\n            GridTri gt;\n            memset(&gt, 0, sizeof gt);\n            gt.kind = GG_FLAT;\n            gt.flat_color = color;\n            gt.x[0] = x0; gt.y[0] = y0;\n            gt.x[1] = x1; gt.y[1] = y1;\n            gt.x[2] = x2; gt.y[2] = y2;\n            grid_raster_triangle(&hr, &gt);\n        } else {\n            raster_flat_triangle(&hr,\n                precise_scaled(0,0,x0,s), precise_scaled(1,0,y0,s),\n                precise_scaled(0,1,x1,s), precise_scaled(1,1,y1,s),\n                precise_scaled(0,2,x2,s), precise_scaled(1,2,y2,s), color);\n        }\n    }\n    if (g_wide_cur) {\n        int s = g_scale, dx = wide_dx();\n        RTarget wt = rt_wide();\n        WideBd bd = wide_bd_get();\n        if (!bd.on && grid_eligible(&wt)) {\n            GridTri gt;\n            memset(&gt, 0, sizeof gt);\n            gt.kind = GG_FLAT;\n            gt.flat_color = color;\n            gt.x[0] = x0 + dx; gt.y[0] = y0;\n            gt.x[1] = x1 + dx; gt.y[1] = y1;\n            gt.x[2] = x2 + dx; gt.y[2] = y2;\n            grid_raster_triangle(&wt, &gt);\n        } else {\n            raster_flat_triangle(&wt,\n                precise_wide_x(0,x0,s,dx,&bd), precise_scaled(1,0,y0,s),\n                precise_wide_x(1,x1,s,dx,&bd), precise_scaled(1,1,y1,s),\n                precise_wide_x(2,x2,s,dx,&bd), precise_scaled(1,2,y2,s), color);\n        }\n    }\n    precise_consumed();\n}\n'

SW_GRID_OLD_3 = '    }\n}\n\nvoid sw_draw_gouraud_triangle(int x0, int y0, uint16_t c0,\n                              int x1, int y1, uint16_t c1,\n                              int x2, int y2, uint16_t c2) {\n'

SW_GRID_NEW_3 = '    }\n}\n\nstatic void grid_fill_gouraud(GridTri *gt, int x0, int y0, uint16_t c0,\n                              int x1, int y1, uint16_t c1,\n                              int x2, int y2, uint16_t c2, int dx) {\n    memset(gt, 0, sizeof *gt);\n    gt->kind = GG_GOURAUD;\n    gt->x[0] = x0 + dx; gt->y[0] = y0;\n    gt->x[1] = x1 + dx; gt->y[1] = y1;\n    gt->x[2] = x2 + dx; gt->y[2] = y2;\n    gt->r[0] = (float)((c0 >>  0) & 0x1F);\n    gt->g[0] = (float)((c0 >>  5) & 0x1F);\n    gt->b[0] = (float)((c0 >> 10) & 0x1F);\n    gt->r[1] = (float)((c1 >>  0) & 0x1F);\n    gt->g[1] = (float)((c1 >>  5) & 0x1F);\n    gt->b[1] = (float)((c1 >> 10) & 0x1F);\n    gt->r[2] = (float)((c2 >>  0) & 0x1F);\n    gt->g[2] = (float)((c2 >>  5) & 0x1F);\n    gt->b[2] = (float)((c2 >> 10) & 0x1F);\n}\n\nvoid sw_draw_gouraud_triangle(int x0, int y0, uint16_t c0,\n                              int x1, int y1, uint16_t c1,\n                              int x2, int y2, uint16_t c2) {\n'

SW_GRID_OLD_4 = '    if (g_hr) {\n        int s = g_scale;\n        RTarget hr = rt_hires();\n        raster_gouraud_triangle(&hr,\n            precise_scaled(0,0,x0,s), precise_scaled(1,0,y0,s), c0,\n            precise_scaled(0,1,x1,s), precise_scaled(1,1,y1,s), c1,\n            precise_scaled(0,2,x2,s), precise_scaled(1,2,y2,s), c2);\n    }\n    if (g_wide_cur) {\n        int s = g_scale, dx = wide_dx();\n        RTarget wt = rt_wide();\n        WideBd bd = wide_bd_get();\n        raster_gouraud_triangle(&wt,\n            precise_wide_x(0,x0,s,dx,&bd), precise_scaled(1,0,y0,s), c0,\n            precise_wide_x(1,x1,s,dx,&bd), precise_scaled(1,1,y1,s), c1,\n            precise_wide_x(2,x2,s,dx,&bd), precise_scaled(1,2,y2,s), c2);\n    }\n    precise_consumed();\n}\n'

SW_GRID_NEW_4 = '    if (g_hr) {\n        int s = g_scale;\n        RTarget hr = rt_hires();\n        if (grid_eligible(&hr)) {\n            GridTri gt;\n            grid_fill_gouraud(&gt, x0, y0, c0, x1, y1, c1, x2, y2, c2, 0);\n            grid_raster_triangle(&hr, &gt);\n        } else {\n            raster_gouraud_triangle(&hr,\n                precise_scaled(0,0,x0,s), precise_scaled(1,0,y0,s), c0,\n                precise_scaled(0,1,x1,s), precise_scaled(1,1,y1,s), c1,\n                precise_scaled(0,2,x2,s), precise_scaled(1,2,y2,s), c2);\n        }\n    }\n    if (g_wide_cur) {\n        int s = g_scale, dx = wide_dx();\n        RTarget wt = rt_wide();\n        WideBd bd = wide_bd_get();\n        if (!bd.on && grid_eligible(&wt)) {\n            GridTri gt;\n            grid_fill_gouraud(&gt, x0, y0, c0, x1, y1, c1, x2, y2, c2, dx);\n            grid_raster_triangle(&wt, &gt);\n        } else {\n            raster_gouraud_triangle(&wt,\n                precise_wide_x(0,x0,s,dx,&bd), precise_scaled(1,0,y0,s), c0,\n                precise_wide_x(1,x1,s,dx,&bd), precise_scaled(1,1,y1,s), c1,\n                precise_wide_x(2,x2,s,dx,&bd), precise_scaled(1,2,y2,s), c2);\n        }\n    }\n    precise_consumed();\n}\n'

SW_GRID_OLD_5 = '            tf = vqa; vqa = vqb; vqb = tf;\n        }\n\n        int span = xb - xa;\n        if (span == 0) span = 1;\n\n        int sx = max_i(xa, t->cx1);\n        int ex = min_i(xb, t->cx2 + 1);\n\n        for (int x = sx; x < ex; x++) {\n            float t_val = (float)(x - xa) / (float)span;\n            float fu = ua + (ub - ua) * t_val;\n            float fv = va + (vb - va) * t_val;\n            if (perspective) {\n                float q = qa + (qb - qa) * t_val;\n                if (q > 1.0e-12f) {\n                    fu = (uqa + (uqb - uqa) * t_val) / q;\n                    fv = (vqa + (vqb - vqa) * t_val) / q;\n                }\n            }\n'

SW_GRID_NEW_5 = '            tf = vqa; vqa = vqb; vqb = tf;\n        }\n\n        int span = xb - xa;\n        if (span == 0) span = 1;\n\n        int sx = max_i(xa, t->cx1);\n        int ex = min_i(xb, t->cx2 + 1);\n\n        for (int x = sx; x < ex; x++) {\n            if (cov_viz_on() && t->s > 1) {\n                put_opaque(t, x, y, cov_colour(g_cov_prim));\n                continue;\n            }\n            float t_val = (float)(x - xa) / (float)span;\n            float fu = ua + (ub - ua) * t_val;\n            float fv = va + (vb - va) * t_val;\n            if (perspective) {\n                float q = qa + (qb - qa) * t_val;\n                if (q > 1.0e-12f) {\n                    fu = (uqa + (uqb - uqa) * t_val) / q;\n                    fv = (vqa + (vqb - vqa) * t_val) / q;\n                }\n            }\n'

SW_GRID_OLD_6 = '    }\n}\n\nvoid sw_draw_textured_triangle(int x0, int y0, int u0, int v0,\n                               int x1, int y1, int u1, int v1,\n                               int x2, int y2, int u2, int v2,\n'

SW_GRID_NEW_6 = '    }\n}\n\nstatic void grid_fill_textured(GridTri *gt,\n                               int x0, int y0, int u0, int v0,\n                               int x1, int y1, int u1, int v1,\n                               int x2, int y2, int u2, int v2,\n                               uint16_t clut_x, uint16_t clut_y,\n                               uint16_t texpage, int kind, int raw_texture,\n                               int dx, const int *rgb9) {\n    memset(gt, 0, sizeof *gt);\n    gt->kind = kind;\n    gt->x[0] = x0 + dx; gt->y[0] = y0;\n    gt->x[1] = x1 + dx; gt->y[1] = y1;\n    gt->x[2] = x2 + dx; gt->y[2] = y2;\n    gt->perspective = g_perspective_valid;\n    const float q0 = g_perspective_valid ? g_perspective_q[0] : 1.0f;\n    const float q1 = g_perspective_valid ? g_perspective_q[1] : 1.0f;\n    const float q2 = g_perspective_valid ? g_perspective_q[2] : 1.0f;\n    gt->u[0] = (float)u0 * q0; gt->v[0] = (float)v0 * q0; gt->q[0] = q0;\n    gt->u[1] = (float)u1 * q1; gt->v[1] = (float)v1 * q1; gt->q[1] = q1;\n    gt->u[2] = (float)u2 * q2; gt->v[2] = (float)v2 * q2; gt->q[2] = q2;\n    gt->clut_x = clut_x; gt->clut_y = clut_y; gt->texpage = texpage;\n    gt->raw_texture = raw_texture;\n    if (rgb9) {\n        gt->r[0] = (float)rgb9[0]; gt->g[0] = (float)rgb9[1]; gt->b[0] = (float)rgb9[2];\n        gt->r[1] = (float)rgb9[3]; gt->g[1] = (float)rgb9[4]; gt->b[1] = (float)rgb9[5];\n        gt->r[2] = (float)rgb9[6]; gt->g[2] = (float)rgb9[7]; gt->b[2] = (float)rgb9[8];\n    }\n}\n\nvoid sw_draw_textured_triangle(int x0, int y0, int u0, int v0,\n                               int x1, int y1, int u1, int v1,\n                               int x2, int y2, int u2, int v2,\n'

SW_GRID_OLD_7 = '                               uint16_t texpage) {\n    int64_t area2 = (int64_t)(x1 - x0) * (int64_t)(y2 - y0)\n                  - (int64_t)(x2 - x0) * (int64_t)(y1 - y0);\n    if (area2 == 0) { precise_consumed(); return; }\n\n    if (g_texture_filter) {\n        int xs[3] = {x0,x1,x2}, ys[3] = {y0,y1,y2};\n        int us[3] = {u0,u1,u2}, vs[3] = {v0,v1,v2};\n'

SW_GRID_NEW_7 = '                               uint16_t texpage) {\n    int64_t area2 = (int64_t)(x1 - x0) * (int64_t)(y2 - y0)\n                  - (int64_t)(x2 - x0) * (int64_t)(y1 - y0);\n    if (area2 == 0) { precise_consumed(); return; }\n    g_cov_prim++;\n\n    if (g_texture_filter) {\n        int xs[3] = {x0,x1,x2}, ys[3] = {y0,y1,y2};\n        int us[3] = {u0,u1,u2}, vs[3] = {v0,v1,v2};\n'

SW_GRID_OLD_8 = '    if (g_hr) {\n        int s = g_scale;\n        RTarget hr = rt_hires();\n        raster_textured_triangle(&hr,\n                                 precise_scaled(0,0,x0,s), precise_scaled(1,0,y0,s), u0, v0,\n                                 precise_scaled(0,1,x1,s), precise_scaled(1,1,y1,s), u1, v1,\n                                 precise_scaled(0,2,x2,s), precise_scaled(1,2,y2,s), u2, v2,\n                                 clut_x, clut_y, texpage, g_perspective_valid,\n                                 g_perspective_q[0], g_perspective_q[1], g_perspective_q[2]);\n    }\n    if (g_wide_cur) {\n        int s = g_scale, dx = wide_dx();\n        RTarget wt = rt_wide();\n        WideBd bd = wide_bd_get();\n        raster_textured_triangle(&wt,\n                                 precise_wide_x(0,x0,s,dx,&bd), precise_scaled(1,0,y0,s), u0, v0,\n                                 precise_wide_x(1,x1,s,dx,&bd), precise_scaled(1,1,y1,s), u1, v1,\n                                 precise_wide_x(2,x2,s,dx,&bd), precise_scaled(1,2,y2,s), u2, v2,\n                                 clut_x, clut_y, texpage, g_perspective_valid,\n                                 g_perspective_q[0], g_perspective_q[1], g_perspective_q[2]);\n    }\n    precise_consumed();\n}\n'

SW_GRID_NEW_8 = '    if (g_hr) {\n        int s = g_scale;\n        RTarget hr = rt_hires();\n        if (grid_eligible(&hr)) {\n            GridTri gt;\n            grid_fill_textured(&gt, x0, y0, u0, v0, x1, y1, u1, v1,\n                               x2, y2, u2, v2, clut_x, clut_y, texpage,\n                               GG_TEX, g_raw_texture, 0, 0);\n            grid_raster_triangle(&hr, &gt);\n        } else {\n            raster_textured_triangle(&hr,\n                                 precise_scaled(0,0,x0,s), precise_scaled(1,0,y0,s), u0, v0,\n                                 precise_scaled(0,1,x1,s), precise_scaled(1,1,y1,s), u1, v1,\n                                 precise_scaled(0,2,x2,s), precise_scaled(1,2,y2,s), u2, v2,\n                                 clut_x, clut_y, texpage, g_perspective_valid,\n                                 g_perspective_q[0], g_perspective_q[1], g_perspective_q[2]);\n        }\n    }\n    if (g_wide_cur) {\n        int s = g_scale, dx = wide_dx();\n        RTarget wt = rt_wide();\n        WideBd bd = wide_bd_get();\n        if (!bd.on && grid_eligible(&wt)) {\n            GridTri gt;\n            grid_fill_textured(&gt, x0, y0, u0, v0, x1, y1, u1, v1,\n                               x2, y2, u2, v2, clut_x, clut_y, texpage,\n                               GG_TEX, g_raw_texture, dx, 0);\n            grid_raster_triangle(&wt, &gt);\n        } else {\n            raster_textured_triangle(&wt,\n                                 precise_wide_x(0,x0,s,dx,&bd), precise_scaled(1,0,y0,s), u0, v0,\n                                 precise_wide_x(1,x1,s,dx,&bd), precise_scaled(1,1,y1,s), u1, v1,\n                                 precise_wide_x(2,x2,s,dx,&bd), precise_scaled(1,2,y2,s), u2, v2,\n                                 clut_x, clut_y, texpage, g_perspective_valid,\n                                 g_perspective_q[0], g_perspective_q[1], g_perspective_q[2]);\n        }\n    }\n    precise_consumed();\n}\n'

SW_GRID_OLD_9 = '            tf = ba; ba = bb; bb = tf;\n        }\n\n        int span = xb - xa;\n        if (span == 0) span = 1;\n\n        int sx = max_i(xa, t->cx1);\n        int ex = min_i(xb, t->cx2 + 1);\n\n        for (int x = sx; x < ex; x++) {\n            float t_val = (float)(x - xa) / (float)span;\n            float fu = ua + (ub - ua) * t_val;\n            float fv = va + (vb - va) * t_val;\n            if (perspective) {\n                float q = qa + (qb - qa) * t_val;\n                if (q > 1.0e-12f) {\n                    fu = (uqa + (uqb - uqa) * t_val) / q;\n                    fv = (vqa + (vqb - vqa) * t_val) / q;\n                }\n            }\n'

SW_GRID_NEW_9 = '            tf = ba; ba = bb; bb = tf;\n        }\n\n        int span = xb - xa;\n        if (span == 0) span = 1;\n\n        int sx = max_i(xa, t->cx1);\n        int ex = min_i(xb, t->cx2 + 1);\n\n        for (int x = sx; x < ex; x++) {\n            if (cov_viz_on() && t->s > 1) {\n                put_opaque(t, x, y, cov_colour(g_cov_prim));\n                continue;\n            }\n            float t_val = (float)(x - xa) / (float)span;\n            float fu = ua + (ub - ua) * t_val;\n            float fv = va + (vb - va) * t_val;\n            if (perspective) {\n                float q = qa + (qb - qa) * t_val;\n                if (q > 1.0e-12f) {\n                    fu = (uqa + (uqb - uqa) * t_val) / q;\n                    fv = (vqa + (vqb - vqa) * t_val) / q;\n                }\n            }\n'

SW_GRID_OLD_10 = '                                      uint16_t texpage, int raw_texture) {\n    int64_t area2 = (int64_t)(x1 - x0) * (int64_t)(y2 - y0)\n                  - (int64_t)(x2 - x0) * (int64_t)(y1 - y0);\n    if (area2 == 0) { precise_consumed(); return; }\n\n    if (g_texture_filter) {\n        int xs[3] = {x0,x1,x2}, ys[3] = {y0,y1,y2};\n        int us[3] = {u0,u1,u2}, vs[3] = {v0,v1,v2};\n'

SW_GRID_NEW_10 = '                                      uint16_t texpage, int raw_texture) {\n    int64_t area2 = (int64_t)(x1 - x0) * (int64_t)(y2 - y0)\n                  - (int64_t)(x2 - x0) * (int64_t)(y1 - y0);\n    if (area2 == 0) { precise_consumed(); return; }\n    g_cov_prim++;\n\n    if (g_texture_filter) {\n        int xs[3] = {x0,x1,x2}, ys[3] = {y0,y1,y2};\n        int us[3] = {u0,u1,u2}, vs[3] = {v0,v1,v2};\n'

SW_GRID_OLD_11 = '    if (g_hr) {\n        int s = g_scale;\n        RTarget hr = rt_hires();\n        raster_shaded_textured_triangle(&hr,\n            precise_scaled(0,0,x0,s), precise_scaled(1,0,y0,s), u0, v0, r0, g0, b0,\n            precise_scaled(0,1,x1,s), precise_scaled(1,1,y1,s), u1, v1, r1, g1, b1,\n            precise_scaled(0,2,x2,s), precise_scaled(1,2,y2,s), u2, v2, r2, g2, b2,\n            clut_x, clut_y, texpage, raw_texture, g_perspective_valid,\n            g_perspective_q[0], g_perspective_q[1], g_perspective_q[2]);\n    }\n    if (g_wide_cur) {\n        int s = g_scale, dx = wide_dx();\n        RTarget wt = rt_wide();\n        WideBd bd = wide_bd_get();\n        raster_shaded_textured_triangle(&wt,\n            precise_wide_x(0,x0,s,dx,&bd), precise_scaled(1,0,y0,s), u0, v0, r0, g0, b0,\n            precise_wide_x(1,x1,s,dx,&bd), precise_scaled(1,1,y1,s), u1, v1, r1, g1, b1,\n            precise_wide_x(2,x2,s,dx,&bd), precise_scaled(1,2,y2,s), u2, v2, r2, g2, b2,\n            clut_x, clut_y, texpage, raw_texture, g_perspective_valid,\n            g_perspective_q[0], g_perspective_q[1], g_perspective_q[2]);\n    }\n    precise_consumed();\n}\n'

SW_GRID_NEW_11 = '    if (g_hr) {\n        int s = g_scale;\n        RTarget hr = rt_hires();\n        if (grid_eligible(&hr)) {\n            const int rgb9[9] = { r0, g0, b0, r1, g1, b1, r2, g2, b2 };\n            GridTri gt;\n            grid_fill_textured(&gt, x0, y0, u0, v0, x1, y1, u1, v1,\n                               x2, y2, u2, v2, clut_x, clut_y, texpage,\n                               GG_SHADED_TEX, raw_texture, 0, rgb9);\n            grid_raster_triangle(&hr, &gt);\n        } else {\n            raster_shaded_textured_triangle(&hr,\n                precise_scaled(0,0,x0,s), precise_scaled(1,0,y0,s), u0, v0, r0, g0, b0,\n                precise_scaled(0,1,x1,s), precise_scaled(1,1,y1,s), u1, v1, r1, g1, b1,\n                precise_scaled(0,2,x2,s), precise_scaled(1,2,y2,s), u2, v2, r2, g2, b2,\n                clut_x, clut_y, texpage, raw_texture, g_perspective_valid,\n                g_perspective_q[0], g_perspective_q[1], g_perspective_q[2]);\n        }\n    }\n    if (g_wide_cur) {\n        int s = g_scale, dx = wide_dx();\n        RTarget wt = rt_wide();\n        WideBd bd = wide_bd_get();\n        if (!bd.on && grid_eligible(&wt)) {\n            const int rgb9[9] = { r0, g0, b0, r1, g1, b1, r2, g2, b2 };\n            GridTri gt;\n            grid_fill_textured(&gt, x0, y0, u0, v0, x1, y1, u1, v1,\n                               x2, y2, u2, v2, clut_x, clut_y, texpage,\n                               GG_SHADED_TEX, raw_texture, dx, rgb9);\n            grid_raster_triangle(&wt, &gt);\n        } else {\n            raster_shaded_textured_triangle(&wt,\n                precise_wide_x(0,x0,s,dx,&bd), precise_scaled(1,0,y0,s), u0, v0, r0, g0, b0,\n                precise_wide_x(1,x1,s,dx,&bd), precise_scaled(1,1,y1,s), u1, v1, r1, g1, b1,\n                precise_wide_x(2,x2,s,dx,&bd), precise_scaled(1,2,y2,s), u2, v2, r2, g2, b2,\n                clut_x, clut_y, texpage, raw_texture, g_perspective_valid,\n                g_perspective_q[0], g_perspective_q[1], g_perspective_q[2]);\n        }\n    }\n    precise_consumed();\n}\n'

D24HOLD_OLD_1 = "static uint32_t s_d24_upload_x1 = 0;\nstatic int      s_d24_present_hold = 0; /* vblanks to skip Swap after GP1(07h) */\n"

D24HOLD_NEW_1 = "static uint32_t s_d24_upload_x1 = 0;\n/* Set by a GP1(07h) band retarget while depth24: the span above still\n * describes the image in VRAM (a retarget moves no pixels), so it is\n * NOT discarded; the next depth24 upload restarts the span from its\n * own extent, because that commit is where a genuinely new image\n * begins. Consumed by depth24_note_upload. */\nstatic int      s_d24_span_restart_pending = 0;\nstatic int      s_d24_present_hold = 0; /* vblanks to skip Swap after GP1(07h) */\n"

D24HOLD_OLD_2 = "static void depth24_note_upload(uint32_t x, uint32_t w) {\n    if (!(display_depth & 1u)) return;\n    if (s_d24_present_hold > 0) return;\n    uint32_t x1 = x + w;\n    if (x1 > 1024u) x1 = 1024u;\n    if (x1 > s_d24_upload_x1) s_d24_upload_x1 = x1;\n}\n"

D24HOLD_NEW_2 = "static void depth24_note_upload(uint32_t x, uint32_t w) {\n    if (!(display_depth & 1u)) return;\n    /* The present-hold gates PRESENTING only (it drives the Swap skip\n     * via s_d24_present_skip); it must never gate accounting. Commits\n     * landing inside the 3-vblank hold after a GP1(07h) retarget are\n     * real coverage of the image being built: dropping them, together\n     * with the retarget reset this replaces, under-counted a static\n     * once-uploaded depth24 image whose upload straddled the retarget\n     * - Gran Turismo's 640x480 title stopped at 840/960 halfwords on\n     * hardware timing and the staging blanked its right 80 RGB px for\n     * the whole session (post-title screens, uploaded after the last\n     * retarget, were unaffected). */\n    uint32_t x1 = x + w;\n    if (x1 > 1024u) x1 = 1024u;\n    if (s_d24_span_restart_pending) {\n        /* First commit after a band retarget: this is where the new\n         * image's coverage starts; the previous image's extent no\n         * longer describes what scanout will show. */\n        s_d24_span_restart_pending = 0;\n        s_d24_upload_x1 = x1;\n    } else if (x1 > s_d24_upload_x1) {\n        s_d24_upload_x1 = x1;\n    }\n}\n"

D24HOLD_OLD_3 = "    if ((display_depth & 1u) && s_d24_prev_disp_h != 0u && h != s_d24_prev_disp_h) {\n        s_d24_upload_x1 = 0;\n        s_d24_present_hold = 3;\n    }\n"

D24HOLD_NEW_3 = "    if ((display_depth & 1u) && s_d24_prev_disp_h != 0u && h != s_d24_prev_disp_h) {\n        /* Do NOT zero the span here: the retarget itself makes nothing\n         * in VRAM stale - the image on screen is still the one the\n         * guest uploaded, and for a static image (GT's title) no\n         * further upload ever re-extends a zeroed span, so the\n         * staging blanked real columns permanently. The hold below\n         * covers the flash window the reset was added for (MotK\n         * intro->crawl): presents skip Swap for 3 vblanks, and the\n         * first commit of the next image restarts the span via\n         * s_d24_span_restart_pending (see depth24_note_upload). */\n        s_d24_span_restart_pending = 1;\n        s_d24_present_hold = 3;\n    }\n"

D24HOLD_OLD_4 = "void gpu_depth24_upload_span_reset(void) {\n    s_d24_upload_x1 = 0;\n}\n"

D24HOLD_NEW_4 = "void gpu_depth24_upload_span_reset(void) {\n    s_d24_upload_x1 = 0;\n    s_d24_span_restart_pending = 0;\n}\n"

D24HOLD_OLD_5 = "    s_d24_upload_x1 = 0;\n    s_d24_present_hold = 0;\n    s_d24_present_skip = 0;\n    s_d24_prev_disp_h = 0;\n"

D24HOLD_NEW_5 = "    s_d24_upload_x1 = 0;\n    s_d24_span_restart_pending = 0;\n    s_d24_present_hold = 0;\n    s_d24_present_skip = 0;\n    s_d24_prev_disp_h = 0;\n"

D24HOLD_OLD_6 = "    WU(s_d24_upload_x1); WI(s_d24_present_hold | 0x200 | (s_d24_present_skip ? 0x100 : 0));\n"

D24HOLD_NEW_6 = "    WU(s_d24_upload_x1); WI(s_d24_present_hold | 0x200 | (s_d24_present_skip ? 0x100 : 0) | (s_d24_span_restart_pending ? 0x400 : 0));\n"

D24HOLD_OLD_7 = "    s_d24_present_skip = (s_d24_present_hold & 0x200)\n        ? (s_d24_present_hold & 0x100) != 0\n        : (s_d24_present_hold & 0x100) || (s_d24_present_hold & 0xFF) > 0;\n    s_d24_present_hold &= 0xFF;\n"

D24HOLD_NEW_7 = "    s_d24_present_skip = (s_d24_present_hold & 0x200)\n        ? (s_d24_present_hold & 0x100) != 0\n        : (s_d24_present_hold & 0x100) || (s_d24_present_hold & 0xFF) > 0;\n    /* Bit 10 (0x400): span-restart-pending, only meaningful in the\n     * 0x200 format; snaps without the marker predate the flag. */\n    s_d24_span_restart_pending = (s_d24_present_hold & 0x400) != 0;\n    s_d24_present_hold &= 0xFF;\n"

def patch_gpu_depth24_hold(template, log=print, fail_loud=False):
    """Fix the depth24 upload-span accounting around GP1(07h) band
    retargets. Two accounting rules threw away real coverage: (1)
    depth24_note_upload discarded every CPU->VRAM commit that landed
    inside the 3-vblank present-hold after a retarget, although the
    hold exists to gate PRESENTING (Swap skip), not accounting; and
    (2) the retarget itself hard-reset the span to zero, discarding
    coverage the guest had already delivered for the SAME image - a
    streamed movie re-uploads and re-grows the span within a frame,
    but a static once-uploaded image never does. Gran Turismo's
    640x480 depth24 title screen was left with a counted span of
    840/960 halfwords on hardware timing, so the depth24 staging
    blanked its right 80 RGB px for the whole session (only the title
    cropped; later screens upload after the last retarget). The patch
    counts commits during the hold, and replaces the hard reset with
    a restart-pending flag the next depth24 commit consumes (the new
    image's coverage starts at its own first commit; the hold still
    covers the stale-flash window the reset protected MotK against).
    Depth toggles and depth24-exit resets stay hard resets. The flag
    rides the GPU snapshot in bit 0x400 of the packed hold word, next
    to the existing skip flag. Composes with patch_gpu_depth24_span
    (v1.0.17): anchors are byte-identical before and after that patch,
    and the span limit / staging logic are untouched.
    Idempotent text patch on the template / framework cache and on
    scaffolded projects (found 2026-11-07, GT title crop)."""
    try:
        src = Path(template) / "psxrecomp" / "runtime" / "src" / \
            "gpu.c"
        if not src.exists():
            if fail_loud:
                raise StageError(
                    f"gpu.c missing under {template} - cannot "
                    f"apply the depth24 hold patch")
            return
        text = src.read_text()
        if "s_d24_span_restart_pending" in text:
            return
        pairs = [(D24HOLD_OLD_1, D24HOLD_NEW_1),
                 (D24HOLD_OLD_2, D24HOLD_NEW_2),
                 (D24HOLD_OLD_3, D24HOLD_NEW_3),
                 (D24HOLD_OLD_4, D24HOLD_NEW_4),
                 (D24HOLD_OLD_5, D24HOLD_NEW_5),
                 (D24HOLD_OLD_6, D24HOLD_NEW_6),
                 (D24HOLD_OLD_7, D24HOLD_NEW_7)]
        missing = [i for i, (old, _n) in enumerate(pairs)
                   if old not in text]
        if missing:
            if fail_loud:
                raise StageError(
                    f"gpu.c in {template} does NOT take the "
                    f"depth24 hold patch: {len(missing)} of "
                    f"{len(pairs)} anchors missing {missing} - "
                    f"this runtime variant is not covered by "
                    f"this tool version, and static 24-bit "
                    f"screens shown right after a display-band "
                    f"change (Gran Turismo's title screen) would "
                    f"ship with the right side cropped. Failing "
                    f"the job instead of shipping it broken; "
                    f"update the tool or the template.")
            return
        for old, new in pairs:
            text = text.replace(old, new, 1)
        src.write_text(text)
        log("patched gpu.c: depth24 span counts commits during the "
            "present-hold; GP1(07h) retarget defers the span reset to "
            "the next upload (GT title-screen crop fix)")
    except StageError:
        raise
    except Exception as e:  # a patch failure must never break a run
        if fail_loud:
            raise StageError(
                f"gpu depth24 hold patch failed on {template}: "
                f"{e}") from e
        log(f"gpu depth24 hold patch skipped: {e}")


def patch_sw_guest_grid(template, log=print, fail_loud=False):
    """Fix hi-res coverage in the software rasterizer: decide coverage
    on the guest pixel grid instead of rasterizing scaled geometry.

    PS1 Doom-class engines draw their world from one-guest-pixel-thin
    textured triangles whose abutting edges coincide on the guest
    grid. The native pass tiles them exactly (biased edge DDA), but
    the hi-res pass re-rasterized the scaled vertices with exact
    geometric coverage, so at internal resolution > 1x sub-pixel
    slivers of each guest pixel were claimed by the wrong span or by
    none — a darker surface behind showed through in a phase-locked
    pattern: the dense vertical striping measured on Final Doom's
    walls/floors (period exactly 1 guest pixel, up to ~20% luminance
    modulation in the raw hi-res buffer, identical in both
    renderers). The patch adds a guest-grid rasterizer to
    gpu_sw_renderer.c: for non-PGXP triangles on hi-res/wide targets
    it takes ownership spans from the same guest-space edge DDA the
    native pass uses, so every guest pixel is covered by exactly one
    of the primitives that tile it, and fills the pixel's SxS block
    by sampling the primitive's barycentric attribute field at
    sub-pixel centres (hi-res sampling inside the guest pixel —
    texture detail preserved, no blur, no fattened geometry). The
    native VRAM pass is untouched; PGXP/precise and
    faithful-authority primitives keep the legacy scaled path.
    Verified on Final Doom (software renderer): coverage-viz split
    guest blocks 52-89% -> 0.0%; stripe-period modulation 19.8% ->
    0.67% (wall centre) at 4x internal. The OpenGL backend is
    covered by the companion patch_gl_guest_grid (same release);
    Vulkan has its own pipeline and is not covered by either.
    Idempotent text patch on the template /
    framework cache and on scaffolded projects (found 2026-11-06).
    """
    try:
        src = Path(template) / "psxrecomp" / "runtime" / "src" / \
            "gpu_sw_renderer.c"
        if not src.exists():
            if fail_loud:
                raise StageError(
                    f"gpu_sw_renderer.c missing under {template} - "
                    f"cannot apply the software guest-grid patch")
            return
        text = src.read_text()
        if SW_GRID_MARKER in text:
            return
        pairs = [(SW_GRID_OLD_0, SW_GRID_NEW_0), (SW_GRID_OLD_1, SW_GRID_NEW_1), (SW_GRID_OLD_2, SW_GRID_NEW_2), (SW_GRID_OLD_3, SW_GRID_NEW_3), (SW_GRID_OLD_4, SW_GRID_NEW_4), (SW_GRID_OLD_5, SW_GRID_NEW_5), (SW_GRID_OLD_6, SW_GRID_NEW_6), (SW_GRID_OLD_7, SW_GRID_NEW_7), (SW_GRID_OLD_8, SW_GRID_NEW_8), (SW_GRID_OLD_9, SW_GRID_NEW_9), (SW_GRID_OLD_10, SW_GRID_NEW_10), (SW_GRID_OLD_11, SW_GRID_NEW_11)]
        missing = [i for i, (old, _n) in enumerate(pairs)
                   if old not in text]
        if missing:
            if fail_loud:
                raise StageError(
                    f"gpu_sw_renderer.c in {template} does NOT take "
                    f"the software guest-grid patch: {len(missing)} "
                    f"of {len(pairs)} anchors missing {missing} - "
                    f"this renderer variant is not covered by this "
                    f"tool version, and the game would ship WITHOUT "
                    f"the Doom / Final Doom striping fix (software "
                    f"renderer). Failing the job instead of shipping "
                    f"it broken; update the tool or the template.")
            return
        for old, new in pairs:
            text = text.replace(old, new, 1)
        src.write_text(text)
        log("patched gpu_sw_renderer.c: guest-grid coverage for "
            "hi-res software rendering (Doom / Final Doom striping "
            "fix; software renderer)")
    except StageError:
        raise
    except Exception as e:  # a patch failure must never break a run
        if fail_loud:
            raise StageError(
                f"software guest-grid patch failed on {template}: "
                f"{e}") from e
        log(f"sw guest-grid patch skipped: {e}")


GL_GRID_MARKER = "guest-grid coverage (Doom-engine striping fix, GL backend)"

GL_GRID_OLD_0 = '#include "gpu_render.h"\n#include "gl_scale_limits.h"\n#include "gpu_sw_renderer.h"\n#include "gpu_fmv_hd.h"\n#include "gpu_gl_renderer.h"\n#include "video_telemetry.h"\n'

GL_GRID_NEW_0 = '#include "gpu_render.h"\n#include "gl_scale_limits.h"\n#include "gpu_sw_renderer.h"\n#include "gpu_sw_edges.h"   /* guest-grid coverage: the native DDA rule (GL fix) */\n#include "gpu_fmv_hd.h"\n#include "gpu_gl_renderer.h"\n#include "video_telemetry.h"\n'

GL_GRID_OLD_1 = "static int s_ws_ablate = 0;\nstatic void flush_tex_batch_impl(void); /* textured-prim batch — defined below, flushed from coherency points */\nstatic void flush_flat_batch_impl(void); /* flat/gouraud GEO batch (MotK starfield 0x68 dots) */\n/* Every flush call site is attributed (gl_renderer_batch_sites): a flush that\n * draws a non-empty batch counts against its caller's source line. */\nstatic void batch_site_note(int kind, int line);\n"

GL_GRID_NEW_1 = "static int s_ws_ablate = 0;\nstatic void flush_tex_batch_impl(void); /* textured-prim batch — defined below, flushed from coherency points */\nstatic void flush_flat_batch_impl(void); /* flat/gouraud GEO batch (MotK starfield 0x68 dots) */\n/* Guest-grid GEO draw — defined with flush_flat_batch, used by the hiw\n * queue replays above it. */\nstatic void geo_grid_draw(int nverts, int semi, int dmode, int mask, int check,\n                          int b3d, float shift, float xoff, float xhalf,\n                          float xscale, float xcenter,\n                          const float *fb, const float *fbg);\n/* Every flush call site is attributed (gl_renderer_batch_sites): a flush that\n * draws a non-empty batch counts against its caller's source line. */\nstatic void batch_site_note(int kind, int line);\n"

GL_GRID_OLD_2 = "/* Programs. */\nstatic GLuint s_geo_prog = 0, s_geo_vao = 0, s_geo_vbo = 0;\nstatic GLuint s_tex_prog = 0, s_tex_vao = 0, s_tex_vbo = 0;\n/* Textured vertex: pos(2) uv(2) col(4) tpage(2) clut(2) depth(1) raw(1) limits(4)\n * semi(1) q(1) twin(1)\n * — per-prim texture state in flat attributes so prims batch (see flush_tex_batch).\n * q is the perspective weight ([video] perspective_texturing): 0 = affine, the\n * PS1-faithful default, which makes the vertex shader's w exactly 1.0 and the\n * fragment shader read the noperspective varying — i.e. bit-identical to the\n * pre-feature pipeline. twin is the prim's GP0(E2h) texture window, its low 20\n * bits as a whole float (mask x, mask y, offset x, offset y; 5 bits each). */\n#define TEXV 27\nstatic GLuint s_blit_prog = 0, s_blit_vao = 0, s_blit_vbo = 0;\nstatic GLuint s_blit_hi_prog = 0;            /* windowed hi surface blit */\nstatic GLint  s_uBhSrc = -1, s_uBhPass = -1, s_uBhMaskset = -1, s_uBhSrcDiv = -1;\n"

GL_GRID_NEW_2 = "/* Programs. */\nstatic GLuint s_geo_prog = 0, s_geo_vao = 0, s_geo_vbo = 0;\nstatic GLuint s_tex_prog = 0, s_tex_vao = 0, s_tex_vbo = 0;\n/* Guest-grid GEO variant (GEO_GRID_VS/FS): own program + VAO; the VAO's\n * attribs 0/1 read s_geo_vbo like s_geo_vao, attribs 2..7 read the side\n * buffer s_geo_grid_vbo. 0 when its (non-fatal) compile failed — GEO grid\n * is then disabled and flat/gouraud prims keep the legacy path. */\nstatic GLuint s_geo_grid_prog = 0, s_geo_grid_vao = 0, s_geo_grid_vbo = 0;\nstatic GLint s_geog_uShift = -1, s_geog_uXoff = -1, s_geog_uXhalf = -1,\n             s_geog_uXscale = -1, s_geog_uXcenter = -1, s_geog_uZbias = -1,\n             s_geog_uBcls = -1, s_geog_uBadd = -1,\n             s_geog_uDither = -1, s_geog_uAccurate = -1;\nstatic GLint s_tex_uCovviz = -1;\n/* Textured vertex: pos(2) uv(2) col(4) tpage(2) clut(2) depth(1) raw(1) limits(4)\n * semi(1) q(1) twin(1) hd_source(4) hd_mode(1) filter(1) gg(4)\n * — per-prim texture state in flat attributes so prims batch (see flush_tex_batch).\n * q is the perspective weight ([video] perspective_texturing): 0 = affine, the\n * PS1-faithful default, which makes the vertex shader's w exactly 1.0 and the\n * fragment shader read the noperspective varying — i.e. bit-identical to the\n * pre-feature pipeline. twin is the prim's GP0(E2h) texture window, its low 20\n * bits as a whole float (mask x, mask y, offset x, offset y; 5 bits each).\n * gg is the guest-grid coverage record (slots 27..30, GG_STRIDE floats,\n * identical on a prim's 3 verts): the prim's Y-sorted guest verts packed\n * two 12-bit-biased coords per exact-integer float (3) plus the\n * ownership-test flag (1). The vertex shader rebuilds the native-DDA\n * edge steps from the verts. Zero for non-eligible prims. GL-only: the\n * Vulkan backend has its own vertex format (gpu_vk_renderer.c) and never\n * reads this array. */\n#define TEXV 31\n#define GG_STRIDE 4   /* guest-grid record floats per vertex (TEX + GEO side) */\nstatic GLuint s_blit_prog = 0, s_blit_vao = 0, s_blit_vbo = 0;\nstatic GLuint s_blit_hi_prog = 0;            /* windowed hi surface blit */\nstatic GLint  s_uBhSrc = -1, s_uBhPass = -1, s_uBhMaskset = -1, s_uBhSrcDiv = -1;\n"

GL_GRID_OLD_3 = '    "  vec3 k = clamp(floor((c * 255.0 + m[p.y * 4 + p.x]) / 8.0), 0.0, 31.0);\\n" \\\n    "  return u_accurate != 0 ? k * (8.0 / 255.0) : k / 31.0;   /* the surface\'s basis */\\n" \\\n    "}\\n"\nstatic const char *GEO_VS =\n    "#version 330\\n"\n    "layout(location=0) in vec2 a_pos;\\n"\n'

GL_GRID_NEW_3 = '    "  vec3 k = clamp(floor((c * 255.0 + m[p.y * 4 + p.x]) / 8.0), 0.0, 31.0);\\n" \\\n    "  return u_accurate != 0 ? k * (8.0 / 255.0) : k / 31.0;   /* the surface\'s basis */\\n" \\\n    "}\\n"\n/* ---- guest-grid coverage (Doom-engine striping fix, GL backend) ---------\n * The software rasterizer\'s fix (gpu_sw_renderer.c grid_raster_triangle)\n * decides coverage on the guest pixel grid: per guest row, the span from\n * psx_triangle_edges_at_y (biased 64-bit fixed-point DDA, gpu_sw_edges.h)\n * is owned by exactly one primitive. This is the same rule for the GL\n * backend, evaluated per fragment: the batch writer expands an eligible\n * prim (seam_expand) so GL generates fragments across every owned guest\n * pixel, and the fragment shader discards the fragments whose guest\n * pixel the prim does not own. The edge evaluation below is bit-exact\n * against the C DDA by construction: the only division (psx_edge_step)\n * is computed on the CPU in C; the shader only adds/multiplies mod 2^64\n * in uint arithmetic and takes the high word (== psx_edge_unfp). The\n * The prim\'s DDA steps are a pure function of its sorted guest verts\n * (psx_edge_step of the three edges), so the vertex carries only the\n * six coords — two 12-bit-biased coords per exact-integer float, three\n * floats plus the on flag in ONE vec4 attribute (attribute locations\n * are scarce: this driver caps them at 16) — and the vertex shader\n * recomputes the steps with an emulated int64 division (schoolbook\n * base-2^16, exact in uint arithmetic; dy > 0 there, matching the\n * branches the C code evaluates).\n * Guest position comes from v_npos: with u_shift = 0.5/S - 1/64 the\n * interpolated value at a fragment centre is FBO_px/S + 1/64 on every\n * surface (hr, wide, window tiles) at every scale, so floor(v_npos)\n * is the fragment\'s guest pixel, scale- and surface-independent. */\n#define GG_GLSL \\\n    "int gg_edge_x(int fx, uvec2 step, int k){\\n" \\\n    "  uint slo = step.x, shi = step.y;\\n" \\\n    "  uint a = slo & 65535u, b = slo >> 16;\\n" \\\n    "  uint t0 = a * uint(k), t1 = b * uint(k);\\n" \\\n    "  uint plo = t0 + (t1 << 16);\\n" \\\n    "  uint phi = (t1 >> 16) + (plo < t0 ? 1u : 0u);\\n" \\\n    "  int pshi = int(shi) * k + int(phi);\\n" \\\n    "  uint lo = 0xFFFFF800u + plo;\\n" \\\n    "  uint hi = uint(pshi) + uint(fx) + (lo < 0xFFFFF800u ? 1u : 0u);\\n" \\\n    "  return int(hi);\\n" \\\n    "}\\n" \\\n    "bool gg_owned(ivec2 g, ivec3 p0, ivec2 p1, uvec2 sl, uvec2 s1, uvec2 s2){\\n" \\\n    "  if (g.y < p0.y || g.y >= p1.y) return false;\\n" \\\n    "  int xa = gg_edge_x(p0.x, sl, g.y - p0.y);\\n" \\\n    "  int xb = g.y < p0.z ? gg_edge_x(p0.x, s1, g.y - p0.y)\\n" \\\n    "                      : gg_edge_x(p1.x, s2, g.y - p0.z);\\n" \\\n    "  int lo = min(xa, xb), hi = max(xa, xb);\\n" \\\n    "  return g.x >= lo && g.x < hi;\\n" \\\n    "}\\n" \\\n    "vec3 gg_hash_colour(ivec3 p0, ivec2 p1, uvec2 sl){\\n" \\\n    "  uint h = uint(p0.x * 73856093) ^ uint(p0.y * 19349663) ^ uint(p1.x * 83492791) ^ sl.x ^ (sl.y << 7);\\n" \\\n    "  h ^= h >> 13; h *= 0x5bd1e995u; h ^= h >> 15;\\n" \\\n    "  return vec3(float((h >> 16) & 255u), float((h >> 8) & 255u), float(h & 255u)) / 255.0;\\n" \\\n    "}\\n"\n#define GG_GLSL_DECODE \\\n    "uvec2 gg_step(int dx, int dy){\\n" \\\n    "  if (dy <= 0) return uvec2(0u);\\n" \\\n    "  int r = dx < 0 ? -(dy - 1) : (dx > 0 ? (dy - 1) : 0);\\n" \\\n    "  uint nlo = uint(r);\\n" \\\n    "  int nhi = dx + (r < 0 ? -1 : 0);\\n" \\\n    "  bool neg = nhi < 0;\\n" \\\n    "  uint alo = neg ? (0u - nlo) : nlo;\\n" \\\n    "  int ahi = neg ? (-nhi - (nlo != 0u ? 1 : 0)) : nhi;\\n" \\\n    "  uint dyu = uint(dy), rem = 0u, cur;\\n" \\\n    "  cur = uint(ahi);              uint q2 = cur / dyu; rem = cur - q2 * dyu;\\n" \\\n    "  cur = rem * 65536u + (alo >> 16);   uint q1 = cur / dyu; rem = cur - q1 * dyu;\\n" \\\n    "  cur = rem * 65536u + (alo & 65535u); uint q0 = cur / dyu;\\n" \\\n    "  uint qlo = (q1 << 16) | q0, qhi = q2;\\n" \\\n    "  if (neg) { uint olo = qlo; qlo = 0u - qlo; qhi = 0u - qhi - (olo != 0u ? 1u : 0u); }\\n" \\\n    "  return uvec2(qlo, qhi);\\n" \\\n    "}\\n" \\\n    "void gg_decode(vec4 g, out ivec3 p0, out ivec2 p1, out uvec2 sl, out uvec2 s1, out uvec2 s2){\\n" \\\n    "  uint c0 = uint(g.x), c1 = uint(g.y), c2 = uint(g.z);\\n" \\\n    "  int x0 = int(c0 & 4095u) - 2048, y0 = int(c0 >> 12) - 2048;\\n" \\\n    "  int x1 = int(c1 & 4095u) - 2048, y1 = int(c1 >> 12) - 2048;\\n" \\\n    "  int x2 = int(c2 & 4095u) - 2048, y2 = int(c2 >> 12) - 2048;\\n" \\\n    "  p0 = ivec3(x0, y0, y1); p1 = ivec2(x1, y2);\\n" \\\n    "  sl = gg_step(x2 - x0, y2 - y0);\\n" \\\n    "  s1 = gg_step(x1 - x0, y1 - y0);\\n" \\\n    "  s2 = gg_step(x2 - x1, y2 - y1);\\n" \\\n    "}\\n"\nstatic const char *GEO_VS =\n    "#version 330\\n"\n    "layout(location=0) in vec2 a_pos;\\n"\n'

GL_GRID_OLD_4 = '    "  frag = v_cp != 0 ? vec4(v_col_p, v_col.a) : v_col;\\n"\n    "  frag.rgb = psx_dither(frag.rgb, v_npos, v_dith); }\\n";\n\n/* Textured prims: sample raw 1555 VRAM (integer), CLUT decode per depth,\n * texture window, optional bilinear, texel-0 discard, STP-split discard,\n * PS1 *2-around-0x80 modulation. Output alpha = bit15 of the written pixel.\n'

GL_GRID_NEW_4 = '    "  frag = v_cp != 0 ? vec4(v_col_p, v_col.a) : v_col;\\n"\n    "  frag.rgb = psx_dither(frag.rgb, v_npos, v_dith); }\\n";\n\n/* Guest-grid variant of the GEO program: identical shading to GEO_VS/FS,\n * plus the per-prim guest-grid data (attributes 2..7, from the side VBO\n * s_geo_grid_vbo, GG_STRIDE floats per vertex — same packing as the TEX\n * slots 27..38) and the ownership discard. Used ONLY for grid batch\n * flushes and their mirrors/replays, so no other GEO draw can observe\n * the extra attributes. Keep in step with GEO_VS/GEO_FS above. */\nstatic const char *GEO_GRID_VS =\n    "#version 330\\n"\n    "layout(location=0) in vec2 a_pos;\\n"\n    "layout(location=1) in vec4 a_col;\\n"\n    "layout(location=2) in vec4 a_gg;   /* guest-grid: packed sorted guest verts + on */\\n"\n\n    "uniform float u_shift;\\n"\n    "uniform float u_xoff;\\n"\n    "uniform float u_xhalf;\\n"\n    "uniform float u_xscale;\\n"\n    "uniform float u_xcenter;\\n"\n    "uniform float u_zbias;\\n"\n    "noperspective out vec4 v_col;\\n"\n    "smooth out vec3 v_col_p;\\n"\n    "flat out int v_cp;\\n"\n    "noperspective out float v_b3d;\\n"\n    "noperspective out vec2 v_npos;\\n"\n    "flat out int v_dith;\\n"\n    "flat out ivec3 v_gg_p0; flat out ivec2 v_gg_p1;\\n"\n    "flat out uvec2 v_gg_sl; flat out uvec2 v_gg_s1; flat out uvec2 v_gg_s2;\\n"\n    GG_GLSL_DECODE\n    "void main(){\\n"\n    "  v_npos = a_pos;\\n"\n    "  gg_decode(a_gg, v_gg_p0, v_gg_p1, v_gg_sl, v_gg_s1, v_gg_s2);\\n"\n    "  float a = a_col.a, code = floor(a * 0.5), zn = 0.0, w = 1.0;\\n"\n    "  float m = a - 2.0 * code;\\n"\n    "  float dth = (a >= 0.0 && code >= 131072.0) ? 1.0 : 0.0; code -= 131072.0 * dth;\\n"\n    "  float cp = code >= 65536.0 ? 1.0 : 0.0, sz = code - 65536.0 * cp;\\n"\n    "  bool far_code = a < -1.5;\\n"\n    "  if (far_code) { uint bits = floatBitsToUint(-a); m = float(bits & 1u); cp = float((bits >> 1u) & 1u); dth = float((bits >> 2u) & 1u); sz = uintBitsToFloat(bits & 0xfffffff8u) - 2.0; }\\n"\n    "  v_dith = int(dth);\\n"\n    "  if (a < 0.0 && !far_code) { m = 0.0; zn = 1.0; sz = 0.0; cp = 0.0; }\\n"\n    "  else if (sz > 0.5) { zn = 1.0 - 512.0 / (max(sz * (1.0 - u_zbias) - (u_zbias > 0.0 ? 48.0 : 0.0), 1.0) + 256.0); if (cp > 0.5) w = sz / 1024.0; }\\n"\n    "  v_col = vec4(a_col.rgb, m); v_col_p = a_col.rgb; v_cp = int(cp);\\n"\n    "  v_b3d = ((a >= 0.0 || far_code) && sz > 0.5) ? 1.0 : 0.0;\\n"\n    "  float xb = a_pos.x;\\n"\n    "  if (u_xscale < 0.0) {\\n"\n    "    float s = -u_xscale; float h = u_xhalf / s;\\n"\n    "    float l = u_xcenter - h, r = u_xcenter + h;\\n"\n    "    if (xb < l) xb = l + (xb-l)*s; else if (xb > r) xb = r + (xb-r)*s;\\n"\n    "  } else xb = (xb - u_xcenter)*u_xscale + u_xcenter;\\n"\n    "  gl_Position = vec4(((xb+u_shift+u_xoff)/u_xhalf - 1.0) * w, ((a_pos.y+u_shift)/256.0 - 1.0) * w, zn * w, w); }\\n";\nstatic const char *GEO_GRID_FS =\n    "#version 330\\n"\n    "noperspective in vec4 v_col; smooth in vec3 v_col_p; flat in int v_cp; out vec4 frag;\\n"\n    "noperspective in float v_b3d;\\n"\n    "uniform int u_bcls;\\n"\n    "uniform int u_badd;\\n"\n    "noperspective in vec2 v_npos; flat in int v_dith;\\n"\n    "flat in ivec3 v_gg_p0; flat in ivec2 v_gg_p1;\\n"\n    "flat in uvec2 v_gg_sl; flat in uvec2 v_gg_s1; flat in uvec2 v_gg_s2;\\n"\n    PSX_DITHER_GLSL\n    GG_GLSL\n    "void main(){\\n"\n    "  if (!gg_owned(ivec2(floor(v_npos)), v_gg_p0, v_gg_p1, v_gg_sl, v_gg_s1, v_gg_s2)) discard;\\n"\n    "  if (u_bcls != 0) { if (u_bcls == 1 && !(v_b3d > 0.999 || u_badd != 0)) discard; frag = vec4(0.0); return; }\\n"\n    "  frag = v_cp != 0 ? vec4(v_col_p, v_col.a) : v_col;\\n"\n    "  frag.rgb = psx_dither(frag.rgb, v_npos, v_dith); }\\n";\n\n/* Textured prims: sample raw 1555 VRAM (integer), CLUT decode per depth,\n * texture window, optional bilinear, texel-0 discard, STP-split discard,\n * PS1 *2-around-0x80 modulation. Output alpha = bit15 of the written pixel.\n'

GL_GRID_OLD_5 = '    "layout(location=11) in vec4 a_hd_source; /* page origin + native extent */\\n"\n    "layout(location=12) in float a_hd_mode;\\n"\n    "layout(location=13) in float a_filter; /* 0 nearest, 1 bilinear, 2 stable */\\n"\n    "uniform float u_shift;\\n"\n    "uniform float u_xoff;   /* native-wide x translation (px); 0 canonical */\\n"\n    "uniform float u_xhalf;  /* x clip half-extent (px); 512 canonical */\\n"\n'

GL_GRID_NEW_5 = '    "layout(location=11) in vec4 a_hd_source; /* page origin + native extent */\\n"\n    "layout(location=12) in float a_hd_mode;\\n"\n    "layout(location=13) in float a_filter; /* 0 nearest, 1 bilinear, 2 stable */\\n"\n    "layout(location=14) in vec4 a_gg;   /* guest-grid: packed sorted guest verts + on */\\n"\n    "uniform float u_shift;\\n"\n    "uniform float u_xoff;   /* native-wide x translation (px); 0 canonical */\\n"\n    "uniform float u_xhalf;  /* x clip half-extent (px); 512 canonical */\\n"\n'

GL_GRID_OLD_6 = '    "smooth out vec3 v_col_p; flat out int v_cp;\\n"\n    "noperspective out float v_b3d;   /* bloom source: 1 on a PGXP 3D vertex */\\n"\n    "noperspective out vec2 v_npos;  /* native VRAM position (dithering) */\\n"\n    "void main(){ v_uv = a_uv; v_uv_p = a_uv; v_col = a_col; v_col_p = a_col.rgb;\\n"\n    "  v_npos = a_pos;\\n"\n    "  v_persp = (a_q > 0.0) ? 1 : 0;\\n"\n    "  v_tpage = ivec2(a_tpage + 0.5); v_clut = ivec2(a_clut + 0.5);\\n"\n    "  v_depth = int(a_depth + 0.5); v_raw = int(a_raw + 0.5);\\n"\n'

GL_GRID_NEW_6 = '    "smooth out vec3 v_col_p; flat out int v_cp;\\n"\n    "noperspective out float v_b3d;   /* bloom source: 1 on a PGXP 3D vertex */\\n"\n    "noperspective out vec2 v_npos;  /* native VRAM position (dithering) */\\n"\n    "flat out int v_gg_on; flat out ivec3 v_gg_p0; flat out ivec2 v_gg_p1;\\n"\n    "flat out uvec2 v_gg_sl; flat out uvec2 v_gg_s1; flat out uvec2 v_gg_s2;\\n"\n    GG_GLSL_DECODE\n    "void main(){ v_uv = a_uv; v_uv_p = a_uv; v_col = a_col; v_col_p = a_col.rgb;\\n"\n    "  v_npos = a_pos;\\n"\n    "  v_gg_on = int(a_gg.w + 0.5);\\n"\n    "  gg_decode(a_gg, v_gg_p0, v_gg_p1, v_gg_sl, v_gg_s1, v_gg_s2);\\n"\n    "  v_persp = (a_q > 0.0) ? 1 : 0;\\n"\n    "  v_tpage = ivec2(a_tpage + 0.5); v_clut = ivec2(a_clut + 0.5);\\n"\n    "  v_depth = int(a_depth + 0.5); v_raw = int(a_raw + 0.5);\\n"\n'

GL_GRID_OLD_7 = '    "smooth in vec3 v_col_p; flat in int v_cp;\\n"\n    "noperspective in vec2 v_npos;\\n"\n    PSX_DITHER_GLSL\n    "out vec4 frag; out vec4 blend_factor;\\n"\n    "noperspective in float v_b3d;\\n"\n    "uniform int u_bcls;      /* bloom source pass (gl_postfx.inc): 0 off, 2 clear, 1 mark */\\n"\n'

GL_GRID_NEW_7 = '    "smooth in vec3 v_col_p; flat in int v_cp;\\n"\n    "noperspective in vec2 v_npos;\\n"\n    PSX_DITHER_GLSL\n    GG_GLSL\n    "out vec4 frag; out vec4 blend_factor;\\n"\n    "noperspective in float v_b3d;\\n"\n    "uniform int u_bcls;      /* bloom source pass (gl_postfx.inc): 0 off, 2 clear, 1 mark */\\n"\n'

GL_GRID_OLD_8 = '    "flat in int v_twin;      /* texture window, GP0(E2h) bits 0..19 */\\n"\n    "flat in int v_filter;    /* sampling mode belongs to this primitive */\\n"\n    "flat in vec4 v_hd_source; flat in int v_hd_mode;\\n"\n    "uniform sampler2D u_hd_texture;\\n"\n    "uniform usampler2D u_vram;\\n"\n    "uniform usampler2D u_palette;\\n"\n    "uniform int u_semipass;  /* 0=all texels, 1=STP=0 only, 2=STP=1 only */\\n"\n'

GL_GRID_NEW_8 = '    "flat in int v_twin;      /* texture window, GP0(E2h) bits 0..19 */\\n"\n    "flat in int v_filter;    /* sampling mode belongs to this primitive */\\n"\n    "flat in vec4 v_hd_source; flat in int v_hd_mode;\\n"\n    "flat in int v_gg_on; flat in ivec3 v_gg_p0; flat in ivec2 v_gg_p1;\\n"\n    "flat in uvec2 v_gg_sl; flat in uvec2 v_gg_s1; flat in uvec2 v_gg_s2;\\n"\n    "uniform sampler2D u_hd_texture;\\n"\n    "uniform int u_covviz;    /* PSX_GL_COV_VIZ: prim-id colouring (diagnostic) */\\n"\n    "uniform usampler2D u_vram;\\n"\n    "uniform usampler2D u_palette;\\n"\n    "uniform int u_semipass;  /* 0=all texels, 1=STP=0 only, 2=STP=1 only */\\n"\n'

GL_GRID_OLD_9 = '    "  return mix(xbr_c(E), xbr_c(px), a);\\n"\n    "}\\n"\n    "void main(){\\n"\n    "  int stp; vec3 rgb;\\n"\n    "  int fmode = v_filter & 15;\\n"\n    "  /* v_persp is 0 for every prim unless [video] perspective_texturing is on\\n"\n'

GL_GRID_NEW_9 = '    "  return mix(xbr_c(E), xbr_c(px), a);\\n"\n    "}\\n"\n    "void main(){\\n"\n    "  /* Guest-grid coverage: a fragment whose guest pixel this prim does\\n"\n    "   * not own (per the native DDA spans) does not exist. Runs before\\n"\n    "   * every sampling/blend decision so all passes (colour, STP split,\\n"\n    "   * stencil fixup, bloom marking) agree on coverage exactly. */\\n"\n    "  if (v_gg_on != 0 && !gg_owned(ivec2(floor(v_npos)), v_gg_p0, v_gg_p1, v_gg_sl, v_gg_s1, v_gg_s2)) discard;\\n"\n    "  if (u_covviz != 0) { frag = vec4(gg_hash_colour(v_gg_p0, v_gg_p1, v_gg_sl), 1.0); blend_factor = vec4(0.0); return; }\\n"\n    "  int stp; vec3 rgb;\\n"\n    "  int fmode = v_filter & 15;\\n"\n    "  /* v_persp is 0 for every prim unless [video] perspective_texturing is on\\n"\n'

GL_GRID_OLD_10 = ' * frames/s; queued, the mirrors land in one pass per wide surface at the same\n * sync points. Lines drawn as GL_LINES (1x) are not queued; they flush the\n * queue and mirror at once. PSX_GL_WIDE_QUEUE=0 turns the queue off. */\nenum { HQ_TEX = 1, HQ_GEO = 2 };\ntypedef struct {\n    uint8_t kind;\n    int8_t  semi;            /* tex: batch semi key; geo: blend mode, -1 opaque */\n'

GL_GRID_NEW_10 = ' * frames/s; queued, the mirrors land in one pass per wide surface at the same\n * sync points. Lines drawn as GL_LINES (1x) are not queued; they flush the\n * queue and mirror at once. PSX_GL_WIDE_QUEUE=0 turns the queue off. */\nenum { HQ_TEX = 1, HQ_GEO = 2, HQ_GEO_GRID = 3 };\ntypedef struct {\n    uint8_t kind;\n    int8_t  semi;            /* tex: batch semi key; geo: blend mode, -1 opaque */\n'

GL_GRID_OLD_11 = '    GLuint  palette, hd_tex;\n    int     ax0, ay0, ax1, ay1;  /* draw area, inclusive */\n    size_t  vfirst;          /* float offset into s_hq_v */\n    int     vcount;          /* vertices */\n    GLuint  wfbo;            /* native-wide mirror target, 0: none */\n    int     wdx;             /* its x shift (wide_dx()) */\n'

GL_GRID_NEW_11 = '    GLuint  palette, hd_tex;\n    int     ax0, ay0, ax1, ay1;  /* draw area, inclusive */\n    size_t  vfirst;          /* float offset into s_hq_v */\n    size_t  gfirst;          /* HQ_GEO_GRID: float offset into s_hq_g */\n    int     vcount;          /* vertices */\n    GLuint  wfbo;            /* native-wide mirror target, 0: none */\n    int     wdx;             /* its x shift (wide_dx()) */\n'

GL_GRID_OLD_12 = 'static int     s_hq_n = 0, s_hq_cap = 0;\nstatic float  *s_hq_v = NULL;\nstatic size_t  s_hq_vn = 0, s_hq_vcap = 0;\nstatic uint64_t s_hq_flushes = 0, s_hq_cmds = 0;\n\nstatic HiCmd *hiw_enqueue(int kind, const float *verts, int nverts, int stride) {\n'

GL_GRID_NEW_12 = 'static int     s_hq_n = 0, s_hq_cap = 0;\nstatic float  *s_hq_v = NULL;\nstatic size_t  s_hq_vn = 0, s_hq_vcap = 0;\nstatic float  *s_hq_g = NULL;   /* HQ_GEO_GRID side data (GG_STRIDE/vertex) */\nstatic size_t  s_hq_gn = 0, s_hq_gcap = 0;\nstatic uint64_t s_hq_flushes = 0, s_hq_cmds = 0;\n\nstatic HiCmd *hiw_enqueue(int kind, const float *verts, int nverts, int stride) {\n'

GL_GRID_OLD_13 = "    return wq;\n}\n\n/* Replay the queued native-wide mirrors, in order, into their wide surfaces:\n * the state wide_target_begin sets (full-width scissor, the draw area's rows,\n * the S sample-grid shift) plus each entry's shift and backdrop stretch. */\n"

GL_GRID_NEW_13 = "    return wq;\n}\n\n/* Queue a guest-grid GEO batch: the stride-6 verts ride s_hq_v as usual,\n * the GG_STRIDE side data rides the parallel s_hq_g array. */\nstatic int hiw_enqueue_geo_grid(const float *verts, const float *gdata, int nverts,\n                                int semi, int mask, int mirror, int gate) {\n    int wq = hiw_wide_queue_ok(mirror);\n    if (!wq && !hiw_area_touches()) return 0;\n    size_t nf = (size_t)nverts * GG_STRIDE;\n    if (s_hq_gn + nf > s_hq_gcap) {\n        size_t cap = s_hq_gcap ? s_hq_gcap : 65536;\n        while (cap < s_hq_gn + nf) cap *= 2;\n        float *v = (float *)realloc(s_hq_g, cap * sizeof(float));\n        if (!v) return 0;\n        s_hq_g = v; s_hq_gcap = cap;\n    }\n    HiCmd *c = hiw_enqueue(HQ_GEO_GRID, verts, nverts, 6);\n    if (!c) { hiw_flush_queue(); return 0; }\n    c->gfirst = s_hq_gn;\n    memcpy(s_hq_g + s_hq_gn, gdata, nf * sizeof(float));\n    s_hq_gn += nf;\n    c->semi = (int8_t)semi;\n    c->mask = (uint8_t)(mask ? 1 : 0);\n    if (wq) hiw_wide_set(c, gate);\n    return wq;\n}\n\n/* Replay the queued native-wide mirrors, in order, into their wide surfaces:\n * the state wide_target_begin sets (full-width scissor, the draw area's rows,\n * the S sample-grid shift) plus each entry's shift and backdrop stretch. */\n"

GL_GRID_OLD_14 = '            depth_apply_ex(c->depth, 1);\n            tex_draw_passes_ex(c->vcount, c->semi, c->mask, c->check, 0, -1);\n            depth_restore_ex(c->depth, 1, c->vcount);\n        } else {\n            if (cur != HQ_GEO) {\n                p_glUseProgram(s_geo_prog);\n'

GL_GRID_NEW_14 = '            depth_apply_ex(c->depth, 1);\n            tex_draw_passes_ex(c->vcount, c->semi, c->mask, c->check, 0, -1);\n            depth_restore_ex(c->depth, 1, c->vcount);\n        } else if (c->kind == HQ_GEO_GRID) {\n            if (c->semi >= 0) apply_psx_blend(c->semi); else glDisable(GL_BLEND);\n            mask_stencil_ex(c->mask, c->check);\n            geo_grid_draw(c->vcount, c->semi, 0, c->mask, c->check, -1,\n                          s_shift_hi, (float)c->wdx, (float)g_wide_w / 2.0f,\n                          c->wscale, c->wcenter,\n                          s_hq_v + c->vfirst, s_hq_g + c->gfirst);\n            cur = 0;   /* the grid draw bound its own program/VAO */\n        } else {\n            if (cur != HQ_GEO) {\n                p_glUseProgram(s_geo_prog);\n'

GL_GRID_OLD_15 = '                depth_apply_ex(c->depth, 1);\n                tex_draw_passes_ex(c->vcount, c->semi, c->mask, c->check, 0, -1);\n                depth_restore_ex(c->depth, 1, c->vcount);\n            } else {\n                if (cur != HQ_GEO) {\n                    p_glUseProgram(s_geo_prog);\n'

GL_GRID_NEW_15 = '                depth_apply_ex(c->depth, 1);\n                tex_draw_passes_ex(c->vcount, c->semi, c->mask, c->check, 0, -1);\n                depth_restore_ex(c->depth, 1, c->vcount);\n            } else if (c->kind == HQ_GEO_GRID) {\n                if (c->semi >= 0) apply_psx_blend(c->semi); else glDisable(GL_BLEND);\n                mask_stencil_ex(c->mask, c->check);\n                geo_grid_draw(c->vcount, c->semi, 0, c->mask, c->check, -1,\n                              s_shift_hi, (float)(-T->x0), (float)ww / 2.0f, 1.0f, 0.0f,\n                              s_hq_v + c->vfirst, s_hq_g + c->gfirst);\n                cur = 0;   /* the grid draw bound its own program/VAO */\n            } else {\n                if (cur != HQ_GEO) {\n                    p_glUseProgram(s_geo_prog);\n'

GL_GRID_OLD_16 = '    if (!s_hiw) p_glBindFramebuffer(PSXGL_FRAMEBUFFER, s_hr_fbo);\n    s_hq_n = 0;\n    s_hq_vn = 0;\n}\n\nstatic void flush_tex_batch_impl(void) {\n'

GL_GRID_NEW_16 = '    if (!s_hiw) p_glBindFramebuffer(PSXGL_FRAMEBUFFER, s_hr_fbo);\n    s_hq_n = 0;\n    s_hq_vn = 0;\n    s_hq_gn = 0;\n}\n\nstatic void flush_tex_batch_impl(void) {\n'

GL_GRID_OLD_17 = 'static int   s_fbl_at[FLATBATCH_MAXL];\nstatic int   s_fbl_n = 0;\nstatic int   s_fb_gate = 0;\n\nstatic int mirror_flat_batch_center_only(int nverts) {\n    if (!wide_center_is_canonical() || nverts <= 0) return 0;\n'

GL_GRID_NEW_17 = 'static int   s_fbl_at[FLATBATCH_MAXL];\nstatic int   s_fbl_n = 0;\nstatic int   s_fb_gate = 0;\n/* Guest-grid flat batch: s_fb_grid is a batch key (a grid batch is drawn\n * with the GEO_GRID program from s_fbg side data, so grid and legacy\n * triangles never share a batch). s_fbg holds GG_STRIDE floats per\n * vertex, valid only while s_fb_grid is set. */\nstatic int   s_fb_grid = 0;\nstatic float s_fbg[FLATBATCH_MAXV * GG_STRIDE];\n\nstatic int mirror_flat_batch_center_only(int nverts) {\n    if (!wide_center_is_canonical() || nverts <= 0) return 0;\n'

GL_GRID_OLD_18 = "    return n;\n}\n\nstatic void flush_flat_batch_impl(void) {\n    if (s_fb_n == 0) return;\n    wide_stencil_ready();   /* before any of this batch's GL state */\n"

GL_GRID_NEW_18 = "    return n;\n}\n\n/* Draw nverts grid-batch triangles (fb stride 6 + fbg side data) on the\n * currently bound surface with its projection values. The caller has set\n * blend and mask_stencil. BCLS marking runs with the grid program's own\n * uniform locations swapped into the shared globals for the duration. */\nstatic void geo_grid_draw(int nverts, int semi, int dmode, int mask, int check,\n                          int b3d, float shift, float xoff, float xhalf,\n                          float xscale, float xcenter,\n                          const float *fb, const float *fbg) {\n    p_glUseProgram(s_geo_grid_prog);\n    p_glUniform1f(s_geog_uShift, shift);\n    p_glUniform1f(s_geog_uXoff, xoff);\n    p_glUniform1f(s_geog_uXhalf, xhalf);\n    p_glUniform1f(s_geog_uXscale, xscale);\n    p_glUniform1f(s_geog_uXcenter, xcenter);\n    p_glUniform1i(s_geog_uDither, s_dither_mode);\n    p_glUniform1i(s_geog_uAccurate, s_accurate_blend);\n    p_glBindVertexArray(s_geo_grid_vao);\n    p_glBindBuffer(PSXGL_ARRAY_BUFFER, s_geo_vbo);\n    p_glBufferData(PSXGL_ARRAY_BUFFER, (ptrdiff_t)(nverts * 6 * sizeof(float)),\n                   fb, PSXGL_STREAM_DRAW);\n    p_glBindBuffer(PSXGL_ARRAY_BUFFER, s_geo_grid_vbo);\n    p_glBufferData(PSXGL_ARRAY_BUFFER, (ptrdiff_t)(nverts * GG_STRIDE * sizeof(float)),\n                   fbg, PSXGL_STREAM_DRAW);\n    GLint save_bcls = s_geo_uBcls, save_badd = s_geo_uBadd;\n    s_geo_uBcls = s_geog_uBcls; s_geo_uBadd = s_geog_uBadd;\n    BCLS_DRAW(semi, dmode, mask, check, b3d, glDrawArrays(GL_TRIANGLES, 0, nverts));\n    s_geo_uBcls = save_bcls; s_geo_uBadd = save_badd;\n}\n\nstatic void flush_flat_batch_impl(void) {\n    if (s_fb_n == 0) return;\n    wide_stencil_ready();   /* before any of this batch's GL state */\n"

GL_GRID_OLD_19 = '    const int b3d = nl ? -1 : s_fb_b3d;   /* lines in the batch: mixed */\n    int dmode = s_fb_depth;\n    GLenum fmode = s_fb_mode;\n    s_fb_n = 0;\n    s_fbl_n = 0;\n\n    hr_begin(1);\n    if (semi >= 0) apply_psx_blend(semi); else glDisable(GL_BLEND);\n    mask_stencil(mask);\n    if (fmode == GL_LINES) glLineWidth((float)s_hr_scale);\n    p_glUseProgram(s_geo_prog);\n    p_glBindVertexArray(s_geo_vao);\n    p_glBindBuffer(PSXGL_ARRAY_BUFFER, s_geo_vbo);\n    if (nl) memcpy(&s_fb[nverts * 6], s_fbl, (size_t)nl * 2 * 6 * sizeof(float));\n    p_glBufferData(PSXGL_ARRAY_BUFFER, (ptrdiff_t)((nverts + 2 * nl) * 6 * sizeof(float)),\n                   s_fb, PSXGL_STREAM_DRAW);\n    depth_apply_ex(dmode, 0);\n    BCLS_DRAW(semi, dmode, mask, s_mask_check, b3d, if (nl) flat_batch_draw_hr_lines(nverts, nl);\n                       else glDrawArrays(fmode, 0, nverts));\n    depth_restore_ex(dmode, 0, nverts);\n    int mirror = g_wide_cur && !s_wide_suppress && s_ws_ablate != 1 &&\n                 !(!g_ws_bd_stretch_on && mirror_flat_batch_center_only(nverts));\n    if (mirror) {\n'

GL_GRID_NEW_19 = '    const int b3d = nl ? -1 : s_fb_b3d;   /* lines in the batch: mixed */\n    int dmode = s_fb_depth;\n    GLenum fmode = s_fb_mode;\n    const int grid = s_fb_grid;   /* grid batch: GEO_GRID program + s_fbg */\n    s_fb_n = 0;\n    s_fbl_n = 0;\n\n    hr_begin(1);\n    if (semi >= 0) apply_psx_blend(semi); else glDisable(GL_BLEND);\n    mask_stencil(mask);\n    if (grid) {\n        geo_grid_draw(nverts, semi, dmode, mask, s_mask_check, b3d,\n                      s_shift_hr, 0.0f, 512.0f, 1.0f, 0.0f, s_fb, s_fbg);\n    } else {\n        if (fmode == GL_LINES) glLineWidth((float)s_hr_scale);\n        p_glUseProgram(s_geo_prog);\n        p_glBindVertexArray(s_geo_vao);\n        p_glBindBuffer(PSXGL_ARRAY_BUFFER, s_geo_vbo);\n        if (nl) memcpy(&s_fb[nverts * 6], s_fbl, (size_t)nl * 2 * 6 * sizeof(float));\n        p_glBufferData(PSXGL_ARRAY_BUFFER, (ptrdiff_t)((nverts + 2 * nl) * 6 * sizeof(float)),\n                       s_fb, PSXGL_STREAM_DRAW);\n        depth_apply_ex(dmode, 0);\n        BCLS_DRAW(semi, dmode, mask, s_mask_check, b3d, if (nl) flat_batch_draw_hr_lines(nverts, nl);\n                           else glDrawArrays(fmode, 0, nverts));\n        depth_restore_ex(dmode, 0, nverts);\n    }\n    int mirror = g_wide_cur && !s_wide_suppress && s_ws_ablate != 1 &&\n                 !(!g_ws_bd_stretch_on && mirror_flat_batch_center_only(nverts));\n    if (mirror) {\n'

GL_GRID_OLD_20 = '        wst_note_draw(g_wide_cur, s_fb, nverts, 6, wide_dx(), sc, ce, s_mask_check);\n    }\n    s_hq_depth_cur = dmode;\n    if ((hiw_on() || (mirror && fmode == GL_TRIANGLES && wide_queue_live())) &&\n        hiw_enqueue_geo(s_fb, nverts, semi, mask, mirror, s_fb_gate)) mirror = 0;\n    s_hq_depth_cur = 0;\n\n'

GL_GRID_NEW_20 = '        wst_note_draw(g_wide_cur, s_fb, nverts, 6, wide_dx(), sc, ce, s_mask_check);\n    }\n    s_hq_depth_cur = dmode;\n    if (grid) {\n        if ((hiw_on() || (mirror && fmode == GL_TRIANGLES && wide_queue_live())) &&\n            hiw_enqueue_geo_grid(s_fb, s_fbg, nverts, semi, mask, mirror, s_fb_gate)) mirror = 0;\n    } else if ((hiw_on() || (mirror && fmode == GL_TRIANGLES && wide_queue_live())) &&\n        hiw_enqueue_geo(s_fb, nverts, semi, mask, mirror, s_fb_gate)) mirror = 0;\n    s_hq_depth_cur = 0;\n\n'

GL_GRID_OLD_21 = '        int dx = wide_dx();\n        s_bd_gate = s_fb_gate;\n        gl_perf_mirror_begin();\n        wide_target_begin(dx, s_geo_uXoff, s_geo_uXhalf);\n        wide_set_bd_scale(s_geo_uXscale, s_geo_uXcenter);\n        depth_apply_ex(dmode, 0);\n        if (s_ws_ablate != 2) BCLS_DRAW(semi, dmode, mask, s_mask_check, b3d, glDrawArrays(fmode, 0, nverts));\n        depth_restore_ex(dmode, 0, nverts);\n        wide_clear_bd_scale(s_geo_uXscale, s_geo_uXcenter);\n        wide_target_end(s_geo_uXoff, s_geo_uXhalf);\n        gl_perf_mirror_end();\n    }\n    hr_end();\n'

GL_GRID_NEW_21 = "        int dx = wide_dx();\n        s_bd_gate = s_fb_gate;\n        gl_perf_mirror_begin();\n        if (grid) {\n            /* The grid draw carries its own projection uniforms, so the\n             * mirror replicates wide_target_begin's surface setup with\n             * the grid program's values (wide_target_begin would poke\n             * the plain programs' shift locations). Blend + mask state\n             * carry over from the canonical draw, as on the plain path. */\n            float sc, ce;\n            wide_bd_scale(s_fb_gate, &sc, &ce);\n            if (s_ws_ablate != 3) p_glBindFramebuffer(PSXGL_FRAMEBUFFER, g_wide_cur);\n            glViewport(0, 0, g_wide_w * s_out_scale, VRAM_H * s_out_scale);\n            wide_band_scissor_x(view_pad_left, g_wide_w - view_pad_left - view_pad_right);\n            if (s_ws_ablate != 2)\n                geo_grid_draw(nverts, semi, dmode, mask, s_mask_check, b3d,\n                              s_hiw ? s_shift_hi : s_shift_hr, (float)dx,\n                              (float)g_wide_w / 2.0f, sc, ce, s_fb, s_fbg);\n            if (s_ws_ablate != 3) p_glBindFramebuffer(PSXGL_FRAMEBUFFER, s_hr_fbo);\n        } else {\n            wide_target_begin(dx, s_geo_uXoff, s_geo_uXhalf);\n            wide_set_bd_scale(s_geo_uXscale, s_geo_uXcenter);\n            depth_apply_ex(dmode, 0);\n            if (s_ws_ablate != 2) BCLS_DRAW(semi, dmode, mask, s_mask_check, b3d, glDrawArrays(fmode, 0, nverts));\n            depth_restore_ex(dmode, 0, nverts);\n            wide_clear_bd_scale(s_geo_uXscale, s_geo_uXcenter);\n            wide_target_end(s_geo_uXoff, s_geo_uXhalf);\n        }\n        gl_perf_mirror_end();\n    }\n    hr_end();\n"

GL_GRID_OLD_22 = '    const float y0 = (float)s_area_y1, y1 = (float)s_area_y2 + 1.0f;\n    const float q[6][2] = { {x0, y0}, {x1, y0}, {x0, y1}, {x1, y0}, {x1, y1}, {x0, y1} };\n    s_fb_mode = GL_TRIANGLES; s_fb_semi = -1; s_fb_mask = (int)s_mask_set;\n    s_fb_gate = 0; s_fb_depth = 2;\n    for (int i = 0; i < 6; i++) {\n        float *v = &s_fb[i * 6];\n        v[0] = q[i][0]; v[1] = q[i][1]; v[2] = v[3] = v[4] = 0.0f; v[5] = -1.0f;\n'

GL_GRID_NEW_22 = '    const float y0 = (float)s_area_y1, y1 = (float)s_area_y2 + 1.0f;\n    const float q[6][2] = { {x0, y0}, {x1, y0}, {x0, y1}, {x1, y0}, {x1, y1}, {x0, y1} };\n    s_fb_mode = GL_TRIANGLES; s_fb_semi = -1; s_fb_mask = (int)s_mask_set;\n    s_fb_gate = 0; s_fb_depth = 2; s_fb_grid = 0;\n    for (int i = 0; i < 6; i++) {\n        float *v = &s_fb[i * 6];\n        v[0] = q[i][0]; v[1] = q[i][1]; v[2] = v[3] = v[4] = 0.0f; v[5] = -1.0f;\n'

GL_GRID_OLD_23 = '    for (int k = 0; k < 3; k++) z[k] = o[k];\n}\n\nstatic void gpu_geometry(GLenum mode, const int *xs, const int *ys,\n                         const uint16_t *cs, int n, int semi) {\n    flush_tex_batch();   /* flat prim: drain textured draws first (order + program) */\n'

GL_GRID_NEW_23 = '    for (int k = 0; k < 3; k++) z[k] = o[k];\n}\n\n/* ---- guest-grid coverage (GL): CPU side ---------------------------------\n * Eligibility mirrors the software fix\'s grid_eligible as closely as the\n * GL architecture allows: the prim\'s geometry must BE the guest grid —\n * no precise (PGXP) vertices, no perspective provenance, not a rectangle\n * (rects keep their rect semantics), no HD replacement texture, and no\n * backdrop stretch (batch gate 0, so v_npos is the drawn position in\n * every pass). Unlike SW — whose native pass already tiles at 1x — GL\n * applies the grid at every internal scale: GL\'s geometric coverage is\n * what leaves span-boundary seams at 1x. PSX_GL_GRID=0 is a diagnostic\n * kill-switch restoring the legacy path exactly. */\nstatic int gg_enabled(void) {\n    static int on = -1;\n    if (on < 0) { const char *e = getenv("PSX_GL_GRID"); on = !(e && e[0] == \'0\'); }\n    return on;\n}\nstatic int gg_covviz(void) {\n    static int on = -1;\n    if (on < 0) { const char *e = getenv("PSX_GL_COV_VIZ"); on = (e && e[0] == \'1\'); }\n    return on;\n}\n/* Pack one prim\'s guest-grid record (GG_STRIDE floats; one layout for\n * the TEX slots 27.. and the GEO side array): sort the guest verts by Y\n * with the software rasterizer\'s exact scheme, then pack each vert as\n * one exact-integer float holding two 12-bit-biased coords\n * ((x+2048) + (y+2048)*4096 <= 2^24-1, exact in float32); the fourth\n * float is the ownership-test flag. The vertex shader rebuilds the\n * native-DDA edge steps from the verts (gg_decode), so nothing else\n * needs to travel. Returns 0 (record left untouched) when a coord is\n * outside the packable range — the caller then keeps the legacy path;\n * PS1 vertex + drawing-offset coords are 11-bit fields, so the range\n * [-2048, 2047] covers every value the hardware can express. The data\n * is packed whenever the prim is eligible, even with the ownership\n * test switched off, so the coverage-viz hash can still identify\n * prims in the legacy configuration. */\nstatic int gg_pack(float *dst, const int *xs, const int *ys, int on) {\n    int x[3] = { xs[0], xs[1], xs[2] }, y[3] = { ys[0], ys[1], ys[2] };\n    static const int pairs[3][2] = { {0, 1}, {0, 2}, {1, 2} };\n    for (int p = 0; p < 3; p++) {\n        int a = pairs[p][0], b = pairs[p][1];\n        if (y[a] > y[b]) {\n            int t;\n            t = x[a]; x[a] = x[b]; x[b] = t;\n            t = y[a]; y[a] = y[b]; y[b] = t;\n        }\n    }\n    for (int i = 0; i < 3; i++)\n        if (x[i] < -2048 || x[i] > 2047 || y[i] < -2048 || y[i] > 2047)\n            return 0;\n    for (int i = 0; i < 3; i++)\n        dst[i] = (float)((x[i] + 2048) + (y[i] + 2048) * 4096);\n    dst[3] = on ? 1.0f : 0.0f;\n    return 1;\n}\n/* Expand a grid prim\'s positions so GL generates fragments over every\n * guest pixel it owns (an owned pixel\'s sub-pixel centres lie within\n * ~sqrt(2) guest px of the geometric triangle: span endpoints are the\n * row\'s floored edge eval, so a centre sits < 1 px past an edge\n * horizontally and < 1 px vertically). Attributes extrapolate with the\n * same barycentric matrix the PGXP seam path uses; q is 0 (affine) for\n * every eligible prim, so nothing else needs extrapolation. */\n#define GG_EXPAND 1.5f\nstatic void gg_expand_tex(float *t0) {\n    float x[3], y[3], B[3][3];\n    for (int i = 0; i < 3; i++) { x[i] = t0[i * TEXV]; y[i] = t0[i * TEXV + 1]; }\n    if (seam_expand(x, y, GG_EXPAND, B)) {\n        seam_attr(B, t0 + 2, TEXV, 2, NULL);   /* uv */\n        seam_attr(B, t0 + 4, TEXV, 3, NULL);   /* colour */\n        for (int i = 0; i < 3; i++) { t0[i * TEXV] = x[i]; t0[i * TEXV + 1] = y[i]; }\n    }\n}\nstatic void gg_expand_geo(float *v0) {\n    float x[3], y[3], B[3][3];\n    for (int i = 0; i < 3; i++) { x[i] = v0[i * 6]; y[i] = v0[i * 6 + 1]; }\n    if (seam_expand(x, y, GG_EXPAND, B)) {\n        seam_attr(B, v0 + 2, 6, 3, NULL);      /* colour */\n        for (int i = 0; i < 3; i++) { v0[i * 6] = x[i]; v0[i * 6 + 1] = y[i]; }\n    }\n}\n\nstatic void gpu_geometry(GLenum mode, const int *xs, const int *ys,\n                         const uint16_t *cs, int n, int semi) {\n    flush_tex_batch();   /* flat prim: drain textured draws first (order + program) */\n'

GL_GRID_OLD_24 = '    if (mode == GL_LINES && n == 2 && s_hr_scale == 1 && !s_hiw && g_wide_cur &&\n        !bd_prim_gate(xs, n, 0)) {\n        if (s_fb_n > 0 && (s_fb_mode != GL_LINES || s_fb_semi != semi ||\n                           s_fb_mask != (int)s_mask_set || s_fb_gate != 0 || s_fb_depth != 0))\n            flush_flat_batch();\n        if (s_fb_n + 2 > FLATBATCH_MAXV)\n            flush_flat_batch();\n        s_fb_mode = GL_LINES;\n        s_fb_gate = 0;\n        s_fb_depth = 0;   /* lines are 2D: never depth-tested (G1.14) */\n        s_fb_semi = semi;\n        s_fb_mask = (int)s_mask_set;\n'

GL_GRID_NEW_24 = '    if (mode == GL_LINES && n == 2 && s_hr_scale == 1 && !s_hiw && g_wide_cur &&\n        !bd_prim_gate(xs, n, 0)) {\n        if (s_fb_n > 0 && (s_fb_mode != GL_LINES || s_fb_semi != semi ||\n                           s_fb_mask != (int)s_mask_set || s_fb_gate != 0 || s_fb_depth != 0 ||\n                           s_fb_grid != 0))\n            flush_flat_batch();\n        if (s_fb_n + 2 > FLATBATCH_MAXV)\n            flush_flat_batch();\n        s_fb_mode = GL_LINES;\n        s_fb_gate = 0;\n        s_fb_grid = 0;\n        s_fb_depth = 0;   /* lines are 2D: never depth-tested (G1.14) */\n        s_fb_semi = semi;\n        s_fb_mask = (int)s_mask_set;\n'

GL_GRID_OLD_25 = '        }\n        line_to_quad(lv, quad);\n        if (s_fb_n > 0 && (s_fb_mode != GL_TRIANGLES || s_fb_semi != semi ||\n                           s_fb_mask != (int)s_mask_set || s_fb_gate != 0 || s_fb_depth != 0))\n            flush_flat_batch();\n        if (s_fb_n + 6 > FLATBATCH_MAXV)\n            flush_flat_batch();\n'

GL_GRID_NEW_25 = '        }\n        line_to_quad(lv, quad);\n        if (s_fb_n > 0 && (s_fb_mode != GL_TRIANGLES || s_fb_semi != semi ||\n                           s_fb_mask != (int)s_mask_set || s_fb_gate != 0 || s_fb_depth != 0 ||\n                           s_fb_grid != 0))\n            flush_flat_batch();\n        if (s_fb_n + 6 > FLATBATCH_MAXV)\n            flush_flat_batch();\n'

GL_GRID_OLD_26 = '        /* The expanded line is still world geometry. A previous backdrop\n         * batch may have left its stretch key set even after being flushed. */\n        s_fb_gate = 0;\n        s_fb_semi = semi;\n        s_fb_mask = (int)s_mask_set;\n        s_fb_depth = 0;   /* lines are 2D: never depth-tested (G1.14) */\n'

GL_GRID_NEW_26 = '        /* The expanded line is still world geometry. A previous backdrop\n         * batch may have left its stretch key set even after being flushed. */\n        s_fb_gate = 0;\n        s_fb_grid = 0;\n        s_fb_semi = semi;\n        s_fb_mask = (int)s_mask_set;\n        s_fb_depth = 0;   /* lines are 2D: never depth-tested (G1.14) */\n'

GL_GRID_OLD_27 = '    if (dmode) depth_before_tri();\n    /* Bloom on: 3D sources and the rest never share a batch (BCLS_DRAW). */\n    const int b3d = s_bcls_on && n == 3 && pgxp_tri_is_3d();\n    if (s_fb_n > 0 && (s_fb_mode != GL_TRIANGLES || s_fb_semi != semi ||\n                       s_fb_mask != (int)s_mask_set || s_fb_gate != gate ||\n                       s_fb_depth != dmode || s_fb_b3d != b3d))\n        flush_flat_batch();\n    if (s_fb_n + n > FLATBATCH_MAXV)\n        flush_flat_batch();\n'

GL_GRID_NEW_27 = '    if (dmode) depth_before_tri();\n    /* Bloom on: 3D sources and the rest never share a batch (BCLS_DRAW). */\n    const int b3d = s_bcls_on && n == 3 && pgxp_tri_is_3d();\n    /* Guest-grid eligibility (flat/gouraud): mirrors the textured gate —\n     * guest-integer prims only, no depth, no backdrop stretch. n == 3\n     * only: the record describes one triangle. */\n    float ggbuf[GG_STRIDE];\n    const int gg = n == 3 && gg_enabled() && s_geo_grid_prog && !precise &&\n                   dmode == 0 && gate == 0 && gg_pack(ggbuf, xs, ys, 1);\n    if (s_fb_n > 0 && (s_fb_mode != GL_TRIANGLES || s_fb_semi != semi ||\n                       s_fb_mask != (int)s_mask_set || s_fb_gate != gate ||\n                       s_fb_depth != dmode || s_fb_b3d != b3d ||\n                       s_fb_grid != gg))\n        flush_flat_batch();\n    if (s_fb_n + n > FLATBATCH_MAXV)\n        flush_flat_batch();\n'

GL_GRID_OLD_28 = '    s_fb_gate = gate;\n    s_fb_depth = dmode;\n    s_fb_b3d = b3d;\n\n    float mask_a = (s_mask_set ? 1.0f : 0.0f) + geo_dither_code();\n    float *v0 = &s_fb[s_fb_n * 6];\n    for (int i = 0; i < n; i++) {\n        float *v = &s_fb[s_fb_n * 6];\n        v[0] = precise ? s_pc_x[i] : (float)xs[i];\n'

GL_GRID_NEW_28 = '    s_fb_gate = gate;\n    s_fb_depth = dmode;\n    s_fb_b3d = b3d;\n    s_fb_grid = gg;\n\n    float mask_a = (s_mask_set ? 1.0f : 0.0f) + geo_dither_code();\n    float *v0 = &s_fb[s_fb_n * 6];\n    const int fb_base = s_fb_n;\n    for (int i = 0; i < n; i++) {\n        float *v = &s_fb[s_fb_n * 6];\n        v[0] = precise ? s_pc_x[i] : (float)xs[i];\n'

GL_GRID_OLD_29 = '        v[4] = (((cs[i] >> 10) & 0x1F) << 3) / 255.0f;\n        v[5] = mask_a;\n        s_fb_n++;\n    }\n    if (n == 3 && pgxp_tri_is_3d()) {\n        float z[3] = { s_pz[0], s_pz[1], s_pz[2] };\n'

GL_GRID_NEW_29 = '        v[4] = (((cs[i] >> 10) & 0x1F) << 3) / 255.0f;\n        v[5] = mask_a;\n        s_fb_n++;\n    }\n    if (gg) {\n        for (int i = 0; i < 3; i++)\n            memcpy(&s_fbg[(fb_base + i) * GG_STRIDE], ggbuf, sizeof ggbuf);\n        gg_expand_geo(v0);\n    }\n    if (n == 3 && pgxp_tri_is_3d()) {\n        float z[3] = { s_pz[0], s_pz[1], s_pz[2] };\n'

GL_GRID_OLD_30 = '            vp[23] = (float)hd.source_width; vp[24] = (float)hd.source_height;\n            vp[25] = (float)hd.alpha_mode;\n            vp[26] = (float)filter; /* per-primitive filter */\n        }\n        if (pgxp_tri_is_3d()) {\n            float *t0 = &s_tb[s_tb_n * TEXV];\n'

GL_GRID_NEW_30 = '            vp[23] = (float)hd.source_width; vp[24] = (float)hd.source_height;\n            vp[25] = (float)hd.alpha_mode;\n            vp[26] = (float)filter; /* per-primitive filter */\n        }\n        /* Guest-grid coverage record (slots 27..38): packed for every\n         * eligible prim; the ownership test is armed (on=1) unless the\n         * PSX_GL_GRID kill-switch is set, and expansion pairs with it. */\n        {\n            const int gg_elig = !s_pc_valid && !s_pq_valid && !s_projected_uv_valid &&\n                                !s_prim_rect && !hd_tex && gate == 0;\n            float ggbuf[GG_STRIDE];\n            const int gg_ok = gg_elig && gg_pack(ggbuf, xs, ys, gg_enabled());\n            if (!gg_ok) memset(ggbuf, 0, sizeof ggbuf);\n            float *gp = &s_tb[s_tb_n * TEXV];\n            for (int i = 0; i < 3; i++) memcpy(gp + i * TEXV + 27, ggbuf, sizeof ggbuf);\n            if (gg_ok && gg_enabled()) gg_expand_tex(gp);\n        }\n        if (pgxp_tri_is_3d()) {\n            float *t0 = &s_tb[s_tb_n * TEXV];\n'

GL_GRID_OLD_31 = '\n    s_geo_prog  = build_program(GEO_VS, GEO_FS);\n    s_tex_prog  = build_program_ex(TEX_VS, TEX_FS, 1);\n    s_blit_prog = build_program(BLIT_VS, BLIT_FS);\n    s_blit_hi_prog = build_program(BLIT_VS_HI, BLIT_FS);\n    s_pack_prog = build_program(PACK_VS, PACK_FS);\n'

GL_GRID_NEW_31 = '\n    s_geo_prog  = build_program(GEO_VS, GEO_FS);\n    s_tex_prog  = build_program_ex(TEX_VS, TEX_FS, 1);\n    /* Guest-grid GEO variant: NON-fatal — a failed compile only disables\n     * the flat/gouraud half of the guest-grid fix (logged at VAO setup). */\n    s_geo_grid_prog = build_program(GEO_GRID_VS, GEO_GRID_FS);\n    s_blit_prog = build_program(BLIT_VS, BLIT_FS);\n    s_blit_hi_prog = build_program(BLIT_VS_HI, BLIT_FS);\n    s_pack_prog = build_program(PACK_VS, PACK_FS);\n'

GL_GRID_OLD_32 = '        s_tex_uShift = p_glGetUniformLocation(s_tex_prog, "u_shift");\n        s_geo_uZbias = p_glGetUniformLocation(s_geo_prog, "u_zbias");\n        s_tex_uZbias = p_glGetUniformLocation(s_tex_prog, "u_zbias");\n        p_glUseProgram(s_geo_prog);\n        p_glUniform1f(s_geo_uShift, shift);\n        /* Native-wide projection defaults: x translation 0, clip half-extent\n'

GL_GRID_NEW_32 = '        s_tex_uShift = p_glGetUniformLocation(s_tex_prog, "u_shift");\n        s_geo_uZbias = p_glGetUniformLocation(s_geo_prog, "u_zbias");\n        s_tex_uZbias = p_glGetUniformLocation(s_tex_prog, "u_zbias");\n        s_tex_uCovviz = p_glGetUniformLocation(s_tex_prog, "u_covviz");\n        if (s_geo_grid_prog) {\n            s_geog_uShift = p_glGetUniformLocation(s_geo_grid_prog, "u_shift");\n            s_geog_uXoff = p_glGetUniformLocation(s_geo_grid_prog, "u_xoff");\n            s_geog_uXhalf = p_glGetUniformLocation(s_geo_grid_prog, "u_xhalf");\n            s_geog_uXscale = p_glGetUniformLocation(s_geo_grid_prog, "u_xscale");\n            s_geog_uXcenter = p_glGetUniformLocation(s_geo_grid_prog, "u_xcenter");\n            s_geog_uZbias = p_glGetUniformLocation(s_geo_grid_prog, "u_zbias");\n            s_geog_uBcls = p_glGetUniformLocation(s_geo_grid_prog, "u_bcls");\n            s_geog_uBadd = p_glGetUniformLocation(s_geo_grid_prog, "u_badd");\n            s_geog_uDither = p_glGetUniformLocation(s_geo_grid_prog, "u_dither");\n            s_geog_uAccurate = p_glGetUniformLocation(s_geo_grid_prog, "u_accurate");\n        }\n        p_glUseProgram(s_geo_prog);\n        p_glUniform1f(s_geo_uShift, shift);\n        /* Native-wide projection defaults: x translation 0, clip half-extent\n'

GL_GRID_OLD_33 = '        p_glUniform1f(s_tex_uShift, shift);\n        p_glUniform1f(s_tex_uXoff, 0.0f);\n        p_glUniform1f(s_tex_uXhalf, 512.0f);\n        p_glUseProgram(s_blit_prog);\n        p_glUniform1f(p_glGetUniformLocation(s_blit_prog, "u_shift"), shift);\n        p_glUseProgram(0);\n'

GL_GRID_NEW_33 = '        p_glUniform1f(s_tex_uShift, shift);\n        p_glUniform1f(s_tex_uXoff, 0.0f);\n        p_glUniform1f(s_tex_uXhalf, 512.0f);\n        p_glUniform1i(s_tex_uCovviz, gg_covviz() ? 1 : 0);\n        p_glUseProgram(s_blit_prog);\n        p_glUniform1f(p_glGetUniformLocation(s_blit_prog, "u_shift"), shift);\n        p_glUseProgram(0);\n'

GL_GRID_OLD_34 = '        p_glVertexAttribPointer(11, 4, GL_FLOAT, GL_FALSE, st, (void*)(21*sizeof(float))); p_glEnableVertexAttribArray(11); /* HD source */\n        p_glVertexAttribPointer(12, 1, GL_FLOAT, GL_FALSE, st, (void*)(25*sizeof(float))); p_glEnableVertexAttribArray(12); /* HD alpha mode */\n        p_glVertexAttribPointer(13, 1, GL_FLOAT, GL_FALSE, st, (void*)(26*sizeof(float))); p_glEnableVertexAttribArray(13); /* filter */\n    }\n\n    p_glGenVertexArrays(1, &s_blit_vao);\n'

GL_GRID_NEW_34 = '        p_glVertexAttribPointer(11, 4, GL_FLOAT, GL_FALSE, st, (void*)(21*sizeof(float))); p_glEnableVertexAttribArray(11); /* HD source */\n        p_glVertexAttribPointer(12, 1, GL_FLOAT, GL_FALSE, st, (void*)(25*sizeof(float))); p_glEnableVertexAttribArray(12); /* HD alpha mode */\n        p_glVertexAttribPointer(13, 1, GL_FLOAT, GL_FALSE, st, (void*)(26*sizeof(float))); p_glEnableVertexAttribArray(13); /* filter */\n        p_glVertexAttribPointer(14, 4, GL_FLOAT, GL_FALSE, st, (void*)(27*sizeof(float))); p_glEnableVertexAttribArray(14); /* gg record */\n    }\n\n    /* Guest-grid GEO VAO: attribs 0/1 read the shared s_geo_vbo exactly\n     * like s_geo_vao; attribs 2..7 read the side buffer (GG_STRIDE). */\n    if (s_geo_grid_prog) {\n        GLsizei gst = GG_STRIDE * sizeof(float);\n        p_glGenVertexArrays(1, &s_geo_grid_vao);\n        p_glBindVertexArray(s_geo_grid_vao);\n        p_glBindBuffer(PSXGL_ARRAY_BUFFER, s_geo_vbo);\n        p_glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 6 * sizeof(float), (void *)0);\n        p_glEnableVertexAttribArray(0);\n        p_glVertexAttribPointer(1, 4, GL_FLOAT, GL_FALSE, 6 * sizeof(float), (void *)(2 * sizeof(float)));\n        p_glEnableVertexAttribArray(1);\n        p_glGenBuffers(1, &s_geo_grid_vbo);\n        p_glBindBuffer(PSXGL_ARRAY_BUFFER, s_geo_grid_vbo);\n        p_glVertexAttribPointer(2, 4, GL_FLOAT, GL_FALSE, gst, (void *)0);                  p_glEnableVertexAttribArray(2); /* gg record */\n        fprintf(stdout, "psxrecomp: GL guest-grid coverage active (PSX_GL_GRID=0 disables)\\n");\n    } else {\n        fprintf(stdout, "psxrecomp: GL guest-grid GEO program unavailable; flat/gouraud grid disabled\\n");\n    }\n\n    p_glGenVertexArrays(1, &s_blit_vao);\n'

GL_GRID_V2_MARKER = "guest pixel from gl_FragCoord (GL grid v2)"

GL_GRID_V2_OLD_0 = " * queue replays above it. */\nstatic void geo_grid_draw(int nverts, int semi, int dmode, int mask, int check,\n                          int b3d, float shift, float xoff, float xhalf,\n                          float xscale, float xcenter,\n                          const float *fb, const float *fbg);\n/* Every flush call site is attributed (gl_renderer_batch_sites): a flush that\n * draws a non-empty batch counts against its caller's source line. */\nstatic void batch_site_note(int kind, int line);\n"

GL_GRID_V2_NEW_0 = " * queue replays above it. */\nstatic void geo_grid_draw(int nverts, int semi, int dmode, int mask, int check,\n                          int b3d, float shift, float xoff, float xhalf,\n                          float xscale, float xcenter, int ggscale,\n                          const float *fb, const float *fbg);\n/* Guest-grid v2: set a program's guest origin/scale uniforms for the\n * draw target about to be drawn into. orgx is the guest x that window\n * column 0 of the target maps to (== -u_xoff of the draw); scale is\n * the target's host px per guest px. Defined with the gg CPU block. */\nstatic void gg_set_target(GLint uOrgx, GLint uScale, int orgx, int scale);\n/* Every flush call site is attributed (gl_renderer_batch_sites): a flush that\n * draws a non-empty batch counts against its caller's source line. */\nstatic void batch_site_note(int kind, int line);\n"

GL_GRID_V2_OLD_1 = '             s_geog_uBcls = -1, s_geog_uBadd = -1,\n             s_geog_uDither = -1, s_geog_uAccurate = -1;\nstatic GLint s_tex_uCovviz = -1;\n/* Textured vertex: pos(2) uv(2) col(4) tpage(2) clut(2) depth(1) raw(1) limits(4)\n * semi(1) q(1) twin(1) hd_source(4) hd_mode(1) filter(1) gg(4)\n * — per-prim texture state in flat attributes so prims batch (see flush_tex_batch).\n'

GL_GRID_V2_NEW_1 = '             s_geog_uBcls = -1, s_geog_uBadd = -1,\n             s_geog_uDither = -1, s_geog_uAccurate = -1;\nstatic GLint s_tex_uCovviz = -1;\n/* Guest-grid v2: per-target guest origin/scale uniform locations for the\n * two programs that run the ownership test (TEX + GEO_GRID). */\nstatic GLint s_tex_uGgOrgx = -1, s_tex_uGgScale = -1;\nstatic GLint s_geog_uGgOrgx = -1, s_geog_uGgScale = -1;\n/* Textured vertex: pos(2) uv(2) col(4) tpage(2) clut(2) depth(1) raw(1) limits(4)\n * semi(1) q(1) twin(1) hd_source(4) hd_mode(1) filter(1) gg(4)\n * — per-prim texture state in flat attributes so prims batch (see flush_tex_batch).\n'

GL_GRID_V2_OLD_2 = ' * recomputes the steps with an emulated int64 division (schoolbook\n * base-2^16, exact in uint arithmetic; dy > 0 there, matching the\n * branches the C code evaluates).\n * Guest position comes from v_npos: with u_shift = 0.5/S - 1/64 the\n * interpolated value at a fragment centre is FBO_px/S + 1/64 on every\n * surface (hr, wide, window tiles) at every scale, so floor(v_npos)\n * is the fragment\'s guest pixel, scale- and surface-independent. */\n#define GG_GLSL \\\n    "int gg_edge_x(int fx, uvec2 step, int k){\\n" \\\n    "  uint slo = step.x, shi = step.y;\\n" \\\n'

GL_GRID_V2_NEW_2 = ' * recomputes the steps with an emulated int64 division (schoolbook\n * base-2^16, exact in uint arithmetic; dy > 0 there, matching the\n * branches the C code evaluates).\n * Guest position (GL grid v2): the fragment\'s guest pixel is computed\n * from gl_FragCoord in integer arithmetic only (gg_guest_pixel below):\n * the draw target\'s guest origin (u_gg_orgx) and scale (u_gg_scale)\n * are per-draw uniforms set by the host wherever it sets u_xoff, and\n * orgx == -u_xoff holds at every draw site, so the map is exact on\n * every surface (hr, wide, window tiles) at every scale. v1 took the\n * guest pixel from floor(v_npos), a noperspective-interpolated\n * varying: exact on llvmpipe, but on NVIDIA hardware the interpolated\n * value deviates systematically by ~0.1-0.5 guest px on long thin\n * prims (growing toward the far end), flipping exactly the far-side\n * sub-row/sub-column of each guest block and splitting blocks between\n * rival prims (hardware COV_VIZ frame, 2026-11-11: 72% of the 3D view\n * multi-owned at 4x vs ~1% interiors on llvmpipe). gl_FragCoord is\n * the fragment\'s own window position — never interpolated — so the\n * v2 map does not depend on any float rounding mode. v_npos remains\n * in use for dithering only. (guest pixel from gl_FragCoord (GL grid v2)) */\n#define GG_GLSL \\\n    "int gg_edge_x(int fx, uvec2 step, int k){\\n" \\\n    "  uint slo = step.x, shi = step.y;\\n" \\\n'

GL_GRID_V2_OLD_3 = '    "                      : gg_edge_x(p1.x, s2, g.y - p0.z);\\n" \\\n    "  int lo = min(xa, xb), hi = max(xa, xb);\\n" \\\n    "  return g.x >= lo && g.x < hi;\\n" \\\n    "}\\n" \\\n    "vec3 gg_hash_colour(ivec3 p0, ivec2 p1, uvec2 sl){\\n" \\\n    "  uint h = uint(p0.x * 73856093) ^ uint(p0.y * 19349663) ^ uint(p1.x * 83492791) ^ sl.x ^ (sl.y << 7);\\n" \\\n'

GL_GRID_V2_NEW_3 = '    "                      : gg_edge_x(p1.x, s2, g.y - p0.z);\\n" \\\n    "  int lo = min(xa, xb), hi = max(xa, xb);\\n" \\\n    "  return g.x >= lo && g.x < hi;\\n" \\\n    "}\\n" \\\n    "uniform int u_gg_orgx;   /* guest x at this target\'s window column 0 */\\n" \\\n    "uniform int u_gg_scale;  /* target scale: host px per guest px */\\n" \\\n    "ivec2 gg_guest_pixel(){\\n" \\\n    "  ivec2 fc = ivec2(gl_FragCoord.xy - 0.5);\\n" \\\n    "  int sc = u_gg_scale > 0 ? u_gg_scale : 1;\\n" \\\n    "  return ivec2(u_gg_orgx + fc.x / sc, fc.y / sc);\\n" \\\n    "}\\n" \\\n    "vec3 gg_hash_colour(ivec3 p0, ivec2 p1, uvec2 sl){\\n" \\\n    "  uint h = uint(p0.x * 73856093) ^ uint(p0.y * 19349663) ^ uint(p1.x * 83492791) ^ sl.x ^ (sl.y << 7);\\n" \\\n'

GL_GRID_V2_OLD_4 = '    PSX_DITHER_GLSL\n    GG_GLSL\n    "void main(){\\n"\n    "  if (!gg_owned(ivec2(floor(v_npos)), v_gg_p0, v_gg_p1, v_gg_sl, v_gg_s1, v_gg_s2)) discard;\\n"\n    "  if (u_bcls != 0) { if (u_bcls == 1 && !(v_b3d > 0.999 || u_badd != 0)) discard; frag = vec4(0.0); return; }\\n"\n    "  frag = v_cp != 0 ? vec4(v_col_p, v_col.a) : v_col;\\n"\n    "  frag.rgb = psx_dither(frag.rgb, v_npos, v_dith); }\\n";\n'

GL_GRID_V2_NEW_4 = '    PSX_DITHER_GLSL\n    GG_GLSL\n    "void main(){\\n"\n    "  if (!gg_owned(gg_guest_pixel(), v_gg_p0, v_gg_p1, v_gg_sl, v_gg_s1, v_gg_s2)) discard;\\n"\n    "  if (u_bcls != 0) { if (u_bcls == 1 && !(v_b3d > 0.999 || u_badd != 0)) discard; frag = vec4(0.0); return; }\\n"\n    "  frag = v_cp != 0 ? vec4(v_col_p, v_col.a) : v_col;\\n"\n    "  frag.rgb = psx_dither(frag.rgb, v_npos, v_dith); }\\n";\n'

GL_GRID_V2_OLD_5 = '    "   * not own (per the native DDA spans) does not exist. Runs before\\n"\n    "   * every sampling/blend decision so all passes (colour, STP split,\\n"\n    "   * stencil fixup, bloom marking) agree on coverage exactly. */\\n"\n    "  if (v_gg_on != 0 && !gg_owned(ivec2(floor(v_npos)), v_gg_p0, v_gg_p1, v_gg_sl, v_gg_s1, v_gg_s2)) discard;\\n"\n    "  if (u_covviz != 0) { frag = vec4(gg_hash_colour(v_gg_p0, v_gg_p1, v_gg_sl), 1.0); blend_factor = vec4(0.0); return; }\\n"\n    "  int stp; vec3 rgb;\\n"\n    "  int fmode = v_filter & 15;\\n"\n'

GL_GRID_V2_NEW_5 = '    "   * not own (per the native DDA spans) does not exist. Runs before\\n"\n    "   * every sampling/blend decision so all passes (colour, STP split,\\n"\n    "   * stencil fixup, bloom marking) agree on coverage exactly. */\\n"\n    "  if (v_gg_on != 0 && !gg_owned(gg_guest_pixel(), v_gg_p0, v_gg_p1, v_gg_sl, v_gg_s1, v_gg_s2)) discard;\\n"\n    "  if (u_covviz != 0) { frag = vec4(gg_hash_colour(v_gg_p0, v_gg_p1, v_gg_sl), 1.0); blend_factor = vec4(0.0); return; }\\n"\n    "  int stp; vec3 rgb;\\n"\n    "  int fmode = v_filter & 15;\\n"\n'

GL_GRID_V2_OLD_6 = '    wide_band_scissor_x(view_pad_left, g_wide_w - view_pad_left - view_pad_right);\n    p_glUniform1f(uXoff, (float)dx);\n    p_glUniform1f(uXhalf, (float)g_wide_w / 2.0f);\n}\nstatic void wide_target_end(GLint uXoff, GLint uXhalf) {\n    if (s_hiw)\n        p_glUniform1f(uXoff == s_tex_uXoff ? s_tex_uShift : s_geo_uShift, s_shift_hr);\n    p_glUniform1f(uXoff, 0.0f);\n    p_glUniform1f(uXhalf, 512.0f);\n    if (s_ws_ablate != 3)\n        p_glBindFramebuffer(PSXGL_FRAMEBUFFER, s_hr_fbo);\n}\n'

GL_GRID_V2_NEW_6 = '    wide_band_scissor_x(view_pad_left, g_wide_w - view_pad_left - view_pad_right);\n    p_glUniform1f(uXoff, (float)dx);\n    p_glUniform1f(uXhalf, (float)g_wide_w / 2.0f);\n    if (uXoff == s_tex_uXoff)   /* guest-grid v2: wide surface, s_out_scale */\n        gg_set_target(s_tex_uGgOrgx, s_tex_uGgScale, -dx, s_out_scale);\n}\nstatic void wide_target_end(GLint uXoff, GLint uXhalf) {\n    if (s_hiw)\n        p_glUniform1f(uXoff == s_tex_uXoff ? s_tex_uShift : s_geo_uShift, s_shift_hr);\n    p_glUniform1f(uXoff, 0.0f);\n    p_glUniform1f(uXhalf, 512.0f);\n    if (uXoff == s_tex_uXoff)   /* guest-grid v2: back on the hr surface */\n        gg_set_target(s_tex_uGgOrgx, s_tex_uGgScale, 0, s_hr_scale);\n    if (s_ws_ablate != 3)\n        p_glBindFramebuffer(PSXGL_FRAMEBUFFER, s_hr_fbo);\n}\n'

GL_GRID_V2_OLD_7 = '                cur = HQ_TEX;\n            }\n            p_glUniform1f(s_tex_uXoff, (float)c->wdx);\n            p_glUniform1f(s_tex_uXscale, c->wscale);\n            p_glUniform1f(s_tex_uXcenter, c->wcenter);\n            bind_textured_resources(c->tex, c->palette, c->hd_tex);\n'

GL_GRID_V2_NEW_7 = '                cur = HQ_TEX;\n            }\n            p_glUniform1f(s_tex_uXoff, (float)c->wdx);\n            gg_set_target(s_tex_uGgOrgx, s_tex_uGgScale, -c->wdx, s_out_scale);\n            p_glUniform1f(s_tex_uXscale, c->wscale);\n            p_glUniform1f(s_tex_uXcenter, c->wcenter);\n            bind_textured_resources(c->tex, c->palette, c->hd_tex);\n'

GL_GRID_V2_OLD_8 = '            mask_stencil_ex(c->mask, c->check);\n            geo_grid_draw(c->vcount, c->semi, 0, c->mask, c->check, -1,\n                          s_shift_hi, (float)c->wdx, (float)g_wide_w / 2.0f,\n                          c->wscale, c->wcenter,\n                          s_hq_v + c->vfirst, s_hq_g + c->gfirst);\n            cur = 0;   /* the grid draw bound its own program/VAO */\n        } else {\n'

GL_GRID_V2_NEW_8 = '            mask_stencil_ex(c->mask, c->check);\n            geo_grid_draw(c->vcount, c->semi, 0, c->mask, c->check, -1,\n                          s_shift_hi, (float)c->wdx, (float)g_wide_w / 2.0f,\n                          c->wscale, c->wcenter, s_out_scale,\n                          s_hq_v + c->vfirst, s_hq_g + c->gfirst);\n            cur = 0;   /* the grid draw bound its own program/VAO */\n        } else {\n'

GL_GRID_V2_OLD_9 = '                p_glUniform1f(s_tex_uShift, s_shift_hi);\n                p_glUniform1f(s_tex_uXoff, (float)(-T->x0));\n                p_glUniform1f(s_tex_uXhalf, (float)ww / 2.0f);\n                p_glUniform1i(s_uVram, 0);\n                p_glActiveTexture(PSXGL_TEXTURE0);\n                bound = 1;\n'

GL_GRID_V2_NEW_9 = '                p_glUniform1f(s_tex_uShift, s_shift_hi);\n                p_glUniform1f(s_tex_uXoff, (float)(-T->x0));\n                p_glUniform1f(s_tex_uXhalf, (float)ww / 2.0f);\n                gg_set_target(s_tex_uGgOrgx, s_tex_uGgScale, T->x0, s_out_scale);\n                p_glUniform1i(s_uVram, 0);\n                p_glActiveTexture(PSXGL_TEXTURE0);\n                bound = 1;\n'

GL_GRID_V2_OLD_10 = '                if (c->semi >= 0) apply_psx_blend(c->semi); else glDisable(GL_BLEND);\n                mask_stencil_ex(c->mask, c->check);\n                geo_grid_draw(c->vcount, c->semi, 0, c->mask, c->check, -1,\n                              s_shift_hi, (float)(-T->x0), (float)ww / 2.0f, 1.0f, 0.0f,\n                              s_hq_v + c->vfirst, s_hq_g + c->gfirst);\n                cur = 0;   /* the grid draw bound its own program/VAO */\n            } else {\n'

GL_GRID_V2_NEW_10 = '                if (c->semi >= 0) apply_psx_blend(c->semi); else glDisable(GL_BLEND);\n                mask_stencil_ex(c->mask, c->check);\n                geo_grid_draw(c->vcount, c->semi, 0, c->mask, c->check, -1,\n                              s_shift_hi, (float)(-T->x0), (float)ww / 2.0f, 1.0f, 0.0f, s_out_scale,\n                              s_hq_v + c->vfirst, s_hq_g + c->gfirst);\n                cur = 0;   /* the grid draw bound its own program/VAO */\n            } else {\n'

GL_GRID_V2_OLD_11 = '    p_glUniform1f(s_tex_uShift, s_shift_hr);\n    p_glUniform1f(s_tex_uXoff, 0.0f);\n    p_glUniform1f(s_tex_uXhalf, 512.0f);\n    hr_end();\n    /* Native-wide only: leave the hr surface bound, as an immediate mirror\n     * (wide_target_end) does. */\n'

GL_GRID_V2_NEW_11 = '    p_glUniform1f(s_tex_uShift, s_shift_hr);\n    p_glUniform1f(s_tex_uXoff, 0.0f);\n    p_glUniform1f(s_tex_uXhalf, 512.0f);\n    gg_set_target(s_tex_uGgOrgx, s_tex_uGgScale, 0, s_hr_scale);\n    hr_end();\n    /* Native-wide only: leave the hr surface bound, as an immediate mirror\n     * (wide_target_end) does. */\n'

GL_GRID_V2_OLD_12 = ' * uniform locations swapped into the shared globals for the duration. */\nstatic void geo_grid_draw(int nverts, int semi, int dmode, int mask, int check,\n                          int b3d, float shift, float xoff, float xhalf,\n                          float xscale, float xcenter,\n                          const float *fb, const float *fbg) {\n    p_glUseProgram(s_geo_grid_prog);\n    p_glUniform1f(s_geog_uShift, shift);\n    p_glUniform1f(s_geog_uXoff, xoff);\n    p_glUniform1f(s_geog_uXhalf, xhalf);\n'

GL_GRID_V2_NEW_12 = ' * uniform locations swapped into the shared globals for the duration. */\nstatic void geo_grid_draw(int nverts, int semi, int dmode, int mask, int check,\n                          int b3d, float shift, float xoff, float xhalf,\n                          float xscale, float xcenter, int ggscale,\n                          const float *fb, const float *fbg) {\n    p_glUseProgram(s_geo_grid_prog);\n    gg_set_target(s_geog_uGgOrgx, s_geog_uGgScale, -(int)xoff, ggscale);\n    p_glUniform1f(s_geog_uShift, shift);\n    p_glUniform1f(s_geog_uXoff, xoff);\n    p_glUniform1f(s_geog_uXhalf, xhalf);\n'

GL_GRID_V2_OLD_13 = '    mask_stencil(mask);\n    if (grid) {\n        geo_grid_draw(nverts, semi, dmode, mask, s_mask_check, b3d,\n                      s_shift_hr, 0.0f, 512.0f, 1.0f, 0.0f, s_fb, s_fbg);\n    } else {\n        if (fmode == GL_LINES) glLineWidth((float)s_hr_scale);\n        p_glUseProgram(s_geo_prog);\n'

GL_GRID_V2_NEW_13 = '    mask_stencil(mask);\n    if (grid) {\n        geo_grid_draw(nverts, semi, dmode, mask, s_mask_check, b3d,\n                      s_shift_hr, 0.0f, 512.0f, 1.0f, 0.0f, s_hr_scale, s_fb, s_fbg);\n    } else {\n        if (fmode == GL_LINES) glLineWidth((float)s_hr_scale);\n        p_glUseProgram(s_geo_prog);\n'

GL_GRID_V2_OLD_14 = '            if (s_ws_ablate != 2)\n                geo_grid_draw(nverts, semi, dmode, mask, s_mask_check, b3d,\n                              s_hiw ? s_shift_hi : s_shift_hr, (float)dx,\n                              (float)g_wide_w / 2.0f, sc, ce, s_fb, s_fbg);\n            if (s_ws_ablate != 3) p_glBindFramebuffer(PSXGL_FRAMEBUFFER, s_hr_fbo);\n        } else {\n            wide_target_begin(dx, s_geo_uXoff, s_geo_uXhalf);\n'

GL_GRID_V2_NEW_14 = '            if (s_ws_ablate != 2)\n                geo_grid_draw(nverts, semi, dmode, mask, s_mask_check, b3d,\n                              s_hiw ? s_shift_hi : s_shift_hr, (float)dx,\n                              (float)g_wide_w / 2.0f, sc, ce, s_out_scale, s_fb, s_fbg);\n            if (s_ws_ablate != 3) p_glBindFramebuffer(PSXGL_FRAMEBUFFER, s_hr_fbo);\n        } else {\n            wide_target_begin(dx, s_geo_uXoff, s_geo_uXhalf);\n'

GL_GRID_V2_OLD_15 = '    static int on = -1;\n    if (on < 0) { const char *e = getenv("PSX_GL_COV_VIZ"); on = (e && e[0] == \'1\'); }\n    return on;\n}\n/* Pack one prim\'s guest-grid record (GG_STRIDE floats; one layout for\n * the TEX slots 27.. and the GEO side array): sort the guest verts by Y\n'

GL_GRID_V2_NEW_15 = '    static int on = -1;\n    if (on < 0) { const char *e = getenv("PSX_GL_COV_VIZ"); on = (e && e[0] == \'1\'); }\n    return on;\n}\n/* Guest-grid v2: point a program\'s ownership test at the draw target\n * being drawn into. The matching program must be in use, as for every\n * other uniform write in this file. A location of -1 (program without\n * the test) is ignored by glUniform, as elsewhere. */\nstatic void gg_set_target(GLint uOrgx, GLint uScale, int orgx, int scale) {\n    p_glUniform1i(uOrgx, orgx);\n    p_glUniform1i(uScale, scale > 0 ? scale : 1);\n}\n/* Pack one prim\'s guest-grid record (GG_STRIDE floats; one layout for\n * the TEX slots 27.. and the GEO side array): sort the guest verts by Y\n'

GL_GRID_V2_OLD_16 = '        s_geo_uZbias = p_glGetUniformLocation(s_geo_prog, "u_zbias");\n        s_tex_uZbias = p_glGetUniformLocation(s_tex_prog, "u_zbias");\n        s_tex_uCovviz = p_glGetUniformLocation(s_tex_prog, "u_covviz");\n        if (s_geo_grid_prog) {\n            s_geog_uShift = p_glGetUniformLocation(s_geo_grid_prog, "u_shift");\n            s_geog_uXoff = p_glGetUniformLocation(s_geo_grid_prog, "u_xoff");\n'

GL_GRID_V2_NEW_16 = '        s_geo_uZbias = p_glGetUniformLocation(s_geo_prog, "u_zbias");\n        s_tex_uZbias = p_glGetUniformLocation(s_tex_prog, "u_zbias");\n        s_tex_uCovviz = p_glGetUniformLocation(s_tex_prog, "u_covviz");\n        s_tex_uGgOrgx = p_glGetUniformLocation(s_tex_prog, "u_gg_orgx");\n        s_tex_uGgScale = p_glGetUniformLocation(s_tex_prog, "u_gg_scale");\n        if (s_geo_grid_prog) {\n            s_geog_uShift = p_glGetUniformLocation(s_geo_grid_prog, "u_shift");\n            s_geog_uXoff = p_glGetUniformLocation(s_geo_grid_prog, "u_xoff");\n'

GL_GRID_V2_OLD_17 = '            s_geog_uBadd = p_glGetUniformLocation(s_geo_grid_prog, "u_badd");\n            s_geog_uDither = p_glGetUniformLocation(s_geo_grid_prog, "u_dither");\n            s_geog_uAccurate = p_glGetUniformLocation(s_geo_grid_prog, "u_accurate");\n        }\n        p_glUseProgram(s_geo_prog);\n        p_glUniform1f(s_geo_uShift, shift);\n'

GL_GRID_V2_NEW_17 = '            s_geog_uBadd = p_glGetUniformLocation(s_geo_grid_prog, "u_badd");\n            s_geog_uDither = p_glGetUniformLocation(s_geo_grid_prog, "u_dither");\n            s_geog_uAccurate = p_glGetUniformLocation(s_geo_grid_prog, "u_accurate");\n            s_geog_uGgOrgx = p_glGetUniformLocation(s_geo_grid_prog, "u_gg_orgx");\n            s_geog_uGgScale = p_glGetUniformLocation(s_geo_grid_prog, "u_gg_scale");\n        }\n        p_glUseProgram(s_geo_prog);\n        p_glUniform1f(s_geo_uShift, shift);\n'

GL_GRID_V2_OLD_18 = '        p_glUniform1f(s_tex_uShift, shift);\n        p_glUniform1f(s_tex_uXoff, 0.0f);\n        p_glUniform1f(s_tex_uXhalf, 512.0f);\n        p_glUniform1i(s_tex_uCovviz, gg_covviz() ? 1 : 0);\n        p_glUseProgram(s_blit_prog);\n        p_glUniform1f(p_glGetUniformLocation(s_blit_prog, "u_shift"), shift);\n'

GL_GRID_V2_NEW_18 = '        p_glUniform1f(s_tex_uShift, shift);\n        p_glUniform1f(s_tex_uXoff, 0.0f);\n        p_glUniform1f(s_tex_uXhalf, 512.0f);\n        gg_set_target(s_tex_uGgOrgx, s_tex_uGgScale, 0, s_hr_scale);\n        p_glUniform1i(s_tex_uCovviz, gg_covviz() ? 1 : 0);\n        p_glUseProgram(s_blit_prog);\n        p_glUniform1f(p_glGetUniformLocation(s_blit_prog, "u_shift"), shift);\n'

GL_GRID_V2_OLD_19 = '    s_shift_hr = s_shift_hi = 0.5f / (float)snew - 1.0f / 64.0f;\n    p_glUseProgram(s_geo_prog);  p_glUniform1f(s_geo_uShift, s_shift_hr);\n    p_glUseProgram(s_tex_prog);  p_glUniform1f(s_tex_uShift, s_shift_hr);\n    p_glUseProgram(s_blit_prog);\n    p_glUniform1f(p_glGetUniformLocation(s_blit_prog, "u_shift"), s_shift_hr);\n    p_glUseProgram(0);\n'

GL_GRID_V2_NEW_19 = '    s_shift_hr = s_shift_hi = 0.5f / (float)snew - 1.0f / 64.0f;\n    p_glUseProgram(s_geo_prog);  p_glUniform1f(s_geo_uShift, s_shift_hr);\n    p_glUseProgram(s_tex_prog);  p_glUniform1f(s_tex_uShift, s_shift_hr);\n    gg_set_target(s_tex_uGgOrgx, s_tex_uGgScale, 0, snew);\n    p_glUseProgram(s_blit_prog);\n    p_glUniform1f(p_glGetUniformLocation(s_blit_prog, "u_shift"), s_shift_hr);\n    p_glUseProgram(0);\n'

def patch_gl_guest_grid(template, log=print, fail_loud=False):
    """Fix hi-res coverage in the OpenGL renderer: guest-pixel
    ownership test in the fragment shader, replicating the native
    rasterizer's biased edge DDA exactly.

    Companion to patch_sw_guest_grid (same defect, other backend):
    PS1 Doom-class engines draw their world from
    one-guest-pixel-thin textured triangles whose abutting edges
    coincide on the guest grid. The software fix decides coverage
    on that grid; under OpenGL the batched pipeline rasterized the
    scaled geometry with exact geometric coverage, so slivers of
    each guest pixel were claimed by the wrong span or none — the
    dense vertical striping at internal resolution > 1x, and
    hairline seams along span boundaries even at 1x (the GL path
    has no native 1x authority to hide them). The patch carries
    each eligible prim's Y-sorted guest verts packed into one
    vec4 vertex attribute (slots 27..30 of the textured format;
    a side array + dedicated program for flat/gouraud geometry);
    the vertex shader rebuilds the native DDA edge steps from the
    verts with an emulated int64 division (schoolbook base-2^16,
    bit-exact vs psx_edge_step — fuzz-proven over 400k random
    prims / 116M pixels against psx_triangle_edges_at_y), and the
    fragment shader discards fragments whose guest pixel the prim
    does not own under the same rule the software rasterizer uses
    (gg_owned == psx_triangle_edges_at_y spans, bit-for-bit).
    Writers expand eligible triangles by ~1.5 guest px with
    barycentric attribute extrapolation (the seam machinery), so
    the owning prim's fragments reach every sub-pixel it owns;
    depth still resolves overlaps, and the guest position comes
    from the existing noperspective varying, making the test
    identical on hi-res, wide and window-tile surfaces at every
    scale including 1x. Eligibility mirrors the software fix:
    non-PGXP textured / shaded-textured and flat/gouraud triangles
    on gated-free (non-backdrop) draws; rects, projected/PGXP
    prims, HD-texture prims and depth-mode prims keep the legacy
    path. Vulkan has its own vertex format and is untouched.
    PSX_GL_GRID=0 disables the test (bit-identical legacy output,
    verified); PSX_GL_COV_VIZ=1 renders a per-prim coverage
    visualization. Verified on Final Doom (OpenGL): stripe-period
    modulation at 4x internal 14.16% -> 0.33% (wall centre) and
    8.58% -> 0.20% (upper-left wall) vs the software fix's
    0.67% / 0.21%; coverage-viz multi-prim guest blocks in wall
    interiors 51-57% -> 0.3-1.2% (residual full-view splits are
    depth-silhouette edges); HUD pixel-identical at 1x; texel
    footprint check 4.6/255; PGXP-on captures unchanged in class.
    Idempotent text patch on the template / framework cache and
    on scaffolded projects (found 2026-11-06).

    v2 phase (2026-11-11): the guest pixel is no longer read from
    the interpolated noperspective varying (floor(v_npos)). On
    NVIDIA hardware that interpolation deviates systematically by
    ~0.1-0.5 guest px on long thin prims, splitting guest blocks
    between rival prims (hardware COV_VIZ: 72% of the 3D view
    multi-owned at 4x, vs ~1% interiors on llvmpipe). v2 computes
    the guest pixel from gl_FragCoord in pure integer arithmetic
    (guest = u_gg_orgx + (fragcoord - 0.5) / u_gg_scale), with
    per-draw origin/scale uniforms set wherever the host sets
    u_xoff (orgx == -u_xoff at every site; hr surface at
    s_hr_scale, wide surfaces and window tiles at s_out_scale).
    The DDA ownership rule itself is unchanged. The v2 phase
    applies on top of a v1-patched tree (marker-gated), so trees
    converted with v1.0.19 upgrade in place on their next run.
    """
    try:
        src = Path(template) / "psxrecomp" / "runtime" / "src" / \
            "gpu_gl_renderer.c"
        if not src.exists():
            if fail_loud:
                raise StageError(
                    f"gpu_gl_renderer.c missing under {template} - "
                    f"cannot apply the OpenGL guest-grid patch")
            return
        text = src.read_text()
        orig = text
        if GL_GRID_MARKER not in text:
            pairs = [(GL_GRID_OLD_0, GL_GRID_NEW_0), (GL_GRID_OLD_1, GL_GRID_NEW_1), (GL_GRID_OLD_2, GL_GRID_NEW_2), (GL_GRID_OLD_3, GL_GRID_NEW_3), (GL_GRID_OLD_4, GL_GRID_NEW_4), (GL_GRID_OLD_5, GL_GRID_NEW_5), (GL_GRID_OLD_6, GL_GRID_NEW_6), (GL_GRID_OLD_7, GL_GRID_NEW_7), (GL_GRID_OLD_8, GL_GRID_NEW_8), (GL_GRID_OLD_9, GL_GRID_NEW_9), (GL_GRID_OLD_10, GL_GRID_NEW_10), (GL_GRID_OLD_11, GL_GRID_NEW_11), (GL_GRID_OLD_12, GL_GRID_NEW_12), (GL_GRID_OLD_13, GL_GRID_NEW_13), (GL_GRID_OLD_14, GL_GRID_NEW_14), (GL_GRID_OLD_15, GL_GRID_NEW_15), (GL_GRID_OLD_16, GL_GRID_NEW_16), (GL_GRID_OLD_17, GL_GRID_NEW_17), (GL_GRID_OLD_18, GL_GRID_NEW_18), (GL_GRID_OLD_19, GL_GRID_NEW_19), (GL_GRID_OLD_20, GL_GRID_NEW_20), (GL_GRID_OLD_21, GL_GRID_NEW_21), (GL_GRID_OLD_22, GL_GRID_NEW_22), (GL_GRID_OLD_23, GL_GRID_NEW_23), (GL_GRID_OLD_24, GL_GRID_NEW_24), (GL_GRID_OLD_25, GL_GRID_NEW_25), (GL_GRID_OLD_26, GL_GRID_NEW_26), (GL_GRID_OLD_27, GL_GRID_NEW_27), (GL_GRID_OLD_28, GL_GRID_NEW_28), (GL_GRID_OLD_29, GL_GRID_NEW_29), (GL_GRID_OLD_30, GL_GRID_NEW_30), (GL_GRID_OLD_31, GL_GRID_NEW_31), (GL_GRID_OLD_32, GL_GRID_NEW_32), (GL_GRID_OLD_33, GL_GRID_NEW_33), (GL_GRID_OLD_34, GL_GRID_NEW_34)]
            missing = [i for i, (old, _n) in enumerate(pairs)
                       if old not in text]
            if missing:
                if fail_loud:
                    raise StageError(
                        f"gpu_gl_renderer.c in {template} does NOT take "
                        f"the OpenGL guest-grid patch: {len(missing)} "
                        f"of {len(pairs)} anchors missing {missing} - "
                        f"this renderer variant is not covered by this "
                        f"tool version, and the game would ship WITHOUT "
                        f"the Doom / Final Doom striping fix (OpenGL "
                        f"renderer). Failing the job instead of shipping "
                        f"it broken; update the tool or the template.")
                return
            for old, new in pairs:
                text = text.replace(old, new, 1)
        if GL_GRID_V2_MARKER not in text:
            pairs2 = [(GL_GRID_V2_OLD_0, GL_GRID_V2_NEW_0), (GL_GRID_V2_OLD_1, GL_GRID_V2_NEW_1), (GL_GRID_V2_OLD_2, GL_GRID_V2_NEW_2), (GL_GRID_V2_OLD_3, GL_GRID_V2_NEW_3), (GL_GRID_V2_OLD_4, GL_GRID_V2_NEW_4), (GL_GRID_V2_OLD_5, GL_GRID_V2_NEW_5), (GL_GRID_V2_OLD_6, GL_GRID_V2_NEW_6), (GL_GRID_V2_OLD_7, GL_GRID_V2_NEW_7), (GL_GRID_V2_OLD_8, GL_GRID_V2_NEW_8), (GL_GRID_V2_OLD_9, GL_GRID_V2_NEW_9), (GL_GRID_V2_OLD_10, GL_GRID_V2_NEW_10), (GL_GRID_V2_OLD_11, GL_GRID_V2_NEW_11), (GL_GRID_V2_OLD_12, GL_GRID_V2_NEW_12), (GL_GRID_V2_OLD_13, GL_GRID_V2_NEW_13), (GL_GRID_V2_OLD_14, GL_GRID_V2_NEW_14), (GL_GRID_V2_OLD_15, GL_GRID_V2_NEW_15), (GL_GRID_V2_OLD_16, GL_GRID_V2_NEW_16), (GL_GRID_V2_OLD_17, GL_GRID_V2_NEW_17), (GL_GRID_V2_OLD_18, GL_GRID_V2_NEW_18), (GL_GRID_V2_OLD_19, GL_GRID_V2_NEW_19)]
            missing2 = [i for i, (old, _n) in enumerate(pairs2)
                        if old not in text]
            if missing2:
                if fail_loud:
                    raise StageError(
                        f"gpu_gl_renderer.c in {template} does NOT take "
                        f"the OpenGL guest-grid v2 phase: {len(missing2)} "
                        f"of {len(pairs2)} anchors missing {missing2} - "
                        f"this v1-patched renderer variant is not "
                        f"covered by the v2 phase (gl_FragCoord "
                        f"integer guest pixel; NVIDIA interpolation "
                        f"fix). Failing the job instead of shipping "
                        f"it without the v2 fix; update the tool or "
                        f"the template.")
                return
            for old, new in pairs2:
                text = text.replace(old, new, 1)
        if text != orig:
            src.write_text(text)
            log("patched gpu_gl_renderer.c: guest-grid coverage for "
                "hi-res OpenGL rendering (Doom / Final Doom striping "
                "fix; OpenGL renderer, grid v2)")
    except StageError:
        raise
    except Exception as e:  # a patch failure must never break a run
        if fail_loud:
            raise StageError(
                f"OpenGL guest-grid patch failed on {template}: "
                f"{e}") from e
        log(f"gl guest-grid patch skipped: {e}")

def probe_cue(runner, cue, template, players=1, json_out=None):
    patch_probe_subdir_boot(template,
                            getattr(runner, "log", print))
    patch_probe_preflight(template,
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
        # Pin the binary's file name to job.name via the framework's
        # explicit EXE_NAME argument (psxrecomp_add_game_runtime
        # forwards it to psxrecomp_add_runtime_target, where an
        # explicit EXE_NAME wins). Without it the framework derives
        # OUTPUT_NAME as MAKE_C_IDENTIFIER(WINDOW_TITLE) - and that
        # rule prepends "_" when the identifier starts with a digit,
        # so "007Racing" linked as "_007Racing" while stage_build,
        # stage_verify and stage_package all look for job.name (the
        # 007 Racing / Tomorrow Never Dies / The World Is Not Enough
        # failures, 2026-11-07: the full pipeline ran, then "build
        # finished but .../007Racing is missing/empty"). For
        # letter-leading names EXE_NAME equals the derived name, so
        # existing titles produce identical binaries and packages.
        # The optional second group makes the sub idempotent: an
        # EXE_NAME line left by an earlier patch run is replaced,
        # never duplicated.
        (r'(WINDOW_TITLE\s+"[^"]*")(\n[ \t]*EXE_NAME\s+"[^"]*")?',
         f'\\g<1>\n    EXE_NAME "{job.name}"'),
        (r'MAX_PLAYERS\s+\d+', f'MAX_PLAYERS {players}'),
    ])
    r.log(f"CMakeLists patched for {stem} (players {players}): {changed}")
    if f"generated/{stem}_dispatch.c" not in cm.read_text():
        raise StageError("GEN_MARKER patch did not land - check CMakeLists")
    if f'EXE_NAME "{job.name}"' not in cm.read_text():
        raise StageError("EXE_NAME patch did not land - check CMakeLists")


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
        p = subprocess.run([nm, str(exe)], capture_output=True, text=True,
                           timeout=600)
        syms = p.stdout.lower()
        count = len(re.findall(r"\bfunc_[0-9a-f]{8}\b", syms))
        if count == 0 or entry not in syms:
            raise StageError(
                f"verify failed for {exe.name}: {count} game functions, "
                f"entry {entry} {'present' if entry in syms else 'MISSING'}")
        r.log(f"verify OK: {exe.name} has {count} game functions, entry present")


# ---------------------------------------------------------------- overlay staging
#
# Defect A (stutter investigation, 2026-11-05): games that load code
# from the disc at runtime (Gran Turismo, Tekken 1) keep that code in
# "overlays", and the runtime can compile overlays to native shards -
# but only when a toolchain is staged beside the executable. No
# shipped package ever staged one, so every overlay game ran its game
# code in the slow dirty-RAM interpreter permanently (~269k
# interpreted instructions per frame in Gran Turismo). The framework
# ships the staging machinery (psxrecomp/tools/release_stage.py -
# the single source of truth for the cache tag and toolchain layout);
# this section drives it. Nothing here re-derives a tag or a layout.

def overlay_dl_cache():
    """App-managed download cache for the framework's pinned toolchain
    archives. release_stage.py verifies SHA256 on every use."""
    return Path(__file__).resolve().parent.parent / "toolchain-dl-cache"


def _release_stage(r, job, args, timeout=3600):
    script = job.repo / "psxrecomp" / "tools" / "release_stage.py"
    if not script.exists():
        raise StageError(f"framework release_stage.py missing: {script}")
    return r.run_ok(["python3", script, *args], timeout=timeout)


def _cg_tag(r, job, game_toml, build_dir):
    """The overlay cache namespace for a PACKAGED game.toml, from
    release_stage.py cg-tag. The tag must come from the packaged
    game.toml (the one the shipped runtime loads), never the dev one.
    The config-hash query runs the LINUX recompiler for both
    platforms: the hash serializes canonical game.toml fields in code
    shared by every emitter build, so the value is platform-free;
    only the flavor comes from the target's own build dir."""
    repo = job.repo
    script = repo / "psxrecomp" / "tools" / "release_stage.py"
    try:
        out = subprocess.run(
            ["python3", script, "cg-tag",
             "--runtime-include", repo / "psxrecomp" / "runtime" / "include",
             "--recompiler", repo / "build-recompiler" / "psxrecomp-game",
             "--game-toml", game_toml,
             "--flavor-from-build", build_dir],
            capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise StageError("release_stage cg-tag timed out after 300s")
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if out.returncode != 0 or not lines:
        raise StageError("release_stage cg-tag failed: "
                         + (out.stderr or out.stdout)[-400:])
    return lines[-1]


def stage_emitters_mingw(r, job):
    """Cross-build the Windows emitter binaries (psxrecomp-game.exe,
    psxrecomp-bios.exe) from THIS project's framework tree.
    stage-toolchain --platform win stages these into the Windows
    package; an emitter from any other tree would produce shards
    under a codegen hash the shipped runtime never reads (the
    stale-recompiler failure class). The MinGW runtime DLLs the
    emitters import are copied beside them - the exact files this
    machine's compiler driver names - where release_stage looks."""
    repo = job.repo
    bdir = repo / "build-recompiler-mingw"
    game_exe = bdir / "psxrecomp-game.exe"
    bios_exe = bdir / "psxrecomp-bios.exe"
    if not (game_exe.exists() and bios_exe.exists()):
        tc = repo / "psxrecomp" / "cmake" / "toolchain-mingw-w64.cmake"
        r.run_ok(["cmake", "-S", repo / "psxrecomp" / "recompiler",
                  "-B", bdir, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
                  f"-DCMAKE_TOOLCHAIN_FILE={tc}"], timeout=900)
        r.run_ok(["cmake", "--build", bdir, "--target", "psxrecomp-game",
                  "psxrecomp-bios", "-j", job.settings.jobs],
                 timeout=1800)
    for exe in (game_exe, bios_exe):
        if not exe.exists() or exe.stat().st_size == 0:
            raise StageError(
                f"Windows emitter build finished but {exe} is missing/empty")
    copied = []
    for dll in ("libgcc_s_seh-1.dll", "libstdc++-6.dll",
                "libwinpthread-1.dll"):
        if (bdir / dll).exists():
            continue
        q = subprocess.run(["x86_64-w64-mingw32-gcc",
                            f"-print-file-name={dll}"],
                           capture_output=True, text=True, timeout=60)
        src = Path(q.stdout.strip())
        if src.is_file():
            shutil.copy2(src, bdir / dll)
            copied.append(dll)
    r.log("Windows emitters ready: psxrecomp-game.exe + psxrecomp-bios.exe"
          + (f" (MinGW DLLs staged: {', '.join(copied)})" if copied else ""))
    return bdir


def stage_toolchain_into(r, job, stage_dir, target):
    """Stage the framework's self-contained overlay toolchain into a
    package dir (release_stage.py stage-toolchain): pinned Python +
    TinyCC, THIS project's own emitter binaries, runtime headers,
    BIOS profiles, seeds. The runtime finds it beside the exe and
    builds its autocompile command from it (runtime main.cpp); no
    game.toml command is written - an explicit one would bypass the
    runtime's stale-toolchain hash check."""
    repo = job.repo
    if target == "linux":
        recomp_dir, platform = repo / "build-recompiler", "linux"
        gate = stage_dir / "overlay_toolchain" / "python" / "bin" / "python3"
    else:
        recomp_dir, platform = repo / "build-recompiler-mingw", "win"
        gate = stage_dir / "overlay_toolchain" / "python" / "python.exe"
    # stage-toolchain downloads its pinned Python/TinyCC payloads on
    # first use; a transient network timeout must not kill a whole
    # conversion, so retry a few times (the download cache keeps
    # whatever already arrived, and the step is idempotent). A
    # failure that survives every attempt still fails the job.
    args = [
        "stage-toolchain", "--stage", stage_dir,
        "--recomp-dir", recomp_dir,
        "--recomp-tools", repo / "psxrecomp" / "tools",
        "--recomp-include", repo / "psxrecomp" / "runtime" / "include",
        "--dl-cache", overlay_dl_cache(),
        "--platform", platform]
    for attempt in (1, 2, 3):
        try:
            _release_stage(r, job, args)
            break
        except StageError:
            if attempt == 3:
                raise
            r.log(f"stage-toolchain ({target}) attempt {attempt} "
                  "failed - retrying in 20 s (payload downloads are "
                  "cached, so a retry resumes where it stopped)")
            time.sleep(20)
    if not gate.exists():
        raise StageError(
            f"stage-toolchain finished but {gate} is missing - the "
            "runtime's autocompile gate would be false for this package")
    r.log(f"overlay toolchain staged ({target}) in {stage_dir}")


def _stage_cache(r, job, game_id, cache_src_root, stage, tag,
                 arch_abi=None, shared_ext=None):
    """Copy the shippable shards for `tag` out of a producer cache
    into <stage>/cache/<game_id>, through release_stage.py's own
    validation (exact layout match; producer files - generated .c,
    pair-locks, ABI memos, in-progress temp shards - never ship).
    Returns the staged shard count."""
    repo = job.repo
    count_file = repo / ".shard-count.txt"
    args = ["stage-cache", "--game-id", game_id,
            "--cache-src-root", cache_src_root,
            "--stage", stage, "--cg-tag", tag,
            "--count-file", count_file]
    if arch_abi:
        args += ["--arch-abi", arch_abi]
    if shared_ext:
        args += ["--shared-ext", shared_ext]
    _release_stage(r, job, args)
    try:
        return int(count_file.read_text().strip())
    finally:
        count_file.unlink(missing_ok=True)


WARM_MIN_S = 240        # shortest warm run (the no-overlay gate needs
                        # a fair boot + menu + attract window, even
                        # on a slow or busy machine - a run that ends
                        # before the game reaches gameplay would
                        # misread an overlay game as a static one)
WARM_MAX_S = 600        # hard cap
WARM_QUIET_S = 45       # stop this long after the last capture/compile
WARM_FIGHT_S = 240      # once overlay code starts executing, keep
                        # the session in gameplay at least this long
                        # before the quiet rule may end it: captures
                        # grow in bursts at round/stage transitions,
                        # and a 45 s lull mid-fight would ship a
                        # one-stage cache (measured: 19 shards vs
                        # 182 from near-identical sessions)


def _free_display():
    for n in range(77, 90):
        if not Path(f"/tmp/.X{n}-lock").exists():
            return n
    return None


def _warm_send_key(env, xdotool, title, key):
    """Best-effort synthetic keypress to the game window."""
    try:
        out = subprocess.run(
            [xdotool, "search", "--name", f"{title} Recompiled"],
            capture_output=True, text=True, env=env, timeout=10)
        wins = out.stdout.split()
        if not wins:
            out = subprocess.run(
                [xdotool, "search", "--name", "Recompiled"],
                capture_output=True, text=True, env=env, timeout=10)
            wins = out.stdout.split()
        if not wins:
            return
        subprocess.run([xdotool, "windowfocus", "--sync", wins[-1]],
                       capture_output=True, env=env, timeout=10)
        subprocess.run([xdotool, "key", key],
                       capture_output=True, env=env, timeout=10)
    except Exception:
        pass


def warm_run_overlay(r, job, warm):
    """Run the packaged game once - in a scratch copy of the package
    with the toolchain already staged - so the runtime captures its
    runtime-loaded code and compiles native shards into warm/cache.

    A generic input pattern (Start/Cross on a schedule) walks most
    games from title into menus and gameplay; the attract mode covers
    the rest. Returns (shard_count, captures_path_or_None). Raises
    StageError when the run never reaches the runtime's overlay init:
    a broken boot must never masquerade as 'this game has no
    overlays'."""
    repo, name = job.repo, job.name
    exe = warm / name
    game_id = disc_id(job.probes[0])
    log_path = repo / "warm-run.log"
    env = dict(os.environ)
    env["SDL_AUDIODRIVER"] = "dummy"
    xvfb = None
    if not env.get("DISPLAY"):
        n = _free_display()
        xvfb_bin = shutil.which("Xvfb")
        if n is None or not xvfb_bin:
            raise StageError(
                "overlay warm run needs a display: $DISPLAY is unset "
                "and Xvfb is not installed (the toolbox Install button "
                "adds xvfb + xdotool)")
        xvfb = subprocess.Popen([xvfb_bin, f":{n}", "-screen", "0",
                                 "1280x720x24"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        env["DISPLAY"] = f":{n}"
        time.sleep(2)
    xdotool = shutil.which("xdotool")
    if not xdotool:
        r.log("xdotool not installed - warm run relies on the game's "
              "attract mode (no synthetic input); the toolbox Install "
              "button adds it")
    keys = ["Return", "X", "Return", "X", "Z"]   # Start Cross Start Cross Square
    r.log(f"overlay warm run: launching {exe.name} with the staged "
          "toolchain (boot + menus + attract, up to "
          f"{WARM_MAX_S}s) - full log: {log_path}")
    try:
        with open(log_path, "wb") as lf:
            proc = subprocess.Popen(
                [str(exe), "--disc", job.cues[0], "--no-launcher"],
                cwd=warm, env=env, stdout=lf, stderr=subprocess.STDOUT,
                start_new_session=True)
            start = time.time()
            last_growth = start
            last_sig = None
            first_capture = None
            key_i = 0
            next_key = start + 20
            while True:
                now = time.time()
                elapsed = now - start
                if proc.poll() is not None:
                    break
                cache_dir = warm / "cache" / game_id
                count = (len(list(cache_dir.rglob("*.so")))
                         if cache_dir.is_dir() else 0)
                cap = warm / "overlay_captures.json"
                cap_live = cap.exists() and cap.stat().st_size > 10
                sig = (count, cap.stat().st_mtime if cap.exists() else 0)
                if sig != last_sig:
                    last_sig = sig
                    last_growth = now
                if first_capture is None and (count > 0 or cap_live):
                    first_capture = now
                if xdotool and now >= next_key:
                    _warm_send_key(env, xdotool, job.title,
                                   keys[key_i % len(keys)])
                    key_i += 1
                    next_key = now + 15
                if now - last_growth >= WARM_QUIET_S:
                    if first_capture is None and elapsed >= WARM_MIN_S:
                        break
                    if (first_capture is not None
                            and now - first_capture >= WARM_FIGHT_S):
                        break
                if elapsed >= WARM_MAX_S:
                    break
                time.sleep(3)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
    finally:
        if xvfb is not None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
    log_text = (log_path.read_text(errors="replace")
                if log_path.exists() else "")
    for line in log_text.splitlines():
        if ("tier using bundled toolchain" in line
                or "overlay autocompile enabled" in line
                or "no bundled toolchain" in line):
            r.log("warm run: " + line.strip())
    if "overlay autocompile enabled" not in log_text:
        raise StageError(
            "overlay warm run never reached overlay init (no "
            "'overlay autocompile enabled' line in warm-run.log) - "
            "the game did not boot with the staged toolchain; see "
            f"{log_path}")
    cache_dir = warm / "cache" / game_id
    count = (len(list(cache_dir.rglob("*.so")))
             if cache_dir.is_dir() else 0)
    cap = warm / "overlay_captures.json"
    return count, (cap if cap.exists() else None)


def _title_cps_mode(job):
    """True when the title's generated code runs the runtime in CPS
    (continuation-passing) mode: the generated dispatch carries the
    marker `g_psx_cps_mode = 1` (its own comment: 'CPS runtime-mode
    marker (the overlay loader reads g_psx_cps_mode)'). Overlay
    shards MUST be compiled with the matching mode - the runtime
    passes --cps to its own autocompile for such titles, and
    compile_overlays warns the flag 'must match the runtime build'.
    A mismatched shard is not caught by the cache tag or the layout
    validation, so the offline compiles below take the flag from
    here, never from a guess."""
    gen = job.repo / "generated"
    if not gen.is_dir():
        return False
    needle = b"g_psx_cps_mode = 1"
    for f in gen.glob("*_dispatch.c"):
        try:
            with open(f, "rb") as fh:
                tail = b""
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    if needle in tail + chunk:
                        return True
                    tail = chunk[-len(needle):]
        except OSError:
            continue
    return False


def compile_offline_cache(r, job, toml_path, toolchain_dir, captures,
                          target):
    """Compile the warm run's captures into a FRESH cache dir with
    the framework's compile_overlays.py - the full offline pass
    (region audits, strong-root supplements, demand batching).
    This matters: the runtime's own in-session autocompile is
    lazy, compiles fewer shards per region, and its incremental
    skip then blocks the fuller decomposition for any region it
    already touched (measured on Tekken 1: 20 shards live+top-up
    vs 182 from a fresh offline pass over the same captures).
    Returns the scratch cache root, or None on failure - callers
    fall back to the live-compiled cache / toolchain-only.
    --cps follows the title's generated dispatch marker (see
    _title_cps_mode); the Windows pass cross-compiles with the
    MinGW gcc, the Linux pass uses the host gcc."""
    if captures is None or not Path(captures).exists():
        return None
    repo = job.repo
    scratch = repo / f"warm-{target}-cache"
    if scratch.exists():
        shutil.rmtree(scratch)
    cmd = ["python3", repo / "psxrecomp" / "tools" / "compile_overlays.py",
           "--captures", captures,
           "--game-toml", toml_path,
           "--recompiler", repo / "build-recompiler" / "psxrecomp-game",
           "--runtime-include", repo / "psxrecomp" / "runtime" / "include",
           "--project-root", toolchain_dir,
           "--out-dir", scratch,
           "--compiler", "gcc"]
    build_dir = repo / ("build-windows" if target == "windows"
                        else "build-release")
    flavor = "0"
    ff = build_dir / "psxrecomp_overlay_flavor-psx-runtime.txt"
    if ff.exists() and ff.read_text().strip().isdigit():
        flavor = ff.read_text().strip()
    if target == "windows":
        cmd += ["--gcc", "x86_64-w64-mingw32-gcc",
                "--target-os", "win", "--arch-abi", "win-x64",
                "--flavor", flavor]
    else:
        cmd += ["--target-os", "linux", "--arch-abi", "linux-x64",
                "--flavor", flavor]
    if _title_cps_mode(job):
        cmd.append("--cps")
    rc = r.run(cmd, timeout=3600)
    if rc != 0:
        r.log(f"offline {target} shard compile failed (see log)")
        shutil.rmtree(scratch, ignore_errors=True)
        return None
    return scratch


def _merge_live_cache(offline_scratch, live_cache):
    """Union of the offline-compiled cache and the warm run's
    live-compiled cache (the offline files win on conflicts; the
    live pass can hold a shard for state the final captures file
    no longer lists). Returns the merged dir (the scratch)."""
    if live_cache.is_dir():
        for f in live_cache.rglob("*"):
            if f.is_file():
                dest = offline_scratch / f.relative_to(live_cache)
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
    return offline_scratch


def stage_overlay(r, job, stages):
    """Toolchain staging + shard-cache pre-warm for overlay games.

    Evidence-gated: the packaged Linux build runs once with the
    toolchain staged; only a game that actually captures overlay
    code gets the toolchain (and the validated, pre-warmed cache)
    in its packages. A game with no runtime-loaded code ships
    exactly as before. Returns {target: info} for the README."""
    info = {}
    if "linux" not in stages:
        r.log("overlay staging skipped: this job builds no Linux "
              "package to warm-run (the shard cache is produced by "
              "running the game)")
        return info
    repo, name = job.repo, job.name
    game_id = disc_id(job.probes[0])
    warm_root = repo / "warm-linux"
    warm = warm_root / name
    if warm_root.exists():
        shutil.rmtree(warm_root)
    shutil.copytree(stages["linux"], warm)
    try:
        stage_toolchain_into(r, job, warm, "linux")
        shards, captures = warm_run_overlay(r, job, warm)
        caps_n = 0
        if captures is not None:
            try:
                caps_n = len(json.loads(
                    Path(captures).read_text(errors="replace")))
            except Exception:
                caps_n = 0
        if shards <= 0 and caps_n <= 0:
            r.log("overlay warm run: no overlay code captured across "
                  "boot / menus / attract - this game does not load "
                  "code at runtime; packages ship without the "
                  "overlay toolchain (layout unchanged)")
            return info
        r.log(f"overlay warm run: {shards} shard(s) compiled live, "
              f"{caps_n} capture entr(ies) recorded for {game_id}")
        # Linux package: toolchain + validated cache for ITS toml.
        # The shipped cache comes from the full offline compile of
        # the captures (fresh dir - see compile_offline_cache),
        # merged with whatever the live pass compiled.
        stage_toolchain_into(r, job, stages["linux"], "linux")
        tag = _cg_tag(r, job, stages["linux"] / "game.toml",
                      repo / "build-release")
        src = warm / "cache"
        scratch = None
        if captures is not None:
            scratch = compile_offline_cache(
                r, job, warm / "game.toml", warm / "overlay_toolchain",
                captures, "linux")
            if scratch is not None:
                src = _merge_live_cache(scratch, warm / "cache")
        try:
            n = _stage_cache(r, job, game_id, src,
                             stages["linux"], tag)
            info["linux"] = {"shards": n, "tag": tag, "cache": True}
            r.log(f"Linux package: overlay toolchain + {n} "
                  f"pre-compiled shard(s) [{tag}]")
        except StageError as ex:
            info["linux"] = {"shards": 0, "tag": tag, "cache": False}
            r.log("Linux package: overlay toolchain staged WITHOUT "
                  f"a validated cache ({ex}) - the bundled "
                  "toolchain compiles shards on the player's "
                  "machine at first play")
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        if captures is not None:
            keep = repo / "overlay_captures.json"
            shutil.copy2(captures, keep)
            r.log(f"overlay captures kept in the project: {keep}")
        if "windows" in stages:
            stage_emitters_mingw(r, job)
            stage_toolchain_into(r, job, stages["windows"], "windows")
            wtag = _cg_tag(r, job, stages["windows"] / "game.toml",
                           repo / "build-windows")
            wn = None
            if captures is not None:
                wscratch = compile_offline_cache(
                    r, job, stages["windows"] / "game.toml",
                    stages["windows"] / "overlay_toolchain",
                    captures, "windows")
                if wscratch is not None:
                    try:
                        wn = _stage_cache(
                            r, job, game_id, wscratch,
                            stages["windows"], wtag,
                            arch_abi="win-x64", shared_ext=".dll")
                    except StageError as ex:
                        r.log("offline Windows cache did not "
                              f"validate ({ex})")
                    shutil.rmtree(wscratch, ignore_errors=True)
            info["windows"] = {"shards": wn or 0, "tag": wtag,
                               "cache": bool(wn)}
            if wn:
                r.log(f"Windows package: overlay toolchain + {wn} "
                      f"pre-compiled shard(s) [{wtag}]")
            else:
                r.log("Windows package: overlay toolchain staged "
                      "WITHOUT a pre-compiled cache - the bundled "
                      "toolchain compiles shards on the player's "
                      "machine at first play")
        return info
    finally:
        shutil.rmtree(warm_root, ignore_errors=True)


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


def package_readme(job, offer_vulkan=False, overlay=None):
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
    ov = ""
    if overlay:
        cache = (f" It also carries a pre-compiled native shard cache "
                 f"({overlay['shards']} shard(s)) built from a real play "
                 f"session, so the covered code runs native from the "
                 f"first launch."
                 if overlay.get("cache") else
                 " No pre-compiled cache is bundled for this platform; "
                 "the toolchain compiles shards live during your first "
                 "play sessions instead (a small one-time cost per new "
                 "area).")
        ov = ("\n## Runtime-loaded code (native overlay shards)\n\n"
              "This game loads part of its code from the disc while it "
              "runs. The package includes `overlay_toolchain/` - the "
              "self-contained compiler bundle the runtime uses to turn "
              "that code into native PC code instead of interpreting "
              "it (interpreting it was the stutter in earlier builds)."
              + cache
              + " Keep the `overlay_toolchain/` and `cache/` folders "
                "next to the program; without them the game still "
                "runs, but runtime-loaded code falls back to the slow "
                "interpreter.\n")
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
{swap}{ra}{vk}{ov}{ss}
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
    # Assemble every target's folder first: overlay staging (below)
    # runs the Linux package and its verdict decides what BOTH
    # packages carry, and the README - written after - reports it.
    stages = {}
    for osname, bdir, exefile in targets:
        stage = repo / f"pkg-{osname}" / name
        if stage.parent.exists():
            shutil.rmtree(stage.parent)
        stage.mkdir(parents=True)
        stages[osname] = stage
        shutil.copy2(bdir / exefile, stage / exefile)
        offer = bool(job.vulkan_backend.get(osname))
        (stage / "game.toml").write_text(package_game_toml(job, offer))
        shutil.copy2(bdir / "game_options.toml", stage / "game_options.toml")
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
        # Guard fold-in: stage the boot EXE the game.toml exe= line
        # names, plus the framework licenses/ folder. Without
        # disc/<boot exe> the runtime text-image guard has no local
        # reference image to validate against; without
        # licenses/psxrecomp-LICENSE, find_project_root() cannot pin
        # the package as its own root and walks to a marker ancestor,
        # resolving every relative path (the EXE included) against
        # the wrong folder. Either absence leaves the guard unarmed
        # on a real install, so a package that cannot arm fails here
        # instead of shipping.
        boot_exe = job.probes[0]["boot_exe"]
        disc_src = repo / "disc" / boot_exe
        if not disc_src.is_file():
            raise StageError(
                f"boot EXE missing from project tree: {disc_src} "
                f"(game.toml names disc/{boot_exe})")
        disc_dst = stage / "disc" / boot_exe
        disc_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(disc_src, disc_dst)
        fw = framework_cache()
        lic = stage / "licenses"
        lic.mkdir(exist_ok=True)
        notices = fw / "psxrecomp" / "runtime" / "licenses"
        if notices.is_dir():
            for f in sorted(notices.iterdir()):
                if f.is_file():
                    shutil.copy2(f, lic / f.name)
        for src_file, dst_name in (
                (fw / "psxrecomp" / "THIRD_PARTY_ATTRIBUTION.md",
                 "THIRD_PARTY_ATTRIBUTION.md"),
                (fw / "psxrecomp" / "LICENSE", "psxrecomp-LICENSE"),
                (fw / "psxrecomp" / "PERMISSIONS.md",
                 "psxrecomp-PERMISSIONS.md"),
                (fw / "recomp-ui" / "LICENSE", "recomp-ui-LICENSE")):
            if src_file.is_file():
                shutil.copy2(src_file, lic / dst_name)
        if not disc_dst.is_file() \
                or not (lic / "psxrecomp-LICENSE").is_file():
            raise StageError(
                f"guard staging incomplete for {name} ({osname}): "
                f"disc/{boot_exe} or licenses/psxrecomp-LICENSE "
                f"missing from the staged package")
        r.log(f"staged guard files: disc/{boot_exe} + licenses/")
        if s.ra_enabled:
            shutil.copy2(repo / "retroachievements.toml.example", stage)
            if s.ra_toml_path and Path(s.ra_toml_path).exists():
                shutil.copy2(s.ra_toml_path, stage / "retroachievements.toml")
        if not s.save_states:
            (stage / "config.ini").write_text(
                "[KeyMap]\nSaveStateMenu =\nRewind =\n")
    # Overlay staging (v1.0.18): warm-run the Linux package with the
    # framework toolchain staged; overlay games get the toolchain +
    # a pre-warmed native shard cache in their packages, other games
    # ship exactly as before.
    job.overlay_info = stage_overlay(r, job, stages)
    for osname, bdir, exefile in targets:
        stage = stages[osname]
        offer = bool(job.vulkan_backend.get(osname))
        (stage / "README.md").write_text(
            package_readme(job, offer, job.overlay_info.get(osname)))
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


def package_paths(job):
    """The exact zip paths stage_package writes for this job's
    requested targets: <root>/<Name>-<Target>.zip. Exact paths on
    purpose - never a glob or prefix match: 'SouthPark' is a prefix
    of 'SouthParkRally', and a pattern check could credit one
    game's package to another."""
    out = []
    if job.settings.build_linux:
        out.append(job.root / f"{job.name}-Linux.zip")
    if job.settings.build_windows:
        out.append(job.root / f"{job.name}-Windows.zip")
    return out


def missing_packages(job):
    """Requested-target packages that do not exist or are empty.
    A conversion that produced no package is a FAILED conversion,
    never a silent 'done' - the same rule the converter family's
    release checks apply to wrapped tools that exit 0 with no
    output. Requesting no targets at all counts as missing."""
    paths = package_paths(job)
    if not paths:
        return [job.root / "(no build targets selected)"]
    return [p for p in paths
            if not (p.is_file() and p.stat().st_size > 0)]


def job_packages_complete(job):
    """Ground truth for 'is this queue item already converted?':
    every requested target's package exists and is non-empty.
    Multi-disc sets that boot different programs convert as one
    build per disc (the split route: <Name>Disc<i> projects, each
    with its own packages) instead of one package set - accept
    that complete per-disc set too. Anything ambiguous returns
    False: re-running wastes time, but a wrong 'complete' would
    hide a game that was never converted."""
    if not missing_packages(job):
        return True
    if len(job.cues) > 1:
        for i, cue in enumerate(job.cues):
            sub = Job(cues=[cue],
                      name=f"{job.name}Disc{i + 1}",
                      title=job.title,
                      work_dir=job.work_dir, settings=job.settings)
            if missing_packages(sub):
                return False
        return True
    return False


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
    patch_probe_subdir_boot(template, log, fail_loud=True)
    patch_probe_preflight(template, log, fail_loud=True)
    patch_overlay_draw_chain(template, log, fail_loud=True)
    patch_cdrom_getlocp(template, log, fail_loud=True)
    patch_gpu_depth24_span(template, log, fail_loud=True)
    patch_gpu_depth24_hold(template, log, fail_loud=True)
    patch_sw_guest_grid(template, log, fail_loud=True)
    patch_gl_guest_grid(template, log, fail_loud=True)
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
                              patch_probe_subdir_boot(job.repo, log,
                                                  fail_loud=True),
                              patch_probe_preflight(job.repo, log,
                                                    fail_loud=True),
                              patch_overlay_draw_chain(job.repo, log,
                                                  fail_loud=True),
                              patch_cdrom_getlocp(job.repo, log,
                                                  fail_loud=True),
                              patch_gpu_depth24_span(job.repo, log,
                                                  fail_loud=True),
                              patch_gpu_depth24_hold(job.repo, log,
                                                  fail_loud=True),
                              patch_sw_guest_grid(job.repo, log,
                                                  fail_loud=True),
                              patch_gl_guest_grid(job.repo, log,
                                                  fail_loud=True))),
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
    # Output gate: returning 'done' requires the packages to exist.
    # (The disc-set split route returns earlier; its per-disc
    # sub-jobs pass through this same gate individually.)
    missing = missing_packages(job)
    if missing:
        raise StageError(
            "conversion ran to the end but produced no package: "
            + ", ".join(str(p) for p in missing))
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
