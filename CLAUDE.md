# Video Renamer: notes for working on this project

A PySide6 (Qt 6) + libmpv desktop app for Linux (developed on CachyOS, KDE Plasma, Wayland). The workflow is: rip a disc with MakeMKV → preview and rename the files → optionally match them with TheDiscDB → export the folder to a NAS with rsync.

For the history of how and why things were built, see `docs/DEVELOPMENT_LOG.md`. For user-facing features and shortcuts, see `README.md`.

## Run and test

```sh
./run.sh [folder]          # the app
python3 -m unittest        # unit tests (tests/, ~56), no GUI or network needed
```
- **System packages:** `pyside6 mpv python-mpv mkvtoolnix-cli rsync`, plus `makemkv` for ripping. No pip or virtualenv.
- **Scope of the unit tests:** they cover the pure modules. The GUI was checked with throwaway scripts (not in the repo) that drive `MainWindow` directly. For those, set `XDG_CONFIG_HOME=<scratch>` so the user's real settings stay untouched.
- **Offscreen testing:** use `QT_QPA_PLATFORM=offscreen` when no GL is needed. The mpv player needs a real window (a GL context), so test it on the Wayland session.

## Layout

| Module | Role |
|---|---|
| `__main__.py` | QApplication setup, starts `MainWindow` |
| `main_window.py` | Tree + player + name box; all actions/menus; `do_renames()` (the single entry point for renames/moves, with player release/resume and undo); `FileTree` (drag & drop moves); delete; TheDiscDB apply |
| `file_model.py` | `VideoTreeModel` (QStandardItemModel, rebuilt from disk on every refresh) + `VideoTreeProxy` (folders first, recursive filter) |
| `renamer.py` | **Pure** rename logic: validate, plan (clashes), two-phase apply with rollback, subfolder creation, `translate()` for paths inside renamed folders |
| `player.py` | `PlayerWidget`: libmpv rendered into a `QOpenGLWidget`; seek bar with chapter ticks; chapter/video/audio/subtitle pickers |
| `batch_dialog.py` | Pattern batch rename with live preview |
| `makemkv.py` | **Pure**: makemkvcon robot-output parsing, commands, all-tracks profile, track matching for stripping, language handling |
| `rip_dialog.py` | Rip window (one per drive), QProcess pipeline scan → rip → strip, live stats, report |
| `ripstats.py` | **Pure**: transfer meter, speed multiples, time formatting |
| `discdb.py` | **Pure**: TheDiscDB GraphQL query, parsing, length matching, Jellyfin-style name suggestions |
| `discdb_dialog.py` | Match window (QNetworkAccessManager), approve/edit table |
| `export.py` / `export_dialog.py` | rsync args, progress parsing, post-copy size verification / Export window |
| `settings.py` / `settings_dialog.py` | Every setting key and default in one place / Settings window (4 tabs) |

## Design decisions (don't undo without a reason)

- **Tree:** full rebuild from disk on each refresh (fast: about 0.1 s for 3,000 files), with the proxy detached during the rebuild. Expanded folders, selection, current item and scroll position are restored. Auto-scroll is switched off during the rebuild, because otherwise it re-opens collapsed folders. A `QFileSystemWatcher` triggers refreshes (500 ms debounce, deferred while an editor or dialog is open).
- **Undo:** entries are *lists of steps*, undone last-first, one batch each. Steps can depend on each other (files renamed, then their folder), so they can't be merged into one batch.
- **Renames always go through `renamer.apply_renames`**, which moves everything via temp names first, so swaps and chains work, and rolls back on failure.
- **libmpv:**
  - `LC_NUMERIC` must be `"C"` right before creating `mpv.MPV`, because QApplication resets it and libmpv then segfaults.
  - Loads are deferred until the GL render context exists; otherwise the first file gets no video.
  - `fbo_format="rgba16"`: the default `rgba16f` fails in Qt's GL context (GL_INVALID_ENUM) and blanks HDR video as soon as subtitles are on.
  - `keep_open_pause="no"`.
  - The current chapter is derived from the position, because mpv's `chapter` property lags.
  - Nothing autoplays.
