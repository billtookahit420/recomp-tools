#!/usr/bin/env python3
"""Recomp Tools - a friendly front-end for the PSXRecomp pipeline.

Pick discs, see what they are (probe panel + cover art), tick the
house-rule options, and convert - one game at a time, start to finish,
with a live log. Built for Duane so conversions don't need the CLI.

Run:  .venv/bin/python app/main.py
"""
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

import engine

APP_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_ROOT / "settings.json"
ART_CACHE = APP_ROOT / "art-cache"

HELP_TEXT = """\
RECOMP TOOLS - HELP

WHAT THIS DOES
Recomp Tools turns a PlayStation 1 disc image (.cue + .bin files) into a
native PC version of the game (a "recomp"), for Linux and/or Windows,
with the launcher UI and RetroAchievements wired in. It runs the same
pipeline used for all of Duane's delivered recomps:

  1. Scaffold  - creates the project folder from the framework template.
  2. Probe     - reads the disc: serial, region, boot EXE, entry point,
                 track count, and a first-pass function seed list.
  3. Patch     - points the build at the game's generated code.
  4. RA wiring - (if enabled) copies in the RetroAchievements module and
                 adds the one framework accessor it needs.
  5. Emitters  - builds the recompiler tools.
  6. Generate  - translates the game's PS1 code to C. This is the slow,
                 memory-hungry stage: big games can use several GB.
  7. Build     - compiles the Linux and/or Windows executables.
  8. Verify    - checks the binaries really contain the game's code
                 (function count + entry-point symbol).
  9. Package   - stages a clean folder and zips it: game binary, clean
                 config, OpenBIOS, launcher assets, mods, README, and
                 your RetroAchievements file if you picked one. For
                 games that load code at runtime it also stages the
                 overlay compiler bundle and a pre-warmed native shard
                 cache (see the stutter section below).

THE QUEUE
Add discs with "Add Disc". Games convert ONE AT A TIME, start to
finish - that is deliberate (parallel Generates run out of RAM) and it
keeps failures contained: a failed game is marked red, logged, and the
queue moves on.

MULTI-DISC GAMES
Just add the discs - one at a time with "Add Disc", or together with
"Add Disc Set". Discs from the same folder with the same game title
are grouped into ONE queue item automatically. Or pick the folder
that holds your games with "Add Library Folder": it scans up to 4
folders deep (hidden folders are skipped) and adds every .cue it
finds the same way, so a whole library lands in the queue in one go
and multi-disc sets still group into one item each. When the set runs, it
is probed and checked: if all discs boot the SAME program (Metal Gear
Solid, Evil Dead, Driver 2), you get ONE package and the launcher
offers disc selection. If the discs boot DIFFERENT programs
(Resident Evil 2), each disc is converted as its own game
automatically. Only a set that cannot be classified either way is
parked as "needs review", never guessed.

GAMES THAT HUNG AT LEVEL LOAD (FIXED IN v1.0.16)
Some games with CD music tracks (Tomb Raider 1 + 2 are the known
cases) reached their title screen fine, then hung on a black screen
with no audio the moment a level tried to load. The framework's CD
position answer (GetlocP) always claimed "track 1", so a game
checking that its seek landed in the level's music track waited
forever for a confirmation that could never come. Builds made with
v1.0.16 or later carry the corrected answer (real track, real
pregap position, as a real drive reports). Re-run an affected game
here to fix its package.

MOVIES CUT OFF AT THE RIGHT (FIXED IN v1.0.17)
Some games' movies (FMVs) played with the right side cut off - the
picture stopped about two-thirds across with black beyond it, while
gameplay rendered full width (Resident Evil: Director's Cut and
Mega Man 8 are the known cases). The framework's 24-bit movie path
tracked how much of the screen width the game's movie uploads had
covered, but only counted uploads at least 256 units wide - and
these games upload each movie frame as many thin vertical strips,
so the coverage never registered and the presenter blacked out
everything past a stale, narrower width. Builds made with v1.0.17
or later carry the corrected tracking (every upload counts; only a
genuinely uncovered tail is blanked). Re-run an affected game here
to fix its package.

WALLS AND FLOORS WITH DENSE VERTICAL STRIPES (FIXED IN v1.0.19,
SOFTWARE + OPENGL RENDERERS)
Doom-engine games (Doom, Final Doom) drew their worlds from
one-pixel-thin textured strips whose edges the hi-res renderer
covered inexactly, so thin dark slivers showed through in a
regular stripe pattern at internal resolutions above 1x, and
hairline seams could appear along wall/floor joins even at 1x
under OpenGL. Builds made with v1.0.19 or later fix BOTH the
SOFTWARE and OPENGL renderers' coverage: each decides coverage
on the game's own pixel grid (the same rule the native
renderer uses), so strips tile exactly at any internal
resolution. Verified on Final Doom: stripe modulation
19.8% -> 0.7% at 4x (Software), 14.2% -> 0.33% at 4x (OpenGL).
The Vulkan renderer does not have this fix yet - for
Doom-engine games, use the Software or OpenGL renderer.

GAMES WHOSE TITLES START WITH A DIGIT "FAILED" AT THE FINISH
LINE (FIXED IN v1.0.20)
Games whose titles start with a digit (007 Racing, 007 The
World Is Not Enough, 007 Tomorrow Never Dies) converted
completely - full compile, working game - then reported
failure at the last step: the framework named the finished
program with a leading underscore (_007Racing) while the tool
checked for the plain name. Builds made with v1.0.20 or later
pin the program name explicitly, so digit-leading titles
finish, verify and package under their real names.
Letter-leading titles are unchanged. Re-run an affected game
here to convert it cleanly.

TITLE SCREEN CUT OFF AT THE RIGHT (v1.0.20 FIX WAS NOT ENOUGH)
A few games show one static 24-bit screen - Gran Turismo's
640x480 title screen is the known case - with the right side
cut off (about 80 px black), while menus and gameplay render
full width. v1.0.20 shipped a coverage fix for this, but on
hardware the crop is UNCHANGED: the packaged binary carried
the fix, yet the picture still loses the same right strip, and
the run never enters the code the fix patched. A diagnostic
build is out to find which path actually blanks those
columns; until that lands and a fix is verified on hardware,
treat the crop as still open - do not re-run Gran Turismo
expecting this build to fix it.

PROBE RESULTS ON THE WRONG GAME, OR NEVER ARRIVING (FIXED IN
v1.0.21)
Added games are probed in the background, one after another.
Two defects in that chain: results were matched to games by
queue row, so removing a row while probes were still running
shifted later results onto the wrong games - a row could end
up wearing another game's disc details; and about one run in
three, the last game in a batch was never probed at all,
because a game left the waiting list the moment its probe
started, so one missed step ended the chain early. Results
now travel with the game they belong to, results for games
removed mid-flight are dropped, and a game stays on the
waiting list until its own result arrives. This covers "Add
Disc" and "Add Disc Set" too - every add shares the same
chain.

GAMES THAT STUTTER ON RUNTIME-LOADED CODE (IMPROVED IN v1.0.18)
Some games load part of their code from the disc while they run
(Gran Turismo and Tekken 1 are the known cases). That code cannot
be compiled ahead of time, so earlier builds ran it in a slow
built-in interpreter - a big share of those games' stutter. Builds
made with v1.0.18 or later fix the packaging: the converter runs
the finished game once with the framework's compiler bundle
staged beside it, lets the game compile its runtime-loaded code
into native shards, and ships both the bundle (overlay_toolchain/)
and the pre-compiled shard cache in the package. The covered code
then runs natively from the first launch, and the bundle keeps
compiling any code the warm-up run did not reach. Games that load
no code at runtime convert exactly as before. One honest limit:
Gran Turismo's hottest loop has a second, separate problem - it
stays partly interpreted even with the bundle staged - so this
removes the packaging half of its stutter, not all of it.

CHANGING DISCS MID-GAME
Packages built from a disc set support swapping while the game runs
(the package README says the same): when the game asks for another
disc, open the in-game menu with F10 (or Ctrl+L) on the keyboard, or
Select/Back + L3 together on the controller, go to the Disc section,
set "Selected disc", then "Insert disc and resume". This works in
builds made with this version or later - older packages had a bug
where the menu opened invisibly; re-run those games here to fix them.

OPTIONS
- Players override: 0 = trust the probe/setup default. The main menu
  of the game is the ground truth; when in doubt leave 0.
- Launcher UI: the setup/launcher screen shown before the game starts.
  Keep it ON (house rule).
- RetroAchievements: on/off. Pop-ups can be hidden at build time -
  NOTE: the "Logged in" toast uses the same draw path, so hiding
  pop-ups hides the login toast too. Unlocks still work either way.
- RA toml file: pick your filled retroachievements.toml to include it
  in packages. It contains a personal token - don't share the zips.
  RetroAchievements counts unlocks from recomp clients as Casual
  (softcore); that's their policy for unknown clients, not a bug here.
- Renderer default / aspect / controller mode: written into the
  packaged config; changeable later in the launcher.
- Renderer = Vulkan: builds the real Vulkan backend, which needs
  the Vulkan SDK on THIS machine at build time (headers + glslc -
  the toolbox Install button sets that up). Without it a Vulkan
  build stops with that message rather than shipping the
  framework's inert stub. ANY package built with the SDK present
  also OFFERS Vulkan in the game's own launcher, whatever default
  is chosen here - players can switch per game; the default stays
  what was picked here. Achievement pop-ups draw only under
  OpenGL — with Vulkan active they're off; players can switch
  the game to OpenGL in its launcher and pop-ups return.
- Cover art: the art fetched for the queue is bundled into the
  package under artwork/<serial>/, which is where the game's own
  launcher looks for it — so covers show in both places. Fetching
  tries two hosts for the libretro-thumbnails library and retries
  each name, so one host having a bad night no longer means a
  missing cover. The standalone Recomp Covers utility fills covers
  into packages that were built before this.
- Build targets: Linux, Windows, or both.
- Save states: OFF ships a config.ini that unbinds the save-state
  hotkeys (F7/F8). ON ships no config.ini (defaults apply).
- BIOS file: only used for manual test runs. Packages always ship the
  built-in OpenBIOS; a retail BIOS is never packaged.
- Template project: a finished recomp whose framework copy is used
  when the online framework clone fails (it hangs on some networks).

FIRST RUN / TOOLBOX
The header shows the toolbox check. Needed: Python 3, CMake, Ninja, a
C/C++ compiler, MinGW-w64 for Windows builds, nm, zip. On Linux the
"Install missing packages" button in the header installs them with
one password prompt - the Vulkan SDK pieces (libvulkan-dev + glslc)
are included, so Vulkan builds and the in-launcher Vulkan offer
work out of the box. The same list by hand:
  pkexec apt install -y build-essential cmake ninja-build zip binutils \
      gcc-mingw-w64-x86-64 g++-mingw-w64-x86-64 libgl-dev libssl-dev \
      git python3-venv libfreetype-dev libharfbuzz-dev libxtst-dev \
      libasound2-dev libpulse-dev libudev-dev libxext-dev libxrandr-dev \
      libxcursor-dev libxi-dev libxss-dev libxkbcommon-dev libdrm-dev \
      libgbm-dev libegl-dev libdbus-1-dev libvulkan-dev glslang-tools \
      glslc
Re-check after installing. The RetroAchievements module is bundled
with the app. The PSXRecomp framework is downloaded automatically
on first use into the app's framework-cache folder (needs git and
a connection, one time only); if you already keep finished recomp
projects around, one of those can serve as the template instead.

TROUBLESHOOTING
- Generate gets killed / runs out of memory: close other apps, lower
  Jobs to 1, and convert one game at a time. Very large games may need
  a machine with more RAM.
- "GEN_MARKER patch did not land": the template's CMakeLists layout
  changed; convert one game with the CLI pipeline and use that project
  as the template.
- Probe fails: the .cue must sit in the same folder as ALL its .bin
  track files (multi-track games need every track).
- The game boots to a black screen past the title: that is a game
  bug, not a packaging bug - note it and report it with the game name.

CREDITS
Recomp Tools is only a front-end. The heavy lifting is PSXRecomp, the
static-recompilation framework created by Matthew Stanley (mstan),
now part of the RetroPortingToolkit (retroportingtoolkit.com;
github.com/mstan, github.com/RetroPortingToolKit). The launcher in
every converted game is recomp-ui (same team, MIT). RetroAchievements
support uses rcheevos (RetroAchievements, MIT). The bundled BIOS is
OpenBIOS. Cover art comes from libretro-thumbnails.
"""


