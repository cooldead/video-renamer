# Video Renamer

A small native Qt 6 desktop app for Linux: open a folder of videos, preview each one inside the app, and rename it while you watch. You can also rename many files at once with a pattern.

Playback uses libmpv, the engine behind mpv, drawn into the window with OpenGL. That means it runs natively on Wayland and X11, uses GPU decoding, and handles Dolby TrueHD/Atmos 7.1, DTS-HD, 4K HEVC 10-bit and HDR.

## Requirements (CachyOS / Arch)

```sh
sudo pacman -S pyside6 mpv python-mpv
```

Ripping discs needs MakeMKV (`makemkv`, which provides `makemkvcon`). Blu-rays also need a MakeMKV key, or MakeMKV's trial period. While MakeMKV is in beta, a free key is posted [on its forum](https://forum.makemkv.com/forum/viewtopic.php?t=1053). Enter it in **Settings → MakeMKV → Registration key**, or in MakeMKV itself (Help → Register). Either way, MakeMKV stores the key, so you only enter it once, not every session, and MakeMKV doesn't need to be open. DVDs work without a key. Removing unticked tracks after a rip needs `mkvtoolnix-cli`. Exporting uses `rsync`.

```sh
sudo pacman -S mkvtoolnix-cli rsync
```

## Install

Choose one. Both add **Video Renamer** to the application menu. For ripping and exporting, install MakeMKV, `mkvtoolnix-cli` and `rsync` on the computer as described above, whichever way you install the app.

**pacman package (Arch, CachyOS):**

```sh
git clone https://github.com/cooldead/video-renamer.git
cd video-renamer/packaging/arch
makepkg -si
```

**Flatpak:**

```sh
flatpak install --user flathub org.flatpak.Builder
git clone https://github.com/cooldead/video-renamer.git
cd video-renamer
flatpak run org.flatpak.Builder --user --install --install-deps-from=flathub --force-clean \
    ~/.cache/video-renamer-flatpak packaging/flatpak/io.github.cooldead.VideoRenamer.yml
```

The first Flatpak build downloads the KDE runtime and compiles mpv, so it takes a while. The Flatpak runs MakeMKV, mkvmerge, rsync and ffprobe **from the computer**, not from inside the sandbox. It uses MakeMKV's own Flatpak (`com.makemkv.MakeMKV`) if `makemkvcon` isn't installed, and MKVToolNix's Flatpak likewise. Your MakeMKV key and settings are shared with MakeMKV itself. For this, the Flatpak has permission to start programs outside its sandbox, and to read and write all your files (video folders, NAS mounts).

**From the source folder, without installing:** `./run.sh [folder]`.

## Disc → rename → NAS workflow