- **Scrubbing:** keyframe seeks while dragging, at most one in flight; one exact seek on release or 250 ms after the last click. Seeks are clamped 1 s before the end, so the file never hits EOF and pauses.
- **MakeMKV:**
  - `makemkvcon` can pick titles but not tracks. The app rips with its own profile (`defaultSelection="+sel:all"`), then keeps the ticked tracks with `mkvmerge`, matched in order by type, codec, language and forced flag.
  - The file uses ISO 639-2/B codes (`fre`), while MakeMKV reports /T codes (`fra`); `normalize_language` handles both.
  - "Forced only" subtitle tracks are dropped by MakeMKV when the disc has no forced subtitles. That's not an error.
  - If a normal ticked track is missing from the file, all tracks are kept and the report warns about it.
  - Title ids depend on `--minlength`, so scan and rip must use the same value.
  - The registration key is never stored by the app. Settings runs `makemkvcon reg`, and MakeMKV saves the key with its own settings, which the GUI shares. Key and evaluation problems are spotted by message text (`makemkv.KEY_PROBLEMS`) and explained after a failed scan or rip.
- **Rip progress:** the top bar is the overall progress over all titles, weighted by size; the second bar is the current title, driven by bytes written once copying starts. MakeMKV's "preparing" phase (0 bytes written) is shown separately in the stats.
- **Settings:** the Rip window must not silently change the default rip folder or minimum length; only Settings does. The export destination used moves to the top of the list.
- **`makemkvcon` version check:** uses `--noscan` (0.5 s instead of ~12 s) and runs off the GUI thread.
- **TheDiscDB:** never automatic. Match by length (±2 s) on named titles only; the user approves and can edit every rename. The folder and file renames are recorded as one multi-step undo entry.
- **Several exports:** one `ExportDialog` per selected folder, tracked in `MainWindow._export_dialogs`. They copy one at a time by default. Starting while another copies offers "wait", which puts it in a queue (`queued_at`). Each dialog emits `idle` when a copy ends or stops, and `MainWindow._start_next_export` then starts the one that has waited longest.
- **Deleting:** goes to the Trash by default via `QFile.moveToTrash`; the confirmation can be turned off with "Don't ask again". The player is released first.

## Packaging

- **App id** `io.github.cooldead.VideoRenamer` (`main_window.APP_ID`): desktop file, icon and metainfo in `data/`, and the Flatpak id. `setDesktopFileName` uses it, so Wayland matches the window to the installed desktop file.
- `packaging/install.sh <destdir> <prefix>` is the one install step for both packages. The code goes into `<prefix>/lib/video-renamer`, not site-packages, so a Python upgrade doesn't break the pacman package. The launcher is `<prefix>/bin/video-renamer`.
- **pacman:** `packaging/arch/PKGBUILD` builds from the `v$pkgver` git tag and runs the unit tests. To test before tagging, use a copy whose `prepare()` rsyncs the working tree.
- **Flatpak:** `packaging/flatpak/`, on io.qt.PySide.BaseApp and KDE 6.11. libmpv is built with LuaJIT, because `osc=no` fails without Lua. `host-tool.sh` is installed as makemkvcon, mkvmerge, rsync, ffprobe and gio, and runs them on the host with `flatpak-spawn --host`. Exit 127 = not on the host, which `tool_version` reports as not found.
  - **Stopping:** use `rip_dialog.stop_process` (SIGTERM, then SIGKILL). flatpak-spawn passes SIGTERM on to the host program but can't pass SIGKILL, so a bare `kill()` would leave the rip or copy running. Tested.
  - **Trash:** `QFile.moveToTrash` fails in the sandbox; `main_window.move_to_trash` then uses `gio trash` (on the host).
  - Host programs start in the app's working folder; the wrapper switches to `$HOME` when that folder is `/app` or `/usr`, which don't exist on the host.
- **Version numbers** are in `packaging/arch/PKGBUILD` (`pkgver`) and in the metainfo `<releases>`. Bump both before tagging a release.

## Conventions

- Match the existing style: pure logic in modules without Qt, with unit tests. Dialogs are thin. Comments explain *why*.
- The repository is **public**: https://github.com/cooldead/video-renamer (MIT). Never commit personal details: NAS addresses, drive serials, home paths beyond the desktop file, emails.
- Releases: `gh release create vX.Y.Z` with notes. So far: v1.0.0 (2026-09-25, first release) and v1.1.0 (2026-09-26, several exports copied one at a time), v1.2.0 (2026-09-27, MakeMKV registration key), v1.3.0 (2026-09-27, pacman and Flatpak packages). Bump the minor version for new features and the patch version for fixes.
- **Logging:** after each change, add an entry to `docs/DEVELOPMENT_LOG.md`, and update this file if a design decision changes.
