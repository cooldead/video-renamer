# Development log

How Video Renamer was built. It was developed with Claude Code in one long session on 2026-09-25. The entries are in the order things were requested; for each they note what was built, the decisions made and the bugs found. Architecture and gotchas are summarised in `CLAUDE.md`.

## 1. Initial app: list, preview, rename
- **Request:** a native Linux app that lists video files, previews them, and allows renaming while previewing. VLC was suggested.
- **Decision:** Qt Multimedia instead of VLC. VLC 3 can't embed video natively on Wayland.
- **Built:** folder list, player, name box (extension kept), Rename & Next (Ctrl+Enter), batch rename with patterns (`{n}`, `{name}`, `{date}`, `{parent}`, find/replace, regex), undo.
  - Safe two-phase renames with rollback.
- **Bug fixed:** `{name}` was treated as the `{n}` counter.
- **Test artifact:** with a scratch `XDG_CONFIG_HOME`, the toolbar showed a light palette. Not an app bug.

## 2. No autoplay; audio track selection
- **Request:** files should open paused, and add an audio track picker ("some audio issues").

## 3. Dolby TrueHD 7.1 and chapters → switched to libmpv
- **Problem:** TrueHD Atmos 7.1 was unreliable with Qt Multimedia.
- **Decision:** the user chose to replace it with libmpv (python-mpv), rendered into a QOpenGLWidget. This gives TrueHD/Atmos, HDR/Dolby Vision tone mapping and nvdec GPU decoding.
- **Added:** chapters (picker, ticks on the seek bar, Ctrl+←/→).
- **Bugs fixed:**
  - The first "next chapter" did nothing: mpv reports chapter −1 at 0:00. Stepping now uses the chapter times.
  - The combo lagged by one: the current chapter is now derived from the position.
  - libmpv segfaulted at start ("Non-C locale detected") because QApplication resets `LC_NUMERIC`. It's now set to `"C"` right before creating mpv.

## 4. Subfolders
- `/` in names creates subfolders.
- New Folder (Ctrl+Shift+N), Move to Folder (Ctrl+M).
- Undo removes the folders it created.

## 5. Folder tree with auto-refresh
- **Built:** tree model, and a filesystem watcher so changes made outside the app show up.
- **Bugs fixed:**
  - A refresh re-opened collapsed folders: auto-scroll is now switched off during rebuilds.
  - The Name column was squashed after a refresh: header settings are re-applied after the proxy re-attaches.

## 6. Folder renaming, player layout, stream info, subtitles, right-click menu
- **Built:** a full-width seek bar with the controls below it; a video stream picker with an info label (HDR type, bit depth, GPU/CPU decoding); a subtitle picker; a context menu.
- **Bugs fixed:**
  - No video for the first file loaded at start-up: loads are now deferred until the GL context exists.
  - "12-bit" shown for nv12: the bit depth is now parsed from the pixel format.

## 7. Playback bugs: scrubbing and subtitles
- **Scrubbing** stalled or garbled 4K HEVC. Every slider move sent an exact seek (164 per drag), and one hit EOF, where keep-open paused playback.
  - **Fix:** throttled keyframe seeks, one exact seek when the drag settles, clamp 1 s before the end, `keep_open_pause=no`.
- **Subtitles** turned HDR video black while playing.
  - **Cause:** the default `fbo-format=rgba16f` fails in Qt's GL context (GL_INVALID_ENUM).
  - **Fix:** `fbo_format="rgba16"`, found by testing the mpv options one at a time. Switching to desktop GL made no difference and was reverted.
- **Drag-and-drop moving** in the tree was added in this round.

## 8. MakeMKV integration and NAS export
- **Request:** a rip → rename → export workflow, with titles and tracks chosen as in MakeMKV's own app.
- **Findings:**
  - `makemkvcon` can't select tracks, so the app rips all of them with its own profile (`--profile`, `+sel:all`) and strips the unticked ones with mkvmerge (package mkvtoolnix-cli).
  - Robot-mode output was parsed from a real UHD disc, which is now a test fixture.
- **Export:** rsync into a NAS folder, verification of every file's size, optional delete after a verified copy.

## 9. Two drives at once
- **Built:** one Rip window per drive, windows never share a drive or an output folder, and `Name (2)` is used for identical disc names.
  - A rip that finishes while the user is busy doesn't steal the selection.

## 10. Checking the first real rips
- **Result:** two concurrent UHD rips were verified: lengths and chapters matched the discs, and a full read found no corruption. The flood of "non monotonically increasing dts" messages from TrueHD in ffmpeg is harmless.
- **Bugs found:**
  - Track matching failed on real data: ISO 639-2/B vs /T language codes, and "forced only" tracks MakeMKV didn't write. Unticked tracks were therefore not removed.
  - **Fix:** normalise language codes, match forced-only tracks only against forced tracks, and always check tracks after a rip.
