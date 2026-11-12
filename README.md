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

- **v1.0.22 (2026-11-12)** — Packages can arm the text-image
  guard, pre-flight probe classification, and the OpenGL
  guest-grid v2 phase. (1) Guard packaging: stage_package now
  ships the boot EXE at the path game.toml names
  (disc/<boot exe>) plus the framework licenses/ folder.
  Without them the runtime text-image guard has no local
  reference image to validate against — and without the
  license pin the package root resolves against the wrong
  folder — so shipped packages left the guard unarmed and
  runtime-loaded code fell back to slow interpretation (the
  Tekken 3 stutter class). A package that cannot stage both
  now fails at packaging instead of shipping unarmed. Re-run
  games under this version to pick the fix up. (2) Pre-flight:
  every probe result now carries a `preflight` section
  classifying the disc before anything builds — CLEAN,
  NEEDS-CLEAN-PATH (a shell-hostile character in the display
  name; the apostrophe in Tony Hawk's / Director's Cut is the
  known killer), PADDED-EXE (boot EXE larger than its header
  total with an all-zero tail — mastered-in padding, Twisted
  Metal III), EXE-OVERSIZE-CAUTION (non-zero tail — not
  assumed padding), EXE-UNDERSIZE-CAUTION, and
  MULTI-EXE-UNSUPPORTED (a disc set whose discs boot
  different programs — the Xenogears class; classify a whole
  set with the probe's new --disc-set-cue / --disc-set-list
  options). Track layout is reported as information only: a
  single-track cue is how some discs were mastered, never a
  failure flag. (3) OpenGL guest-grid v2: the fragment
  shader's guest pixel is computed from gl_FragCoord in
  integer arithmetic instead of an interpolated varying that
  deviates systematically on NVIDIA hardware. Honest status
  for Doom-engine games: combing still shows when OpenGL is
  scaled above 1x — use the Software renderer, or OpenGL at
  1x (native scale). The DDA ownership rule itself is
  unchanged, and v2 applies on top of v1-patched trees, so
  games converted with v1.0.19 upgrade in place on their
  next run.

- **v1.0.21 (2026-11-07)** — Add Library Folder, plus two
  probe-chain fixes its stress test caught. (1) Add Library
  Folder: a third add control beside "Add Disc" and "Add Disc
  Set". Pick the folder that holds your PS1 games and the tool
  scans it recursively — up to 4 folder levels deep, covering
  cues in the root, per-game subfolders and per-disc subfolders
  — and adds every `.cue` it finds through the normal add
  path. Multi-disc sets group into ONE queue item
  automatically, however the discs are laid out, and anything
  whose package is already complete in the output folder
  reconciles to "done" instead of queueing for a second lap.
  Hidden folders and the tool's own conversion-project folders
  (`-recomp`, `psxrecomp`, `recomp-ui`) are skipped, so
  pointing it at a folder that contains old conversion
  projects is safe; the same cue found twice is added once; a
  game found in two different places is counted in the log
  summary, not silently merged; and a folder with no cues says
  so in a message box instead of failing silently. Measured on
  a fabricated fixture library: 300 cues became 290 queue
  items in about 4 seconds, every item probed. (2) Probe
  results can no longer land on the wrong game. The probe
  chain as shipped in v1.0.20 matched results to games by
  queue row number, so removing a row while probes were still
  landing shifted later results onto the wrong games — one
  game's row could end up wearing another game's disc details.
  Results now travel with the queue item itself, and results
  for items removed mid-flight are dropped. This covers "Add
  Disc" and "Add Disc Set" probing too; it is the same chain.
  (3) The probe chain can no longer lose its last link. In
  about one run in three, the final queued item was never
  probed: an item left the pending list when its probe
  started, so chain advancement depended on a single signal
  arriving exactly once, and any reordering ended the chain
  early. An item now stays pending until its own result lands.
  Both probe defects were caught by the new feature's stress
  tests and are covered by the 32-check add-library suite
  (four consecutive runs, zero failures); the queue suite
  (23/23) and the 007 suite (32/32) still pass. Also: the GUI
  help text for Gran Turismo's title-screen crop no longer
  claims it was "FIXED IN v1.0.20" — hardware acceptance of
  that fix failed on 2026-11-07 (the crop was unchanged), and
  the help text now says so plainly. The crop itself is NOT
  fixed by this release; its diagnostic run on real hardware
  is still pending. No engine changes in this release — GUI
  only (`app/main.py`, plus this changelog).