class Worker(QThread):
    log_line = Signal(str)
    item_status = Signal(int, str)
    finished_all = Signal()

    def __init__(self, items, settings, work_dir):
        super().__init__()
        self.items = items            # list of dicts: cues, name, title
        self.settings = settings
        self.work_dir = work_dir

    def run(self):
        # finished_all fires from a finally: whatever happens to an
        # item - including an error building its Job, which used to
        # sit OUTSIDE the try and kill this thread silently, leaving
        # the queue frozen mid-list - the GUI is always told the run
        # ended, and every item reaches a terminal status.
        try:
            for idx, item in enumerate(self.items):
                self.item_status.emit(idx, "running")
                try:
                    job = engine.Job(cues=item["cues"], name=item["name"],
                                     title=item["title"].split(" (")[0],
                                     work_dir=self.work_dir,
                                     settings=self.settings,
                                     slug_override=item.get("slug", ""),
                                     art=item.get("art", ""))
                    engine.run_job(job, self.log_line.emit)
                except engine.StageError as ex:
                    msg = str(ex)
                    self.log_line.emit(f"FAILED: {msg}")
                    self.item_status.emit(
                        idx, "needs review" if "needs review" in msg
                        else "failed")
                    continue
                except Exception:
                    self.log_line.emit(traceback.format_exc())
                    self.item_status.emit(idx, "failed")
                    continue
                # Ground-truth gate: run_job returning is not enough.
                # 'done' requires the item's packages to exist on
                # disk, complete and non-empty - a conversion that
                # produced no package is a failed conversion, never
                # a done one.
                if engine.job_packages_complete(job):
                    self.item_status.emit(idx, "done")
                else:
                    self.log_line.emit(
                        "FAILED: conversion ended but its package is "
                        "missing or empty (checked: "
                        + ", ".join(str(p) for p
                                    in engine.package_paths(job)) + ")")
                    self.item_status.emit(idx, "failed")
        finally:
            self.finished_all.emit()


