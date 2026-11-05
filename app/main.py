#!/usr/bin/env python3
"""Recomp Tools - a friendly front-end for the PSXRecomp pipeline.

Pick discs, see what they are (probe panel + cover art), tick the
house-rule options, and convert - one game at a time, start to finish,
with a live log. Built for Duane so conversions don't need the CLI.

Run:  .venv/bin/python app/main.py
"""
import json
import re
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt, QThread, Signal
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
                 your RetroAchievements file if you picked one.

THE QUEUE
Add discs with "Add Disc". Games convert ONE AT A TIME, start to
finish - that is deliberate (parallel Generates run out of RAM) and it
keeps failures contained: a failed game is marked red, logged, and the
queue moves on.

MULTI-DISC GAMES
Just add the discs - one at a time with "Add Disc", or together with
"Add Disc Set". Discs from the same folder with the same game title
are grouped into ONE queue item automatically. When the set runs, it
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
        for idx, item in enumerate(self.items):
            self.item_status.emit(idx, "running")
            job = engine.Job(cues=item["cues"], name=item["name"],
                             title=item["title"].split(" (")[0],
                             work_dir=self.work_dir,
                             settings=self.settings,
                             slug_override=item.get("slug", ""),
                             art=item.get("art", ""))
            try:
                engine.run_job(job, self.log_line.emit)
                self.item_status.emit(idx, "done")
            except engine.StageError as ex:
                msg = str(ex)
                self.log_line.emit(f"FAILED: {msg}")
                self.item_status.emit(
                    idx, "needs review" if "needs review" in msg else "failed")
            except Exception:
                self.log_line.emit(traceback.format_exc())
                self.item_status.emit(idx, "failed")
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
    probed = Signal(int, dict)

    def __init__(self, row, cues, template, players):
        super().__init__()
        self.row, self.cues = row, cues
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
                self.probed.emit(self.row, {})
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
            self.probed.emit(self.row, {"probes": probes, "verdict": verdict,
                                        "art": str(art) if art else ""})
        except Exception:
            self.log_line.emit(traceback.format_exc())
            self.probed.emit(self.row, {})


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Recomp Tools")
        self.resize(1180, 800)
        self.items = []               # queue model: dicts
        self.worker = None
        self.probe_worker = None
        self._probe_pending = []      # rows waiting for an auto-probe
        self._build_ui()
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
        self.log.appendPlainText(str(msg))

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
        for row, item in enumerate(self.items):
            cell = self.table.item(row, 5)
            if cell and cell.text() in ("done", "running"):
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
        self._queue_probe(row)        # auto-probe every added disc/set

    def _fill_row(self, row, item, status):
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

    def remove_selected(self):
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
        manual button (force)."""
        if not force and self.items[row]["probes"]:
            return
        if self.probe_worker is not None and self.probe_worker.isRunning():
            if row not in self._probe_pending:
                self._probe_pending.append(row)
            return
        self._start_probe(row)

    def _start_probe(self, row):
        template = engine.find_template(self.tpl_edit.text().strip())
        self._fill_row(row, self.items[row], "probing")
        self.probe_worker = ProbeWorker(row, self.items[row]["cues"],
                                        template, self.players_spin.value() or 1)
        self.probe_worker.log_line.connect(self.log_line)
        self.probe_worker.probed.connect(self._on_probed)
        self.probe_worker.start()

    def _on_probed(self, row, data):
        if not data:
            self._fill_row(row, self.items[row], "probe failed")
            self._probe_next()
            return
        item = self.items[row]
        item["probes"] = data["probes"]
        item["verdict"] = data["verdict"]
        item["art"] = data["art"]
        if item["probes"]:
            item["name"] = engine.compact_name(
                item["probes"][0].get("display_name") or item["title"])
        self._fill_row(row, item, "probed")
        self.show_selected()
        self._probe_next()

    def _probe_next(self):
        while self._probe_pending:
            row = self._probe_pending.pop(0)
            if 0 <= row < len(self.items):
                self._start_probe(row)
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

    def run_queue(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.items:
            QMessageBox.information(self, "Run Queue", "The queue is empty.")
            return
        self._save_settings()
        todo = []
        for row, item in enumerate(self.items):
            status = self.table.item(row, 5)
            if status and status.text() in ("done",):
                continue
            todo.append((row, item))
        if not todo:
            QMessageBox.information(self, "Run Queue", "Everything is done.")
            return
        self.worker = Worker([it for _, it in todo],
                             self.current_settings(),
                             self.out_edit.text().strip())
        row_map = {i: row for i, (row, _) in enumerate(todo)}
        self.worker.log_line.connect(self.log_line)
        self.worker.item_status.connect(
            lambda i, s: self._fill_row(row_map[i], self.items[row_map[i]], s))
        self.worker.finished_all.connect(
            lambda: self.log_line("=== QUEUE FINISHED ==="))
        for row, _ in todo:
            self._fill_row(row, self.items[row], "queued")
        self.worker.start()
        self.log_line("=== QUEUE STARTED (one game at a time) ===")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