1. **Rip Disc… (Ctrl+D).**
   - The drive is found and the disc scanned automatically.
   - As in MakeMKV, you get every title with its length, chapters and size. Expand a title to tick its video, audio and subtitle tracks.
   - Tracks start ticked the MakeMKV way: video, plus audio and subtitles in your preferred languages (Settings → MakeMKV, e.g. `eng, jpn`) or with no language.
   - "Min. title length" hides short titles (MakeMKV's minimum title length).
   - Rips go to `~/Videos/<disc title>` by default.
   - The window runs in the background with progress. When it's done, the folder opens in the renamer.
   - How tracks are chosen: MakeMKV can only pick *titles* on the command line. So the app rips every track (using its own MakeMKV profile; your MakeMKV settings aren't changed), then removes the unticked tracks with `mkvmerge`. That's a lossless re-pack with no re-encoding. If a ticked track can't be found in the ripped file, all tracks are kept and you get a warning, so nothing is lost.
   - **While ripping**, a stats line shows elapsed time, time left, the current speed in MB/s and as a Blu-ray/DVD speed multiple (e.g. `20.4 MB/s (4.5×)`), and how much has been written. At first it says "preparing", while MakeMKV analyses the disc (BD+ processing and so on) before copying.
   - **When it's done**, a report lists each title:
     - the file and its size;
     - the time spent preparing and copying, and the copy speed;
     - how many unticked tracks were removed and how many tracks the file has;
     - any problems.
   - **"Forced only" subtitle tracks** are written only if the disc really has forced subtitles. MakeMKV removes them as empty otherwise, and the report says so.
   - **Several drives:** press Rip Disc… again to open another window. Each window uses its own drive, so discs in different drives rip at the same time. A window never takes a drive another window is using. If two discs have the same name, the second is saved to `Name (2)`. When a rip finishes while you're watching or typing a name, the tree just refreshes and the status bar tells you, so you stay where you are.
2. **Match with TheDiscDB… (Ctrl+T, or right-click a folder)** is optional and never automatic.
   - **Search:** it reads the length of every video in the folder and searches [TheDiscDB](https://thediscdb.com) using the folder name, which you can edit.
   - **Disc choice:** the discs found are ranked by how many of your files match a named title by length (±2 s). You can pick another disc.
   - **Suggestions:** the table shows each file, its length, its match, and a suggested name in the library's layout:
     - main movie → `Title (Year)`
     - extras → `extras/<name>`
     - episodes → `Season 01/Show S01E02 - Name`
     - the folder → `Title (Year)`
   - **Approval:** tick what to rename and edit any name. Clashes show in red. Nothing changes until you press **Rename**, and one Ctrl+Z undoes all of it.
   - **Limits:** TheDiscDB only names what contributors have catalogued. Many UHD discs list just the main movie, so extras often stay unmatched.
3. **Rename** the folder and files as usual, for example `Raiders of the Lost Ark (1981)/Raiders of the Lost Ark (1981).mkv`, with extras in a subfolder.
4. **Export to NAS… (Ctrl+E, or right-click a folder).**
   - Copies the selected folder into `/mnt/media/Movies`, `/mnt/media/TVshows` or any folder you choose. Recent destinations are remembered.
   - Uses `rsync`, with progress, speed and time left. A stopped export picks up where it left off.
   - Afterwards every file's size is checked on the NAS.
   - With "Delete the local folder after the copy has been checked" ticked (off by default), the local folder is removed only when that check passes.
   - **Several folders:** select several movie folders and press Export to open one window per folder. Copying one folder at a time is recommended, because parallel copies share the network. If you start an export while another is copying, it asks **Wait until it finishes (recommended)** or **Start now anyway**. Waiting exports start automatically, in order. **Leave queue** takes one out of the line.

## Run

```sh
./run.sh                 # reopens the last folder
./run.sh ~/Videos        # open a folder
./run.sh ~/Videos/a.mkv  # open a file's folder with that file selected
```

To add it to the KDE application menu:

```sh
cp video-renamer.desktop ~/.local/share/applications/
```

(The `Exec=` line points at this project folder; edit it if you move the project.)

## Using it

| Action | Key |
|---|---|
| Open folder (or drag a folder/file onto the window) | Ctrl+O |
| Refresh (normally automatic) | F5 |
| Play / pause (files open paused; nothing autoplays) | Space |
| Seek −5 s / +5 s | ← / → |
| Previous / next chapter | Ctrl+← / Ctrl+→ |
| Jump to the name box | Ctrl+L |
| Rename (in the name box) | Enter |
| Rename and go to the next file | Ctrl+Enter |
| Rename in the list (files and folders) | F2, double-click a file, or right-click → Rename |
| Batch rename selected files | Ctrl+B |
| New folder (inside the open folder) | Ctrl+Shift+N |
| Move selected files to a folder | Ctrl+M, or drag them onto a folder |
| Undo last rename | Ctrl+Z |

The file extension is always kept; you only edit the part before it.

**Folders** are renamed the same way: select one and type in the name box, press F2, or right-click → Rename. A video playing from inside the folder keeps playing. A `/` moves the folder into a (new) subfolder, and moving a folder into itself is refused.

**Drag and drop:** drag files or folders in the tree to move them on disk. Drop onto a folder to move into it, onto a file to move next to it, or onto empty space to move to the top of the open folder. Several selected items move together. Hovering over a folder while dragging opens it, and a folder can't be dropped into itself. Undo moves everything back. Dragging a folder or video in from outside the app (e.g. from Dolphin) opens it instead.

**Right-click menu** in the tree: Rename, Batch Rename…, Move to Folder…, New Folder…, Undo.

**Folder tree:** the list shows the open folder as a tree, with folders first, including empty ones. Only video files are listed, and hidden folders are skipped. The tree refreshes by itself after renames, moves and new folders, and also when files change on disk outside the app (for example in Dolphin). A refresh keeps your expanded folders, selection and scroll position. The filter box shows matching files along with the folders they're in. **Rename & Next** walks through files in tree order, including files in collapsed folders.

**Subfolders:** a `/` in a new name puts the file in a subfolder, creating it if needed. For example, `The Drama (2026)/The Drama (2026)` moves the file into a `The Drama (2026)` folder. This works in the name box, in the list (F2) and in batch patterns (`Season 1/S01E{n}`). **Move to Folder…** uses the KDE folder picker, which also has a Create Folder button. Undo moves files back and removes any folders the rename created, if they are empty. Folders you made with New Folder are always kept.

**Scrubbing:** dragging the seek bar makes quick keyframe jumps, at most one at a time, then one exact seek where you let go. Clicking the bar works the same way. Seeks stop just short of the end, so the player never gets stuck there.

**Player controls:** the seek bar runs the full width of the video, with chapter starts marked on it. Below it are play/pause, the time, a video info summary (HDR10/HLG/SDR, bit depth, GPU or CPU decoding; hover it for details), mute and volume. Under those are the Chapter, Video, Audio and Subtitles pickers.

**Video streams:** every video stream in the file is listed with codec, resolution, frame rate and Dolby Vision profile, e.g. `1: HEVC · 3840×2160 · 23.976 fps · Dolby Vision P7`. The picker is enabled when a file has more than one.

**Subtitles:** "Off" plus every subtitle track, with language, format (PGS, SRT, ASS, VobSub…), title and a "forced" marker.

**Chapters:** if the file has chapters, a chapter list appears next to the timer and every chapter start is marked on the seek bar. Pick a chapter from the list or use Ctrl+← / Ctrl+→.

**Audio tracks:** files with more than one audio stream get a track picker next to the volume slider (language, codec, channel layout and title, e.g. `1: English · TRUEHD 7.1 · Surround 7.1`). Your chosen video, audio and subtitle tracks are kept when you rename the playing file or the folder it's in.

**Batch rename tokens:** `{n}` counter (start value and minimum digits are set in the dialog), `{n:03}` counter with a fixed width, `{name}` current name, `{date}` file modified date (YYYY-MM-DD), `{parent}` folder name. There's also an optional find/replace step (plain text or regex, where `\1` refers to groups). The preview shows every new name; clashes and invalid names show in red, and Rename stays disabled until they're fixed.

**Safety:** a batch is checked before anything is touched, then applied in two steps through temporary names, so swaps like `a→b, b→a` work. If any step fails, every file is put back. Moving between drives/partitions isn't supported; the move is refused and nothing changes.

## Deleting

Press **Delete**, use **Edit → Delete…**, or right-click → **Delete…** to delete the selected files or folders; a folder is deleted with everything in it. By default they go to the **Trash**, so you can restore them. A popup asks first and shows what will be deleted and how much space it frees. Tick "Don't ask again" to stop the popup. Both the popup and Trash-or-permanent can be changed in Settings.

## Settings

**Settings → Configure Video Renamer…** (Ctrl+,):
- **General:** ask before deleting; Trash or permanent delete.
- **Folders:**
  - At start (input): reopen the last folder, or always open a folder you choose.
  - Rip discs into: the default folder for rips. Each disc gets its own subfolder.
- **MakeMKV:**
  - The locations of `makemkvcon` and `mkvmerge`. "Check programs" shows their versions.
  - Minimum title length and read cache size.
  - Preferred languages: 3-letter codes, e.g. `eng, jpn`. Audio and subtitle tracks in any of them start ticked. Leave it empty to use MakeMKV's own setting.
  - Which tracks start ticked: like MakeMKV, all tracks, or video only.
  - Registration key: paste a new or renewed key and press Register. MakeMKV checks it and stores it; this app doesn't keep a copy.
- **Export:**
  - NAS destinations (output). The first one is the default, and the one you export to moves to the top.
  - Whether to delete the local folder after a checked copy.

## Tests

```sh
python3 -m unittest
```

## License

MIT, see [LICENSE](LICENSE). Disc data comes from [TheDiscDB](https://thediscdb.com); MakeMKV, mpv and MKVToolNix are separate programs under their own licenses.