class InstallWorker(QThread):
    """Runs the one-prompt system-package install (Linux, pkexec)."""
    log_line = Signal(str)
    done = Signal(bool)

    def run(self):
        self.log_line.emit(
            "Installing system packages (one password prompt): "
            + " ".join(engine.LINUX_SYSTEM_PACKAGES))
        try:
            p = subprocess.run(engine.system_install_command(),
                               capture_output=True, text=True, timeout=1800)
            for line in (p.stdout + p.stderr).splitlines()[-12:]:
                self.log_line.emit(line)
            ok = p.returncode == 0
        except Exception as ex:
            self.log_line.emit(f"install failed: {ex}")
            ok = False
        self.done.emit(ok)


class ProbeWorker(QThread):
    log_line = Signal(str)
    probed = Signal(object, dict)

    def __init__(self, item, cues, template, players):
        super().__init__()
        # Carry the queue ITEM, not its row number: rows shift when
        # the user removes (or a folder-add appends) items while a
        # probe is in flight, and a result keyed by row number would
        # land on whatever item now sits in that row. The run
        # worker already routes by identity; probes do the same.
        self.item, self.cues = item, cues
        self.template, self.players = template, players

    def run(self):
        r = engine.Runner(self.log_line.emit)
        try:
            if self.template is None:
                self.log_line.emit("no template project - downloading "
                                   "the framework (one-time)...")
                self.template = engine.ensure_framework_cache(
                    self.log_line.emit)
            if self.template is None:
                self.log_line.emit("FAILED: framework download failed "
                                   "(needs git + a connection)")
                self.probed.emit(self.item, {})
                return
            probes = []
            paths = []
            tmp = Path(self.cues[0]).parent
            for i, cue in enumerate(self.cues):
                jp = tmp / f".recomp-tools-probe-{i}.json"
                data = engine.probe_cue(r, cue, self.template,
                                        self.players, json_out=jp)
                probes.append(data)
                paths.append(jp)
            verdict = {}
            if len(self.cues) > 1:
                verdict = engine.check_disc_set(r, paths, self.template)
            art = engine.fetch_cover(
                self.cues[0], ART_CACHE,
                serial=(probes[0].get("serial", "") if probes else ""))
            self.probed.emit(self.item, {"probes": probes, "verdict": verdict,
                                        "art": str(art) if art else ""})
        except Exception:
            self.log_line.emit(traceback.format_exc())
            self.probed.emit(self.item, {})


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Recomp Tools")
        self.resize(1180, 800)
        self.items = []               # queue model: dicts
        self.worker = None
        self.probe_worker = None
        self._probe_pending = []      # rows waiting for an auto-probe
        self._run_active = False      # a conversion Worker is running
        self._status_override = False  # disk reconcile may set 'done'
        self._log_buf = []            # log lines awaiting a batched paint
        self._build_ui()
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(120)
        self._log_timer.timeout.connect(self._flush_log)
        self._load_settings()
        self.refresh_toolbox()

    # ------------------------------------------------------------ UI

    def _build_ui(self):
        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._convert_tab(), "Convert")
        help_edit = QPlainTextEdit(HELP_TEXT)
        help_edit.setReadOnly(True)
        tabs.addTab(help_edit, "Help")

    def _convert_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        tb = QHBoxLayout()
        self.toolbox_label = QLabel("Toolbox: checking...")
        tb.addWidget(self.toolbox_label, 1)
        b = QPushButton("Re-check toolbox")
        b.clicked.connect(self.refresh_toolbox)
        tb.addWidget(b)
        self.install_btn = QPushButton("Install missing packages")
        self.install_btn.clicked.connect(self.install_packages)
        self.install_btn.setEnabled(False)
        tb.addWidget(self.install_btn)
        v.addLayout(tb)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        btns = QHBoxLayout()
        for label, fn in (("Add Disc", self.add_disc),
                          ("Add Disc Set", self.add_set),
                          ("Add Library Folder", self.add_library_folder),
                          ("Probe Selected", self.probe_selected),
                          ("Remove", self.remove_selected)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b)
        lv.addLayout(btns)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Game", "Serial", "Players", "Tracks", "Art", "Status"])
        self.table.itemSelectionChanged.connect(self.show_selected)
        lv.addWidget(self.table)
        self.run_btn = QPushButton("Run Queue")
        self.run_btn.clicked.connect(self.run_queue)
        lv.addWidget(self.run_btn)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(self._settings_box())
        rv.addWidget(self._probe_box())
        split.addWidget(right)
        split.setSizes([640, 480])
        v.addWidget(split, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(8000)
        v.addWidget(self.log, 1)
        return w

    def _settings_box(self):
        box = QGroupBox("Conversion settings (apply to the whole queue)")
        f = QFormLayout(box)
        self.players_spin = QSpinBox()
        self.players_spin.setRange(0, 4)
        self.players_spin.setSpecialValueText("auto")
        f.addRow("Players override", self.players_spin)
        self.launcher_chk = QCheckBox("Launcher UI")
        self.launcher_chk.setChecked(True)
        f.addRow(self.launcher_chk)
        self.ra_chk = QCheckBox("RetroAchievements")
        self.ra_chk.setChecked(True)
        f.addRow(self.ra_chk)
        self.popups_chk = QCheckBox("Show achievement pop-ups")
        self.popups_chk.setChecked(True)
        f.addRow(self.popups_chk)
        ra_row = QHBoxLayout()
        self.ra_toml_edit = QLineEdit()
        ra_row.addWidget(self.ra_toml_edit, 1)
        b = QPushButton("RA toml...")
        b.clicked.connect(lambda: self._pick_file(self.ra_toml_edit,
                                                  "TOML (*.toml)"))
        ra_row.addWidget(b)
        f.addRow("RA toml file", ra_row)
        self.renderer_combo = QComboBox()
        self.renderer_combo.addItems(["opengl", "vulkan", "software"])
        f.addRow("Renderer default", self.renderer_combo)
        self.renderer_hint = QLabel("")
        self.renderer_hint.setWordWrap(True)
        f.addRow("", self.renderer_hint)
        self.renderer_combo.currentTextChanged.connect(
            self._renderer_hint_update)
        self._renderer_hint_update(self.renderer_combo.currentText())
        self.aspect_combo = QComboBox()
        self.aspect_combo.addItems(["4:3", "16:9", "stretch"])
        f.addRow("Aspect ratio", self.aspect_combo)
        self.pad_combo = QComboBox()
        self.pad_combo.addItems(["digital", "analog"])
        f.addRow("Controller mode", self.pad_combo)
        tgt = QHBoxLayout()
        self.linux_chk = QCheckBox("Linux")
        self.linux_chk.setChecked(True)
        self.win_chk = QCheckBox("Windows")
        self.win_chk.setChecked(True)
        tgt.addWidget(self.linux_chk)
        tgt.addWidget(self.win_chk)
        f.addRow("Build targets", tgt)
        self.jobs_spin = QSpinBox()
        self.jobs_spin.setRange(1, 8)
        self.jobs_spin.setValue(2)
        f.addRow("Build jobs", self.jobs_spin)
        self.ss_chk = QCheckBox("Save states enabled")
        self.ss_chk.setChecked(False)
        f.addRow(self.ss_chk)
        bios_row = QHBoxLayout()
        self.bios_edit = QLineEdit()
        bios_row.addWidget(self.bios_edit, 1)
        b = QPushButton("BIOS...")
        b.clicked.connect(lambda: self._pick_file(self.bios_edit, "BIOS (*.bin)"))
        bios_row.addWidget(b)
        f.addRow("BIOS (test only)", bios_row)
        tpl_row = QHBoxLayout()
        self.tpl_edit = QLineEdit()
        tpl_row.addWidget(self.tpl_edit, 1)
        b = QPushButton("Template...")
        b.clicked.connect(lambda: self._pick_dir(self.tpl_edit))
        tpl_row.addWidget(b)
        f.addRow("Template project", tpl_row)
        out_row = QHBoxLayout()
        self.out_edit = QLineEdit(str(Path.home() / "workspace"))
        out_row.addWidget(self.out_edit, 1)
        b = QPushButton("Output...")
        b.clicked.connect(lambda: self._pick_dir(self.out_edit))
        out_row.addWidget(b)
        f.addRow("Output folder", out_row)
        return box

    def _probe_box(self):
        box = QGroupBox("Disc details")
        v = QVBoxLayout(box)
        self.art_label = QLabel("(no cover art)")
        self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.art_label)
        self.probe_label = QLabel("Select a queue item and press Probe Selected.")
        self.probe_label.setWordWrap(True)
        v.addWidget(self.probe_label)
        return box

    # ------------------------------------------------------------ helpers

    def _pick_file(self, edit, filt):
        p, _ = QFileDialog.getOpenFileName(self, "Choose file", "", filt)
        if p:
            edit.setText(p)

    def _pick_dir(self, edit):
        p = QFileDialog.getExistingDirectory(self, "Choose folder")
        if p:
            edit.setText(p)

    def log_line(self, msg):
        # Log lines arrive one queued signal per child output line -
        # a single compile streams thousands. Painting each one
        # individually lets the GUI thread fall far behind the
        # worker in long sessions, and the queue's status updates
        # wait in the same event queue behind the flood. Buffer and
        # paint in batches instead: the pane stays near-real-time
        # (120 ms) at a fraction of the layout cost. Display only -
        # conversion never reads the log widget.
        self._log_buf.append(str(msg))
        if not self._log_timer.isActive():
            self._log_timer.start()

    def _flush_log(self):
        if not self._log_buf:
            self._log_timer.stop()
            return
        buf, self._log_buf = self._log_buf, []
        self.log.appendPlainText("\n".join(buf))

    def refresh_toolbox(self):
        state = engine.toolbox_check()
        missing = [engine.TOOLS[t] for t, p in state.items() if not p]
        tmpl = engine.find_template(self.tpl_edit.text().strip()
                                    if hasattr(self, "tpl_edit") else "")
        bits = []
        bits.append("Toolbox OK" if not missing
                    else "Missing: " + ", ".join(missing))
        bits.append(f"template: {tmpl.name if tmpl else 'downloads on first use'}")
        headers, glslc = engine.vulkan_sdk_status()
        vk_ok = bool(headers and glslc)
        bits.append("Vulkan SDK OK" if vk_ok else "Vulkan SDK missing")
        self.toolbox_label.setText(" | ".join(bits))
        if hasattr(self, "install_btn"):
            self.install_btn.setEnabled(
                sys.platform.startswith("linux")
                and (bool(missing) or not vk_ok))

    def install_packages(self):
        self.install_btn.setEnabled(False)
        self._install_worker = InstallWorker()
        self._install_worker.log_line.connect(self.log_line)
        self._install_worker.done.connect(self._install_done)
        self._install_worker.start()

    def _install_done(self, ok):
        self.log_line("System packages installed." if ok
                      else "Install did not complete - see the log.")
        self.refresh_toolbox()

    def _renderer_hint_update(self, text):
        if text == "vulkan":
            self.renderer_hint.setText(
                "Vulkan needs the Vulkan SDK (headers + glslc) on this "
                "machine at build time — the toolbox Install button "
                "sets it up; without it the build stops instead of "
                "shipping a fake. Packages built with the SDK offer "
                "Vulkan in the game's launcher whatever the default. "
                "RA pop-ups only draw under OpenGL: they stay off "
                "while Vulkan is the active renderer — switch the "
                "game to OpenGL in its launcher and they return.")
        elif text == "software":
            self.renderer_hint.setText(
                "Software rendering is CPU-heavy (audio can suffer); "
                "it's the fallback, not the fast path.")
        else:
            self.renderer_hint.setText("")

    def current_settings(self):
        return engine.JobSettings(
            players=self.players_spin.value(),
            launcher_ui=self.launcher_chk.isChecked(),
            ra_enabled=self.ra_chk.isChecked(),
            ra_popups=self.popups_chk.isChecked(),
            ra_toml_path=self.ra_toml_edit.text().strip(),
            renderer=self.renderer_combo.currentText(),
            aspect_ratio=self.aspect_combo.currentText(),
            controller_mode=self.pad_combo.currentText(),
            build_linux=self.linux_chk.isChecked(),
            build_windows=self.win_chk.isChecked(),
            jobs=self.jobs_spin.value(),
            save_states=self.ss_chk.isChecked(),
            bios_path=self.bios_edit.text().strip(),
            template_dir=self.tpl_edit.text().strip(),
        )

    def _load_settings(self):
        if SETTINGS_FILE.exists():
            try:
                d = json.loads(SETTINGS_FILE.read_text())
                self.ra_toml_edit.setText(d.get("ra_toml_path", ""))
                self.tpl_edit.setText(d.get("template_dir", ""))
                self.out_edit.setText(d.get("work_dir",
                                            str(Path.home() / "workspace")))
            except Exception:
                pass

    def _save_settings(self):
        SETTINGS_FILE.write_text(json.dumps({
            "ra_toml_path": self.ra_toml_edit.text().strip(),
            "template_dir": self.tpl_edit.text().strip(),
            "work_dir": self.out_edit.text().strip(),
        }, indent=2))

    # ------------------------------------------------------------ queue

    @staticmethod
    def _set_key(cues):
        """Discs of one set share a base title and a set root. Real
        layouts scatter a set: both cues in one folder, each disc in
        its own subfolder (d1/, d2/), or each in a folder named after
        the disc itself - so climb past disc-named and title-named
        folders to the folder the whole set lives under."""
        title = engine.cue_title(cues[0])
        folder = Path(cues[0]).parent
        while folder.name and (
                re.fullmatch(r"(?i)(d|disc|disk|cd)?\s*\d{1,2}",
                             folder.name)
                or engine.cue_title(folder.name) == title):
            folder = folder.parent
        return (str(folder), title)

    def _add_item(self, cues):
        # Auto-group: however the discs are added - one at a time or
        # all at once - discs of the same set land in ONE queue item.
        key = self._set_key(cues)
        if self._run_active and any(
                self._set_key(it["cues"]) == key for it in self.items):
            # Grouping into an existing row rewrites that item's cue
            # list and status; mid-run that item may be the one the
            # worker is converting right now. Appending a NEW set is
            # fine during a run - merging into a live one is not.
            self.log_line("Add deferred: that disc set is already in "
                          "the queue and a conversion run is in "
                          "progress - add its discs again after the "
                          "run finishes.")
            return
        for row, item in enumerate(self.items):
            if item.get("state") in ("done", "running"):
                continue
            if self._set_key(item["cues"]) != key:
                continue
            merged = sorted(set(item["cues"]) | set(cues))
            if len(merged) == len(item["cues"]):
                return                # every cue already queued
            item["cues"] = merged
            base = engine.cue_title(merged[0])
            item["title"] = base + f" ({len(merged)} discs)"
            item["probes"], item["verdict"] = [], {}
            self._fill_row(row, item, "queued")
            self._queue_probe(row, force=True)
            self.log_line(f"grouped {len(merged)} discs of {base} "
                          "into one set")
            return
        base = engine.cue_title(cues[0])
        title = base
        if len(cues) > 1:
            title += f" ({len(cues)} discs)"
        item = {"cues": cues, "title": title,
                "name": engine.compact_name(base),
                "slug": re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-"),
                "probes": [], "verdict": {}, "art": ""}
        self.items.append(item)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._fill_row(row, item, "queued")
        self._reconcile_done()       # already converted? say so now
        self._queue_probe(row)        # auto-probe every added disc/set

    # ------------------------------------------------------------ status
    #
    # A row's status has ONE writer (this class, on the GUI thread)
    # and a monotonic lifecycle: queued -> (probe states) -> running
    # -> terminal (done / failed / needs review). _apply_status is
    # the gate every write passes through, and it refuses backward
    # moves - so a late probe completion can no longer paint
    # 'probed' over a finished game, and nothing can repaint a
    # running or finished row as 'queued'. The two deliberate
    # exceptions: run_queue starts a NEW lifecycle for the items it
    # is about to run (it resets their state itself, below), and
    # _reconcile_done may set 'done' from disk ground truth (the
    # _status_override flag) - disk outranks session memory.

    _TERMINAL = ("done", "failed", "needs review")
    _PRE_RUN = ("queued", "probing", "probed", "probe failed")

    def _apply_status(self, item, status):
        cur = item.get("state", "queued")
        if status == cur:
            return cur
        if cur in self._TERMINAL:
            if status == "done" and self._status_override:
                item["state"] = "done"
                return "done"
            return cur
        if cur == "running" and status not in self._TERMINAL:
            return cur
        item["state"] = status
        return status

    def _fill_row(self, row, item, status):
        status = self._apply_status(item, status)
        probe = item["probes"][0] if item["probes"] else {}
        vals = [item["title"], probe.get("serial", "?"),
                str(self.players_spin.value() or "?"),
                str(probe.get("track_count", "?")),
                "yes" if item["art"] else "-", status]
        for col, val in enumerate(vals):
            self.table.setItem(row, col, QTableWidgetItem(str(val)))

    def add_disc(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Pick a PS1 .cue", "", "CUE sheets (*.cue)")
        if p:
            self._add_item([p])

    def add_set(self):
        ps, _ = QFileDialog.getOpenFileNames(
            self, "Pick ALL .cue files of the disc set", "",
            "CUE sheets (*.cue)")
        if ps:
            self._add_item(sorted(ps))

    # How many folder levels below the picked library root the
    # "Add Library Folder" scan descends. Real layouts are shallow:
    # library/Game/cue (1), library/Game/Disc 1/cue (2), redump
    # per-disc folders (2). Four levels covers those with room to
    # spare while keeping a scan pointed at a whole drive bounded.
    LIBRARY_SCAN_MAX_DEPTH = 4

    # Folder names that hold conversion projects / framework trees,
    # never loose game discs: the project folder written per game
    # (<slug>-recomp, containing game/) and the framework trees a
    # project carries (psxrecomp/, recomp-ui/). A cue found under
    # one is a project's PRIVATE staging copy - its game/disc cue -
    # and adding it would queue the same conversion twice, once from
    # the library copy and once from inside its own project.
    _LIBRARY_SKIP_DIR_SUFFIX = "-recomp"
    _LIBRARY_SKIP_DIR_NAMES = ("psxrecomp", "recomp-ui")

    @classmethod
    def _scan_library(cls, root):
        """Recursively find .cue files under a library root.

        Returns (cues, deep_skipped): cue paths as strings in the
        same sorted order a manual multi-add would produce, deduped
        by resolved path, plus how many cues were left unadded
        because they sit deeper than LIBRARY_SCAN_MAX_DEPTH.
        Hidden folders and project/framework folders are pruned,
        and symlinked folders are not followed, so a scan cannot
        loop or ingest a conversion project that happens to live
        inside the picked folder."""
        root = Path(root)
        found, seen = [], set()
        deep = 0

        def prunable(name):
            n = name.lower()
            return (name.startswith(".")
                    or n.endswith(cls._LIBRARY_SKIP_DIR_SUFFIX)
                    or n in cls._LIBRARY_SKIP_DIR_NAMES)

        def count_deep(d):
            # Count every cue in a subtree the depth floor cut off,
            # so the "skipped for depth" number is honest about the
            # whole buried branch, not just its top folder. Same
            # pruning as the scan itself (hidden / project folders
            # and symlinks are not chased here either).
            nonlocal deep
            try:
                entries = sorted(d.iterdir(), key=lambda p: p.name)
            except OSError:
                return
            for p in entries:
                if p.is_symlink():
                    if not p.is_dir() and \
                            p.name.lower().endswith(".cue"):
                        deep += 1
                    continue
                if p.is_dir():
                    if not prunable(p.name):
                        count_deep(p)
                elif p.name.lower().endswith(".cue"):
                    deep += 1

        def visit(d, depth):
            # Own the walk instead of os.walk so a subtree cut off
            # by the depth floor can still be counted (count_deep)
            # for the summary line, not silently dropped.
            try:
                entries = sorted(d.iterdir(), key=lambda p: p.name)
            except OSError:
                return
            for p in entries:
                if p.is_symlink():
                    if p.is_dir():
                        continue            # never follow links
                    cue = p             # a linked cue file itself is
                elif p.is_dir():        # fine - resolve() dedupes it
                    if prunable(p.name):
                        continue
                    if depth + 1 <= cls.LIBRARY_SCAN_MAX_DEPTH:
                        visit(p, depth + 1)
                    else:
                        count_deep(p)
                    continue
                else:
                    cue = p
                if cue.name.lower().endswith(".cue"):
                    key = str(cue.resolve())
                    if key not in seen:
                        seen.add(key)
                        found.append(str(cue))

        visit(root, 0)
        return sorted(found), deep

    def add_library_folder(self):
        root = QFileDialog.getExistingDirectory(
            self, "Pick the folder that holds your PS1 games")
        if root:
            self.add_library(root)

    def add_library(self, root):
        """Scan a library folder and add every cue it holds through
        the same _add_item path as manual adds: multi-disc sets
        group into one queue item, every item auto-probes, and the
        disk reconcile marks anything already converted as done
        instead of queueing a second conversion."""
        cues, deep = self._scan_library(root)
        if not cues:
            self.log_line(f"Add Library Folder: no .cue files found "
                          f"in {root} - nothing added.")
            QMessageBox.information(
                self, "Add Library Folder",
                "No .cue files found in that folder.")
            return
        before_ids = {id(it) for it in self.items}
        before_cues = {c for it in self.items for c in it["cues"]}
        before_keys = {id(it): self._set_key(it["cues"])
                       for it in self.items}
        for cue in cues:
            self._add_item([cue])
        # Read the outcome back off the queue itself - every number
        # below is measured from what the adds actually did, not
        # predicted: grouping, exact re-adds and run-time deferrals
        # are all decided inside _add_item, and this is the only
        # place that can see all of them at once.
        new_items = [it for it in self.items if id(it) not in before_ids]
        now_cues = {c for it in self.items for c in it["cues"]}
        fresh = [c for c in cues if c not in before_cues]
        grouped_new = [c for c in fresh
                       if c in now_cues
                       and not any(c in it["cues"] for it in new_items)]
        deferred = []
        for c in fresh:
            if c in now_cues:
                continue
            folder, title = self._set_key([c])
            if any(before_keys[id(it)][1] == title
                   and any(cc in it["cues"] for cc in cues)
                   for it in self.items if id(it) in before_ids):
                # Not queued, and an item that was already in the
                # queue carries this scan's other disc(s) of the
                # same title: its merge was refused by the mid-run
                # guard (the only path that drops an add like this).
                deferred.append(c)
        # Group this scan's cues by the key of the ITEM each one
        # landed in (post-grouping), mapping title -> the distinct
        # items that title produced. A title producing MORE THAN
        # ONE item means the same game was found in more than one
        # place and every copy queued separately. Keying on the
        # scan-side _set_key alone cannot see this: that key is
        # layout-sensitive by design (it climbs out of disc-named
        # folders), so one set legitimately arrives under several
        # scan keys, and two copies under different set roots
        # arrive under different keys too.
        landed = {}
        for c in cues:
            for it in self.items:
                if c in it["cues"]:
                    landed.setdefault(
                        self._set_key(it["cues"])[1], set()).add(id(it))
                    break
        doubles = []
        for it in new_items:
            title = self._set_key(it["cues"])[1]
            if len(landed.get(title, ())) > 1:
                # This scan queued this title as more than one
                # item: the same game from more than one place.
                # Every copy stays queued (the normal path decided
                # - same-set discs grouped, separate copies did
                # not), and the summary counts the items, so
                # nobody wonders why the game appears twice.
                doubles.append(it)
        self.log_line(
            f"Add Library Folder: scanned {root} - {len(cues)} .cue "
            f"file(s) found, {len(new_items)} queue item(s) added"
            + (f", {len(grouped_new)} cue(s) grouped into sets "
               f"already in the queue" if grouped_new else "")
            + (f", {len(cues) - len(fresh)} cue(s) already queued "
               f"exactly (skipped)"
               if len(cues) - len(fresh) else "")
            + (f", {len(deferred)} cue(s) deferred - their set is "
               f"in the queue and a run is in progress; add them "
               f"again after it finishes" if deferred else "")
            + (f", {len(doubles)} item(s) are the same game found "
               f"in more than one place - every copy was queued "
               f"({'; '.join(it['title'] for it in doubles)})"
               if doubles else "")
            + (f", {deep} cue(s) skipped - deeper than "
               f"{self.LIBRARY_SCAN_MAX_DEPTH} folder(s) down" if deep
               else "")
            + ".")

    def remove_selected(self):
        if self._run_active:
            # Removing a row mid-run shifts every row below it; the
            # running worker's remaining items would no longer line
            # up with the rows their statuses belong on.
            self.log_line("Remove ignored while a conversion run is "
                          "in progress - removing a row mid-run "
                          "would misalign the queue's bookkeeping.")
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for row in rows:
            del self.items[row]
            self.table.removeRow(row)

    def probe_selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            QMessageBox.information(self, "Probe",
                                    "Select exactly one queue item to probe.")
            return
        self._queue_probe(rows.pop(), force=True)

    def _queue_probe(self, row, force=False):
        """Probe a row now, or after the running probe finishes. Adding
        a disc auto-probes it; re-adding/probing a probed row needs the
        manual button (force). The pending queue carries the ITEM, not
        the row number, so a removal while probes are queued cannot
        shift a pending probe onto a different game."""
        item = self.items[row]
        if not force and item["probes"]:
            return
        if item not in self._probe_pending:
            self._probe_pending.append(item)
        if self.probe_worker is None or not self.probe_worker.isRunning():
            self._probe_next()

    def _start_probe(self, item):
        template = engine.find_template(self.tpl_edit.text().strip())
        if item not in self.items:
            return
        self._fill_row(self.items.index(item), item, "probing")
        self.probe_worker = ProbeWorker(item, item["cues"],
                                        template, self.players_spin.value() or 1)
        self.probe_worker.log_line.connect(self.log_line)
        self.probe_worker.probed.connect(self._on_probed)
        # An item whose probe has been STARTED stays in the pending
        # list until its result lands. _start_probe is only ever
        # called from _probe_next, which pops from the FRONT; items
        # enter the list only via _queue_probe, and a result landing
        # removes its own item wherever it sits. Nothing ever
        # references a probe by position, so removing a row while
        # probes are in flight cannot misroute or lose one. (The
        # earlier version popped the item when STARTING its probe
        # and let removal-during-probe plus signal queueing decide
        # whether the rest of the chain ever ran - sometimes it
        # did not.)
        self._probe_running = item
        self.probe_worker.start()

    def _on_probed(self, item, data):
        # Consume this item's pending entry wherever it sits - it
        # stays listed from queueing until its result lands (see
        # _start_probe), so a result that arrives after reordering
        # still finds and clears exactly its own entry.
        if item in self._probe_pending:
            self._probe_pending.remove(item)
        if getattr(self, "_probe_running", None) is item:
            self._probe_running = None
        if item not in self.items:
            # The item was removed while its probe was in flight:
            # the result has no home - drop it, keep chaining.
            self._probe_next()
            return
        row = self.items.index(item)
        if not data:
            self._fill_row(row, item, "probe failed")
            self._probe_next()
            return
        item["probes"] = data["probes"]
        item["verdict"] = data["verdict"]
        item["art"] = data["art"]
        if item["probes"]:
            item["name"] = engine.compact_name(
                item["probes"][0].get("display_name") or item["title"])
        self._fill_row(row, item, "probed")
        self.show_selected()
        self._reconcile_done()       # the name may have just changed
        self._probe_next()

    def _probe_next(self):
        for item in list(self._probe_pending):
            if item is getattr(self, "_probe_running", None):
                continue            # started; result still coming
            if item not in self.items:
                self._probe_pending.remove(item)
                continue            # removed while waiting its turn
            self._start_probe(item)
            return

    def show_selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            return
        item = self.items[rows.pop()]
        if item["art"] and Path(item["art"]).exists():
            pix = QPixmap(item["art"]).scaledToWidth(220)
            self.art_label.setPixmap(pix)
        else:
            self.art_label.setText("(no cover art)")
        if not item["probes"]:
            self.probe_label.setText("Not probed yet.")
            return
        lines = []
        for i, p in enumerate(item["probes"]):
            lines.append(
                f"Disc {i+1}: {p.get('display_name')} | {p.get('serial')} | "
                f"boot {p.get('boot_exe')} | entry {p.get('entry_pc')} | "
                f"load {p.get('load_address')} | text {p.get('text_size')} | "
                f"{p.get('track_count')} tracks | {p.get('seed_count')} seeds")
        if item["verdict"]:
            lines.append(f"Set verdict: {json.dumps(item['verdict'])[:200]}")
        self.probe_label.setText("\n".join(lines))

    def _reconcile_done(self):
        """Ground truth from DISK: a queue item whose requested
        packages already exist - complete and non-empty - in the
        output folder IS done, whatever this session's table says.
        Runs when an item is added, when its probe lands (the probe
        can change the game's name, hence its package names), and
        before every queue run - so a restarted app, or a session
        whose display fell behind the work, heals from the folder
        instead of re-converting finished games. Never touches a
        row that is running right now."""
        if not self.items:
            return
        settings = self.current_settings()
        work_dir = self.out_edit.text().strip()
        self._status_override = True
        try:
            for row, item in enumerate(self.items):
                if item.get("state") == "running":
                    continue
                try:
                    job = engine.Job(cues=item["cues"], name=item["name"],
                                     title=item["title"].split(" (")[0],
                                     work_dir=work_dir, settings=settings,
                                     slug_override=item.get("slug", ""),
                                     art=item.get("art", ""))
                    complete = engine.job_packages_complete(job)
                except Exception:
                    # A malformed item or an odd output folder must
                    # never break the queue - it just isn't done.
                    complete = False
                    job = None
                if complete:
                    if item.get("state") != "done":
                        paths = engine.package_paths(job)
                        self.log_line(
                            "already converted (package on disk): "
                            + (str(paths[0]) if paths
                               else item.get("title", "?")))
                    self._fill_row(row, item, "done")
        finally:
            self._status_override = False

    def _worker_status(self, todo_items, idx, status):
        """Deliver a worker status to its row. The row is resolved
        by ITEM IDENTITY at delivery time - never by a row number
        captured when the run started: rows are the display's
        business, items are the truth, and a captured row map is
        how statuses used to land on the wrong games. A status for
        an item that has left the queue goes nowhere."""
        if not (0 <= idx < len(todo_items)):
            return
        item = todo_items[idx]
        row = next((r for r, it in enumerate(self.items)
                    if it is item), None)
        if row is None or row >= self.table.rowCount():
            return
        self._fill_row(row, item, status)

    def _queue_finished(self):
        if self._run_active:
            self._run_active = False
            self.log_line("=== QUEUE FINISHED ===")
            self._flush_log()

    def run_queue(self):
        if self._run_active or (self.worker is not None
                                and self.worker.isRunning()):
            # A hard no-op, and it says so: two workers over one
            # queue would interleave their status writes over the
            # same rows - the scrambled-table failure class.
            self.log_line("Run Queue ignored: a conversion run is "
                          "already in progress (games convert one "
                          "at a time; a second run cannot start "
                          "until this one finishes).")
            return
        if not self.items:
            QMessageBox.information(self, "Run Queue", "The queue is empty.")
            return
        self._save_settings()
        self._reconcile_done()
        todo = [(row, item) for row, item in enumerate(self.items)
                if item.get("state") != "done"]
        if not todo:
            QMessageBox.information(self, "Run Queue", "Everything is done.")
            return
        todo_items = [it for _, it in todo]
        self.worker = Worker(todo_items,
                             self.current_settings(),
                             self.out_edit.text().strip())
        self.worker.log_line.connect(self.log_line)
        self.worker.item_status.connect(
            lambda i, s: self._worker_status(todo_items, i, s))
        self.worker.finished_all.connect(self._queue_finished)
        # Backstop: QThread.finished fires even if run() dies early.
        self.worker.finished.connect(self._queue_finished)
        # A new run starts a NEW status lifecycle for its items -
        # this reset (and the disk reconcile above) is the one
        # deliberate exception to the monotonic lifecycle, and it
        # happens here, on the GUI thread, before the worker starts.
        for item in todo_items:
            item["state"] = "queued"
        for row, item in todo:
            self._fill_row(row, item, "queued")
        self._run_active = True
        self.worker.start()
        self.log_line("=== QUEUE STARTED (one game at a time) ===")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
