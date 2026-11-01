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
    """'South Park Rally (USA).cue' -> 'South Park Rally'."""
    stem = Path(cue).stem
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
    probes: list = field(default_factory=list)   # probe JSON dicts, per cue
    set_verdict: dict = field(default_factory=dict)

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

def probe_cue(runner, cue, template, players=1, json_out=None):
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


def fetch_cover(cue, dest_dir):
    """Fetch libretro-thumbnails Named_Boxarts art for the cue's Redump
    name. Returns the PNG path or None."""
    name = Path(cue).stem
    for ch in '&*/:<>?\\|"':
        name = name.replace(ch, "_")
    url = ("https://thumbnails.libretro.com/Sony%20-%20PlayStation/"
           f"Named_Boxarts/{urllib.parse.quote(name)}.png")
    dest = Path(dest_dir) / (Path(cue).stem + ".png")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RecompTools/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if len(data) < 1000:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest
    except Exception:
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
        same = job.set_verdict.get("same_program") or \
            job.set_verdict.get("verdict") == "same_program" or \
            job.set_verdict.get("buildable") is True
        if not same:
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
    # For a verified set, the build disc is the LAST cue (house pattern).
    probe = job.probes[-1]
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
    cue = job.cues[-1]           # build disc = last of a verified set
    r.run_ok(["python3", repo / "psxrecomp" / "psxrecomp_cli.py", "generate",
              "--config", "game.toml", "--project-root", ".",
              "--disc", cue, "--no-toolchain-download"],
             cwd=repo, timeout=3600)
    gen = repo / "generated"
    n = len(list(gen.glob("*.c"))) if gen.is_dir() else 0
    if n == 0:
        raise StageError("Generate produced no C files")
    r.log(f"generated/ holds {n} C files")


def stage_build(r, job, target):
    repo = job.repo
    s = job.settings
    if target == "linux":
        bdir, extra = repo / "build-release", []
    else:
        bdir = repo / "build-windows"
        extra = ["-DCMAKE_TOOLCHAIN_FILE=psxrecomp/cmake/toolchain-mingw-w64.cmake"]
    r.run_ok(["cmake", "-S", repo, "-B", bdir, "-G", "Ninja",
              "-DCMAKE_BUILD_TYPE=Release",
              f"-DPSX_RECOMP_UI={'ON' if s.launcher_ui else 'OFF'}",
              *extra], timeout=900)
    r.run_ok(["cmake", "--build", bdir, "--target", "psx-runtime",
              "-j", s.jobs], timeout=7200)
    exe = bdir / (job.name if target == "linux" else job.name + ".exe")
    if not exe.exists() or exe.stat().st_size == 0:
        raise StageError(f"{target} build finished but {exe} is missing/empty")
    r.log(f"{target} binary: {exe} ({exe.stat().st_size} bytes)")


def stage_verify(r, job):
    repo = job.repo
    probe = job.probes[-1]
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

def package_game_toml(job):
    probe = job.probes[-1]
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
        f'id = "{probe["serial"]}"',
        f"players = {players}",
        f'exe = "disc/{probe["boot_exe"]}"',
        f'load_address = "{probe["load_address"]}"',
        f'entry_pc = "{probe["entry_pc"]}"',
        f'text_size = "{probe["text_size"]}"',
        f'stack_base = "{probe["stack_base"]}"',
    ]
    if len(job.cues) > 1:
        discs = ",\n    ".join(f'"{Path(c).name}"' for c in job.cues)
        serials = ",\n    ".join(f'"{p["serial"]}"' for p in job.probes)
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
        f'aspect_ratio = "{s.aspect_ratio}"',
        "",
        "[controller]",
        f'default_mode = "{s.controller_mode}"',
        "",
    ]
    return "\n".join(lines)


def package_readme(job):
    probe = job.probes[-1]
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
    ss = ""
    if not s.save_states:
        ss = ("\n## Save states\n\nSave-state hotkeys (F7/F8) are unbound in "
              "the included `config.ini` (Recomp Tools setting). Delete that "
              "file or rebind the keys in the launcher to use save states.\n")
    return f"""# {job.title} - Recompiled for PC

{job.title} (PS1, {probe['serial']}), recompiled to native PC.
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
{ra}{ss}
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
        (stage / "game.toml").write_text(package_game_toml(job))
        shutil.copy2(bdir / "game_options.toml", stage / "game_options.toml")
        (stage / "README.md").write_text(package_readme(job))
        shutil.copytree(bdir / "bios", stage / "bios")
        shutil.copytree(bdir / "assets", stage / "assets")
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
    plan = []
    if job.settings.build_linux:
        plan.append("linux")
    if job.settings.build_windows:
        plan.append("windows")
    steps = [
        ("scaffold", lambda: stage_scaffold(r, job, template)),
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
               slug_override=d.get("slug", ""))


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
