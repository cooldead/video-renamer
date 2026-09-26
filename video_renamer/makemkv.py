"""MakeMKV (makemkvcon) integration: reading its robot-mode output, building
commands, and working out which tracks of a ripped file to keep.

Robot mode ("makemkvcon -r") prints one record per line, e.g.
    TINFO:0,9,0,"1:55:18"        title 0, attribute 9 (duration)
    SINFO:0,1,5,0,"A_TRUEHD"     title 0, track 1, attribute 5 (codec id)
    PRGV:1234,5678,65536         progress: current, total, maximum
See https://www.makemkv.com/developers/usage.txt. Nothing here touches Qt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MAKEMKVCON = "makemkvcon"
MKVMERGE = "mkvmerge"

# Attribute ids (from MakeMKV's apdefs.h) used below.
A_TYPE, A_NAME, A_LANG_CODE, A_LANG_NAME, A_CODEC_ID, A_CODEC_SHORT, A_CODEC_LONG = 1, 2, 3, 4, 5, 6, 7
A_CHAPTERS, A_DURATION, A_SIZE, A_SIZE_BYTES, A_BITRATE, A_CHANNELS = 8, 9, 10, 11, 13, 14
A_SOURCE_FILE, A_SAMPLE_RATE, A_VIDEO_SIZE, A_ASPECT, A_FRAME_RATE = 16, 17, 19, 20, 21
A_OUTPUT_FILE, A_DESCRIPTION, A_VOLUME_NAME, A_FLAGS, A_CHANNEL_LAYOUT = 27, 30, 32, 38, 40

KINDS = {"video": "video", "audio": "audio", "subtitles": "subtitle"}

# Rip every track; the app keeps only the ones ticked afterwards (MakeMKV
# can pick titles on the command line, but not individual tracks).
ALL_TRACKS_PROFILE = """<?xml version="1.0" encoding="utf-8"?>
<profile>
    <name lang="eng">Video Renamer - all tracks</name>
    <mkvSettings ignoreForcedSubtitlesFlag="true" useISO639Type2T="false"
        setFirstAudioTrackAsDefault="true" setFirstSubtitleTrackAsDefault="true"
        setFirstForcedSubtitleTrackAsDefault="true" insertFirstChapter00IfMissing="true"/>
    <outputSettings name="copy" outputFormat="directCopy">
        <description lang="eng">Copy track as is</description>
    </outputSettings>
    <trackSettings input="default">
        <output outputSettingsName="copy" defaultSelection="+sel:all"></output>
    </trackSettings>