- **Also:** the profile cache path was doubled (`~/.cache/video-renamer/video-renamer`).

## 11. Honest selection feedback, rip statistics, overall progress
- **Confirmed** in makemkvcon: "Forced subtitles track #… turned out to be empty and was removed". The rip report now explains this.
- **Built:**
  - Live stats: elapsed time, time left, MB/s, ×BD speed, data written, and a separate "preparing" phase.
  - A per-title report with preparing/copying times and speed.
  - The overall progress bar covers all titles, weighted by size. The current-title bar follows the bytes written, so it never goes backwards.

## 12. TheDiscDB matching
- **API:** the public GraphQL endpoint `https://thediscdb.com/graphql`, with `mediaItems` → releases → discs → titles (duration, size, sourceFile, itemType, item.title).
- **Built:** a "Match with TheDiscDB…" window. Matching is by length (±2 s) against named titles, the discs are ranked, and Jellyfin-style names are suggested. The user approves and can edit everything.
- **Bug fixed:** undo of "rename files, then the folder" failed. Undo entries are now multi-step.
- **Limitation:** TheDiscDB often only names the main movie on UHD discs.

## 13. Delete and Settings
- **Delete:** to the Trash by default, with an optional confirmation popup ("Don't ask again").
- **Settings window, four tabs:**
  - General: deleting.
  - Folders: start folder, rip folder.
  - MakeMKV: program paths, cache, minimum length, languages, which tracks start ticked.
  - Export: NAS destinations, delete after export.
- **Behaviour change:** the Rip window no longer overwrites the default settings.
- **Speed-up:** Settings used to open in about 12 s because of the makemkvcon version check. The check now uses `--noscan` and runs in the background.

## 14. Several preferred languages
- **Settings → MakeMKV → Preferred languages** accepts a list such as `eng, jpn`. Invalid codes are flagged, and B/T code variants match.

## 15. Git, GitHub, desktop entry, license, release
- **Repository:** a git repo was created; the drive serial was scrubbed from the fixture history before the first push. It's public on GitHub as `cooldead/video-renamer`.
- **Desktop entry:** installed to `~/.local/share/applications` and `~/Desktop`. The `inode/directory` MimeType was removed so the app never takes over opening folders.
- **License and release:** MIT license added, then the v1.0.0 release.

## 16. Exporting several folders (2026-09-26)
- **Request:** with several movie folders selected, Export should open a prompt for each, but recommend moving one movie folder at a time.
- **Built:** one Export window per selected folder, offset from each other, with a hint while several are open. Starting an export while another copies asks "Wait until it finishes (recommended)" or "Start now anyway". Waiting exports queue and start automatically, in order. There's also "Leave queue".
- **Tested** with three folders and throttled rsync: never more than one copy at a time, finished in order, all verified.
- **Along the way:** clarified with the user which button was meant. "Move" meant **Export to NAS**, the NAS copy, not Move to Folder.

## 17. Release v1.1.0 (2026-09-26)
- Released as v1.1.0: the multi-folder export queue, plus `CLAUDE.md` and this log. v1.0.0 was the first release (2026-09-25).

## 18. MakeMKV registration key (2026-09-27)
- **Question:** how does a new user's copy reach MakeMKV, and should the app take their key or tell them to open MakeMKV first?
- **Answer:** the app only runs `makemkvcon`, which reads the key from MakeMKV's own settings (the same ones the GUI writes). So the key is set up once, and the GUI never has to be running.
- **Built:**
  - The Rip window spots MakeMKV's key and evaluation messages (the text was taken from `libmakemkv`). When a scan or rip fails after one, it explains the problem, says where to enter a key and links the free beta key.
  - **Settings → MakeMKV → Registration key** runs `makemkvcon reg <key>`, so MakeMKV checks the key and stores it itself. The app never keeps a copy.
  - The README says a key (or the trial) is needed for Blu-rays.
- **Tested** offscreen with a throwaway `HOME`: an invalid key shows "Key not found or invalid", and the expiry message brings up the explanation. A valid key was not tried.

## 19. Release v1.2.0 (2026-09-27)
- Released as v1.2.0: MakeMKV key errors explained, and a key can be registered from Settings.

## Open items / ideas not done
- **Whether the all-tracks profile rips every language** was confirmed on real rips (29/27 tracks). Removing tracks after a real multi-language rip has only been tested on short titles.
- **The first two real rips (Indiana Jones, One Battle After Another)** still contain all their tracks; they were made before the matching fix.
- **Possible future work:**
  - Store disc info (playlists) next to rips, for exact TheDiscDB matching by playlist instead of length.
  - A package or installer instead of `run.sh`.
  - Keep the GUI smoke tests in the repo.