- **v1.0.20 (2026-11-07)** — Queue truth, plus three field
  fixes. (1) Digit-leading titles finish: games whose titles
  start with a digit (007 Racing, 007 The World Is Not Enough,
  007 Tomorrow Never Dies) converted completely, then failed at
  the finish line — the framework derived the finished
  program's name with a leading underscore (`_007Racing`) while
  the tool checked for the plain name. The engine now pins the
  program name in the scaffolded project, so these games build,
  verify and package under their real names; letter-leading
  titles are unchanged. Proven end to end on the real 007
  Racing disc (full pipeline, program named exactly
  `007Racing`, booted to its PRESS START title) and through
  verify on 007 The World Is Not Enough; the Windows program
  name is configure-verified only (a full Windows compile of
  these titles was not run). (2) Gran Turismo title-screen
  crop: a static 24-bit screen shown right after a display-band
  change (GT's 640x480 title) lost its right ~80 px for the
  whole session — the depth24 coverage counter discarded
  uploads landing in the present-hold after the change, and
  the change itself zeroed the count, so a once-uploaded
  picture never re-grew it. The engine's depth24 patch now
  counts those uploads and defers the reset to the next
  upload. Verified in the development VM (GT rebuilt
  pixel-identical to the pre-fix build outside the patched
  accounting; the Resident Evil: Director's Cut movie
  regression check still passes full width) — the deciding
  test on real hardware, GT's title screen at full width, is
  still to come, so treat this fix as shipped but not yet
  hardware-confirmed. (3) The queue can no longer lie about
  status. Field reports from a 34-disc run: rows read "done"
  for games with no package in the output folder, a game read
  "done" while still converting, finished games' rows lagged
  behind the work, and a reopened queue re-converted finished
  games from scratch. Now a job reports done only after every
  package it was asked for is verified present and non-empty
  on disk (exact filenames, so South Park can never borrow
  South Park Rally's package); the queue reconciles against
  the output folder when games are added and before every run,
  so finished games show done and are skipped; statuses move
  in one direction only and are routed by game, not by row
  position; rows cannot be removed or merged mid-run; a second
  Run press is a logged no-op; and the log window paints in
  batches instead of line by line, so a long compile's output
  can no longer starve the status column. The engine's
  subprocess runner also gained the wall-clock deadline the
  cartridge tools already had — a conversion step that goes
  silent can no longer hang a queue forever — and the
  framework-clone, symbol-check and tag calls gained timeouts
  of their own. Covered by 23 automated queue tests. (4)
  Engine patches fail loud: the seven text patches the engine
  applies to the framework (probe subdirectory boot, overlay
  draw chain, CD GetlocP, depth24 span and hold, and both
  guest-grid renderer patches) used to skip silently when a
  framework update moved their anchors, so a game could ship
  missing a fix with no sign. A patch that cannot land now
  fails the job with the reason instead of shipping the game
  broken. All seven are verified landing on a fresh framework
  download and still idempotent on re-runs. Also: the launcher
  script now retries the dependency install when the
  environment exists but its packages are missing (previously
  only a first-ever run installed them).

- **v1.0.19 (2026-11-06)** — Doom / Final Doom wall & floor
  striping fix (software + OpenGL renderers): at internal
  resolution above 1x, textured walls and floors in Doom-engine
  games showed dense vertical striping (worst at distance), and
  hairline dark seams could appear along wall/floor joins. The
  cause was coverage, not texture filtering: these engines build
  the world from one-guest-pixel-thin textured triangles, and the
  hi-res rasterizer covered the scaled geometry inexactly,
  leaving sub-pixel slivers claimed by the wrong surface or
  none. The fix makes both renderers decide coverage on the
  guest pixel grid — the same biased edge rule the native
  renderer uses — so abutting strips tile with no gaps and no
  double-claims at any internal scale, while texture sampling
  inside each guest pixel stays at full internal resolution
  (no blur, no geometry changes). The OpenGL backend runs the
  same ownership test in its fragment shader; its edge math was
  fuzz-verified bit-exact against the native rule over 115M
  pixels, zero mismatches. Verified on camera in Final Doom:
  stripe-period modulation at 4x internal fell from 19.8% to
  0.67% (software renderer) and from 14.2% to 0.33% (OpenGL);
  coverage-visualization split guest pixels in wall interiors
  fell from ~52–89% to 0.0% (software) and 51–57% to 0.3–1.2%
  (OpenGL). Per-renderer status, honestly: the VULKAN renderer
  does not have this fix yet (separate pipeline) — for Doom and
  Final Doom, choose the Software or OpenGL renderer, not
  Vulkan. Re-run Doom / Final Doom (or any game) under v1.0.19
  to pick the fix up.

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