</profile>
"""


def split_fields(text: str) -> list[str]:
    """Split a robot-mode record body: comma separated, strings in double
    quotes (which may contain commas and backslash-escaped quotes)."""
    fields, current, quoted, escaped, was_quoted = [], [], False, False, False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
            was_quoted = True
        elif char == "," and not quoted:
            fields.append("".join(current))
            current, was_quoted = [], False
        else:
            current.append(char)
    if current or was_quoted or text.endswith(","):
        fields.append("".join(current))
    return fields


def parse_line(line: str) -> tuple[str, list[str]] | None:
    """("TINFO", ["0", "9", "0", "1:55:18"]) for a record, None otherwise."""
    key, sep, rest = line.rstrip("\r\n").partition(":")
    if not sep or not key.isupper():
        return None
    return key, split_fields(rest)


def duration_seconds(text: str) -> int:
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part or 0)
    return seconds


@dataclass
class Drive:
    index: int
    state: int
    model: str
    disc_label: str
    device: str

    @property
    def has_disc(self) -> bool:
        return bool(self.disc_label)

    def label(self) -> str:
        disc = self.disc_label or "no disc"
        return f"{self.device} · {self.model} · {disc}"


@dataclass
class Track:
    id: int
    attributes: dict[int, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return KINDS.get(self.attributes.get(A_TYPE, "").lower(), "other")

    @property
    def codec_id(self) -> str:
        return self.attributes.get(A_CODEC_ID, "")

    @property
    def language(self) -> str:
        return self.attributes.get(A_LANG_CODE, "")

    @property
    def is_default(self) -> bool:
        return "d" in self.attributes.get(A_FLAGS, "")

    @property
    def is_forced_only(self) -> bool:
        return "forced only" in self.attributes.get(A_DESCRIPTION, "").lower()

    def label(self) -> str:
        """e.g. "TrueHD Atmos · English · 7.1 · Surround 7.1 · default"."""
        a = self.attributes
        if self.kind == "video":
            parts = [a.get(A_CODEC_LONG, ""), a.get(A_VIDEO_SIZE, ""), a.get(A_FRAME_RATE, "").split(" ")[0]]
            if a.get(A_FRAME_RATE):
                parts[-1] += " fps"
        elif self.kind == "audio":
            parts = [a.get(A_CODEC_LONG, ""), a.get(A_LANG_NAME, ""), a.get(A_CHANNEL_LAYOUT, ""), a.get(A_NAME, "")]
        else:
            parts = [a.get(A_CODEC_SHORT, "") or a.get(A_CODEC_LONG, ""), a.get(A_LANG_NAME, "")]
            if self.is_forced_only:
                parts.append("forced only")
        if self.is_default:
            parts.append("default")
        return " · ".join(p for p in parts if p) or a.get(A_DESCRIPTION, f"Track {self.id}")


@dataclass
class Title:
    id: int
    attributes: dict[int, str] = field(default_factory=dict)
    tracks: list[Track] = field(default_factory=list)

    @property
    def duration(self) -> str:
        return self.attributes.get(A_DURATION, "")

    @property
    def seconds(self) -> int:
        return duration_seconds(self.duration) if self.duration else 0

    @property
    def chapters(self) -> int:
        return int(self.attributes.get(A_CHAPTERS, "0") or 0)

    @property
    def size_bytes(self) -> int:
        return int(self.attributes.get(A_SIZE_BYTES, "0") or 0)

    @property
    def size_text(self) -> str:
        return self.attributes.get(A_SIZE, "")

    @property
    def source(self) -> str:
        return self.attributes.get(A_SOURCE_FILE, "")

    @property
    def output_file(self) -> str:
        return self.attributes.get(A_OUTPUT_FILE, "")


@dataclass
class Disc:
    attributes: dict[int, str] = field(default_factory=dict)
    titles: list[Title] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.attributes.get(A_NAME, "") or self.attributes.get(A_VOLUME_NAME, "") or "Disc"

    @property
    def kind(self) -> str:
        return self.attributes.get(A_TYPE, "")


def parse_drives(lines) -> list[Drive]:
    """Drives from "DRV" records; slots without a device are left out."""
    drives = []
    for line in lines:
        record = parse_line(line)
        if record and record[0] == "DRV" and len(record[1]) >= 7 and record[1][6]:
            f = record[1]
            drives.append(Drive(int(f[0]), int(f[1]), f[4], f[5], f[6]))
    return drives


def parse_info(lines) -> Disc:
    disc = Disc()
    titles: dict[int, Title] = {}
    for line in lines:
        record = parse_line(line)
        if record is None:
            continue
        key, f = record
        try:
            if key == "CINFO" and len(f) >= 3:
                disc.attributes[int(f[0])] = f[2]
            elif key == "TINFO" and len(f) >= 4:
                title = titles.setdefault(int(f[0]), Title(int(f[0])))
                title.attributes[int(f[1])] = f[3]
            elif key == "SINFO" and len(f) >= 5:
                title = titles.setdefault(int(f[0]), Title(int(f[0])))
                track_id = int(f[1])
                while len(title.tracks) <= track_id:
                    title.tracks.append(Track(len(title.tracks)))
                title.tracks[track_id].attributes[int(f[2])] = f[4]
        except ValueError:
            continue
    disc.titles = [titles[i] for i in sorted(titles)]
    return disc


@dataclass
class Progress:
    """What a robot-mode line says about progress, if anything."""
    fraction: float | None = None   # PRGV: overall position of the current operation
    task: str | None = None          # PRGT: e.g. "Saving to MKV file"
    step: str | None = None          # PRGC: e.g. "Analyzing seamless segments"
    message: str | None = None       # MSG: log text


def parse_progress(line: str) -> Progress | None:
    record = parse_line(line)
    if record is None:
        return None
    key, f = record
    if key == "PRGV" and len(f) >= 3 and f[2].isdigit() and int(f[2]):
        return Progress(fraction=min(1.0, int(f[1]) / int(f[2])))
    if key == "PRGT" and len(f) >= 3:
        return Progress(task=f[2])
    if key == "PRGC" and len(f) >= 3:
        return Progress(step=f[2])
    if key == "MSG" and len(f) >= 4:
        return Progress(message=f[3])
    return None


def preferred_language(settings_file: Path = Path.home() / ".MakeMKV" / "settings.conf") -> str:
    """MakeMKV's preferred language (app_PreferredLanguage), "eng" if unset."""
    try:
        match = re.search(r'^app_PreferredLanguage\s*=\s*"([a-z]{3})"', settings_file.read_text(), re.MULTILINE)
    except OSError:
        return "eng"
    return match.group(1) if match else "eng"


def parse_languages(text: str) -> tuple[list[str], list[str]]:
    """Language codes from text like "eng, jpn" or "eng jpn": (valid 3-letter
    codes, lower case and without duplicates, in order; anything else)."""
    valid, invalid = [], []
    for part in re.split(r"[\s,;]+", text.strip()):
        if not part:
            continue
        code = part.lower()
        if re.fullmatch(r"[a-z]{3}", code):
            if code not in valid:
                valid.append(code)
        else:
            invalid.append(part)
    return valid, invalid


