# Recomp Tools

A friendly front-end for the PSXRecomp pipeline: pick PS1 discs, probe
them, tick the house-rule options, and convert — one game at a time —
into packaged native PC recomps (Linux + Windows), with the launcher
UI and RetroAchievements wired in.

## Run it

- Linux: `./recomp-tools.sh` (or `.venv/bin/python app/main.py`)
- The engine also runs headless:
  - `.venv/bin/python app/engine.py toolbox`
  - `.venv/bin/python app/engine.py probe --cue "Game (USA).cue"`
  - `.venv/bin/python app/engine.py run --job job.json`

## What's inside

- `app/main.py` — the Qt window (Convert tab + Help tab).
- `app/engine.py` — the conversion pipeline (scaffold → probe → patch →
  RA wiring → emitters → generate → build → verify → package). The GUI
  and the CLI drive the same code.
- `bundle/ra/` — the RetroAchievements module (src_ra + rcheevos),
  copied into each new project when RA is enabled. Includes the login
  toast, the callback-polarity fix, pop-ups, and the
  `RA_DISABLE_POPUPS` build-time toggle.
- `art-cache/` — cover art fetched from libretro-thumbnails.

## Credits

- **PSXRecomp** — the static-recompilation framework this tool drives —
  created by **Matthew Stanley (mstan)**, now part of the
  [RetroPortingToolkit](https://retroportingtoolkit.com/)
  ([github.com/mstan](https://github.com/mstan),
  [github.com/RetroPortingToolKit](https://github.com/RetroPortingToolKit)).
  PSXRecomp is licensed under PolyForm Noncommercial 1.0.0.
  Recomp Tools does not redistribute PSXRecomp; it uses a copy from one
  of your own projects, with the framework's license intact.
- **recomp-ui** (the launcher in every converted game) —
  RetroPortingToolKit, MIT License, © 2026 Matthew Stanley.
- **rcheevos** (bundled in `bundle/ra/`) — RetroAchievements, MIT.
- **OpenBIOS** — the open-source BIOS bundled with PSXRecomp.
- Cover art: [libretro-thumbnails](https://thumbnails.libretro.com/).

## Changes

- **v1.0.18 (2026-11-06)** — Runtime-loaded code (overlay) fix:
  many games load part of their code from the disc while they
  run, and in every package built so far that code was stuck in
  the runtime's slow interpreter — the native overlay tier never
  engaged, because no package carried the compiler toolchain the
  runtime needs (measured: a Tekken 1 fight ran 96.8%
  interpreted). The engine now finishes every conversion of such
  a game by (1) staging the framework's own pinned
  `overlay_toolchain/` — built around the game's OWN recompiler
  binary, as the runtime's stale-toolchain check demands — into
  both packages, (2) playing the game once headlessly (boot,
  menus, attract mode) so the runtime captures the code it loads
  at runtime, (3) compiling the captured set offline into a
  pre-warmed native shard `cache/` that ships in the package
  (Windows shards are cross-compiled here; titles whose code
  runs in CPS mode are detected from their generated dispatch
  and compiled with the matching flag), and (4) noting it in the
  package README. A game whose warm session captures nothing
  ships exactly as before — no toolchain, no cache. Measured on
  Tekken 1 (SLUS-00006): a fight's interpreter load fell from
  262–276k instructions/frame (96.8% interpreted) to 14–30
  instructions/frame once its regions promoted; the shipped
  cache (19 Linux / 184 Windows shards) covers everything the
  staging session reached, and the bundled toolchain keeps
  compiling any remaining code live on your machine the first
  time you play it. Gran Turismo (SCUS-94194) is the honest
  exception: staging engages there too (93 Linux / 603 Windows
  shards ship, and native execution rises from ~0% to ~25% of
  race time), but its race hot loop stays ~74% interpreted even
  with every shard compiled — a separate runtime defect that
  staging is necessary for but does not cure, so GT is NOT
  fixed by this release; it is measured and labelled as such.
  Resident Evil: Director's Cut picked up a small cache the
  same way (it loads one small overlay for its menus). Package
  sizes grow — the toolchain bundle is ~72 MB before
  compression; that is the framework's own pinned payload.
  Windows packages are compile-verified only, as before: their
  caches are built and layout-validated here, not run-verified.
  Hardening found by the v1.0.18 clean-room run: staging downloads
  its pinned Python/TinyCC payloads on first use, and a transient
  network timeout there no longer kills the whole conversion —
  the staging step retries (downloads are cached, so a retry
  resumes where it stopped); only a failure that survives every
  attempt fails the job.

- **v1.0.17 (2026-11-05)** — FMV crop fix: the runtime's depth24
  (24-bit movie) upload-span tracker only counted CPU→VRAM uploads
  ≥256 halfwords wide, but Capcom's STR players upload each movie
  frame as narrow vertical strips (Resident Evil: Director's Cut:
  20 strips of 24×240), so a movie's true coverage never registered
  and the staging pass blacked out every frame past a stale 213-px
  limit — movies rendered ~2/3 width, hard against the left edge,
  while gameplay was full width. The engine now patches the
  framework's `gpu.c` in the template and in every scaffolded
  project: every upload counts toward the span while depth24 is on,
  and the staging blanks nothing at full coverage, otherwise exactly
  the uncovered tail (the narrow-blit stale-VRAM case the fill was
  built for still blanks its tail). Re-run an affected game here to
  pick up the fix; RE Director's Cut verified on camera (intro, logo
  and story FMVs full width). Mega Man 8 uses the same upload
  pattern and is cured by the same patch. Windows packages are
  compile-verified only, as before.

- **v1.0.16 (2026-11-05)** — CD position fix: the runtime's GetlocP
  answer for the not-playing drive hardcoded track 1, so games that
  confirm a seek into a CD music track before loading a level
  (Tomb Raider 1 + 2) hung on a black screen with no audio. The
  engine now patches the framework's `cdrom.c` in the template and
  in every scaffolded project: GetlocP reports the real containing
  track with hardware-correct pregap sub-Q. Re-run an affected game
  here to pick up the fix; TR1 + TR2 verified into gameplay.

## Notes

- Conversions run **one game at a time** on purpose: parallel Generate
  stages run out of RAM, and serial runs keep failures contained.
- The framework is copied from a *template project* (any finished
  recomp) when the online clone fails — pick one in the settings if
  auto-detect finds none.
- Windows `.exe` packaging needs PyInstaller on a Windows machine;
  the app itself is one cross-platform codebase.