def default_selected(track: Track, favorites: str | list[str], mode: str = "makemkv") -> bool:
    """Whether a track starts ticked. mode "makemkv" works like MakeMKV's
    default selection: all video, plus audio/subtitles in one of the preferred
    languages or without a language. "all" ticks everything, "video" only video."""
    if track.kind == "video":
        return True
    if mode == "all":
        return True
    if mode == "video":
        return False
    if track.kind in ("audio", "subtitle"):
        if isinstance(favorites, str):
            favorites = [favorites]
        wanted = {normalize_language(code) for code in favorites}
        return not track.language or normalize_language(track.language) in wanted
    return False


def safe_folder_name(name: str) -> str:
    name = re.sub(r'[/\x00]', " ", name).strip().strip(".")
    return re.sub(r"\s+", " ", name) or "Disc"


# --- commands ---------------------------------------------------------

def drives_args() -> list[str]:
    return ["-r", "--cache=1", "info", "disc:9999"]


def info_args(drive: int, min_length: int) -> list[str]:
    return ["-r", "--progress=-same", "--cache=1", f"--minlength={min_length}", "info", f"disc:{drive}"]


def rip_args(drive: int, title: int, out_dir: Path, min_length: int, profile: Path, cache_mb: int = 0) -> list[str]:
    # Title ids depend on --minlength, so it must match the scan.
    cache = [f"--cache={cache_mb}"] if cache_mb > 0 else []
    return ["-r", "--progress=-same", *cache, f"--minlength={min_length}", f"--profile={profile}",
            "mkv", f"disc:{drive}", str(title), str(out_dir)]


def write_profile(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "video-renamer-alltracks.mmcp.xml"
    if not path.exists() or path.read_text() != ALL_TRACKS_PROFILE:
        path.write_text(ALL_TRACKS_PROFILE)
    return path


# --- keeping only the ticked tracks -------------------------------------

MKVMERGE_KINDS = {"video": "video", "audio": "audio", "subtitles": "subtitle"}


# MakeMKV reports ISO 639-2/T codes ("fra") but writes ISO 639-2/B codes
# ("fre") into the file for these languages.
_BIBLIOGRAPHIC_TO_TERMINOLOGY = {
    "alb": "sqi", "arm": "hye", "baq": "eus", "bur": "mya", "chi": "zho", "cze": "ces", "dut": "nld",
    "fre": "fra", "geo": "kat", "ger": "deu", "gre": "ell", "ice": "isl", "mac": "mkd", "mao": "mri",
    "may": "msa", "per": "fas", "rum": "ron", "slo": "slk", "tib": "bod", "wel": "cym",
}


def normalize_language(code: str) -> str:
    code = (code or "").lower()
    return _BIBLIOGRAPHIC_TO_TERMINOLOGY.get(code, code)


def match_tracks(disc_tracks: list[Track], file_tracks: list[dict]) -> dict[int, int]:
    """Map MakeMKV track ids to track ids in the ripped file ("mkvmerge -J"
    "tracks" entries). Matched in order by type, codec, language and the
    forced flag, so tracks MakeMKV skipped or added don't shift the others.

    "Forced only" subtitle tracks are only written when the disc really has
    forced subtitles; they match only tracks flagged forced in the file."""
    mapping: dict[int, int] = {}
    position = 0
    for track in disc_tracks:
        for index in range(position, len(file_tracks)):
            candidate = file_tracks[index]
            properties = candidate.get("properties", {})
            if MKVMERGE_KINDS.get(candidate.get("type")) != track.kind:
                continue
            if properties.get("codec_id") and track.codec_id and properties["codec_id"] != track.codec_id:
                continue
            language = normalize_language(properties.get("language", ""))
            if track.language and language not in ("", "und", normalize_language(track.language)):
                continue
            if track.kind == "subtitle" and bool(properties.get("forced_track")) != track.is_forced_only:
                continue
            mapping[track.id] = candidate["id"]
            position = index + 1
            break
    return mapping


def mkvmerge_keep_args(source: Path, target: Path, keep_ids: list[int], file_tracks: list[dict]) -> list[str]:
    """mkvmerge arguments writing target with only keep_ids from source."""
    args = ["-o", str(target)]
    for kind, (flag, none_flag) in {"video": ("--video-tracks", "--no-video"),
                                     "audio": ("--audio-tracks", "--no-audio"),
                                     "subtitles": ("--subtitle-tracks", "--no-subtitles")}.items():
        present = [t["id"] for t in file_tracks if t.get("type") == kind]
        kept = [i for i in present if i in keep_ids]
        if not present or kept == present:
            continue
        args += [flag, ",".join(map(str, kept))] if kept else [none_flag]
    args.append(str(source))
    return args
