"""Video preview widget: libmpv rendered into a QOpenGLWidget (native on Wayland and X11)."""

from __future__ import annotations

import locale
import re
import time
from pathlib import Path

import mpv
from PySide6.QtCore import QLocale, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QOpenGLContext, QPainter, QPen
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QSlider, QStyle, QStyleOptionSlider, QToolButton,
    QVBoxLayout, QWidget,
)

SEEK_STEP_MS = 5000



def format_time(ms: int) -> str:
    seconds = max(ms, 0) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def language_name(code: str | None) -> str | None:
    if not code or code in ("und", "zxx", "mis", "mul"):
        return None
    language = QLocale.codeToLanguage(code, QLocale.LanguageCodeType.AnyLanguageCode)
    if language in (QLocale.Language.AnyLanguage, QLocale.Language.C):
        return code
    return QLocale.languageToString(language)


CHANNEL_NAMES = {1: "mono", 2: "stereo", 6: "5.1", 7: "6.1", 8: "7.1"}


def audio_track_label(number: int, track: dict) -> str:
    """Label like "2: English · TRUEHD 7.1 · Surround 7.1"."""
    parts = []
    language = language_name(track.get("lang"))
    if language:
        parts.append(language)
    codec = (track.get("codec") or "").upper()
    channels = CHANNEL_NAMES.get(track.get("demux-channel-count"), "")
    if codec or channels:
        parts.append(" ".join(p for p in (codec, channels) if p))
    if track.get("title"):
        parts.append(str(track["title"]))
    return f"{number}: " + (" · ".join(parts) if parts else f"Track {number}")


VIDEO_CODECS = {"hevc": "HEVC", "h264": "H.264", "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
                "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4", "vc1": "VC-1"}
SUBTITLE_CODECS = {"hdmv_pgs_subtitle": "PGS", "subrip": "SRT", "ass": "ASS", "ssa": "SSA",
                   "dvd_subtitle": "VobSub", "dvb_subtitle": "DVB", "mov_text": "TX3G", "webvtt": "WebVTT"}


def video_track_label(number: int, track: dict) -> str:
    """Label like "1: HEVC · 3840×2160 · 23.976 fps · Dolby Vision P7"."""
    parts = []
    codec = track.get("codec") or ""
    if codec:
        parts.append(VIDEO_CODECS.get(codec, codec.upper()))
    if track.get("demux-w") and track.get("demux-h"):
        parts.append(f"{track['demux-w']}×{track['demux-h']}")
    if track.get("demux-fps"):
        parts.append(f"{track['demux-fps']:.3f}".rstrip("0").rstrip(".") + " fps")
    if track.get("dolby-vision-profile"):
        parts.append(f"Dolby Vision P{track['dolby-vision-profile']}")
    if track.get("title"):
        parts.append(str(track["title"]))
    return f"{number}: " + (" · ".join(parts) if parts else f"Track {number}")


def subtitle_track_label(number: int, track: dict) -> str:
    """Label like "2: English · PGS · SDH · forced"."""
    parts = []
    language = language_name(track.get("lang"))
    if language:
        parts.append(language)
    codec = track.get("codec") or ""
    if codec:
        parts.append(SUBTITLE_CODECS.get(codec, codec.upper()))
    if track.get("title"):
        parts.append(str(track["title"]))
    if track.get("forced"):
        parts.append("forced")
    if track.get("external"):
        parts.append("external file")
    return f"{number}: " + (" · ".join(parts) if parts else f"Track {number}")


def bit_depth(pixelformat: str) -> int:
    """Bits per colour sample from an FFmpeg/mpv pixel format name:
    nv12/yuv420p -> 8, p010/yuv420p10le -> 10, p012/yuv444p12 -> 12."""
    pixelformat = pixelformat.lower()
    match = re.fullmatch(r"p0(\d\d)", pixelformat) or re.search(r"p(\d\d)(?:le|be)?$", pixelformat)
    return int(match.group(1)) if match else 8


def video_info(params: dict, hwdec) -> tuple[str, str]:
    """Short summary and a detailed tooltip of the video actually being shown."""
    if not params:
        return "", ""
    gamma = params.get("gamma")
    dynamic_range = {"pq": "HDR10 (PQ)", "hlg": "HLG"}.get(gamma, "SDR")
    pixelformat = str(params.get("hw-pixelformat") or params.get("pixelformat") or "")
    bits = f"{bit_depth(pixelformat)}-bit"
    decoder = f"GPU ({hwdec})" if hwdec and hwdec != "no" else "CPU decoding"
    summary = " · ".join((dynamic_range, bits, decoder))
    details = [f"{key}: {params[key]}" for key in ("w", "h", "pixelformat", "hw-pixelformat", "primaries",
                                                    "gamma", "colormatrix", "colorlevels", "sig-peak")
               if params.get(key) not in (None, "")]
    return summary, "\n".join(details)


def chapter_label(number: int, chapter: dict) -> str:
    title = chapter.get("title") or f"Chapter {number}"
    return f"{number}. {title}  ({format_time(int(chapter.get('time', 0) * 1000))})"


class MpvVideoWidget(QOpenGLWidget):
    """Draws mpv's video output. mpv calls back from its own thread, so
    repaint requests are passed through a queued signal."""

    _frameReady = Signal()
    ready = Signal()  # the OpenGL render context exists; mpv can output video

    def __init__(self, player: mpv.MPV, parent=None):
        super().__init__(parent)
        self._player = player
        self._render = None
        self._frameReady.connect(self.update, Qt.ConnectionType.QueuedConnection)
        # Keep a reference: mpv holds the raw function pointer.
        self._get_proc_address = mpv.MpvGlGetProcAddressFn(self._proc_address)

    @staticmethod
    def _proc_address(_ctx, name: bytes) -> int:
        context = QOpenGLContext.currentContext()
        address = context.getProcAddress(name) if context else None
        return int(address) if address else 0

    def initializeGL(self) -> None:
        self._render = mpv.MpvRenderContext(
            self._player, "opengl", opengl_init_params={"get_proc_address": self._get_proc_address},
        )
        self._render.update_cb = self._frameReady.emit
        self.context().aboutToBeDestroyed.connect(self.release)
        self.ready.emit()

    def is_ready(self) -> bool:
        return self._render is not None

    def paintGL(self) -> None:
        if self._render is None:
            return
        ratio = self.devicePixelRatioF()
        self._render.render(
            flip_y=True,
            opengl_fbo={"w": int(self.width() * ratio), "h": int(self.height() * ratio), "fbo": self.defaultFramebufferObject()},
        )

    def release(self) -> None:
        if self._render is None:
            return
        self.makeCurrent()
        self._render.update_cb = None
        self._render.free()
        self._render = None
        self.doneCurrent()


class ChapterSlider(QSlider):
    """Seek slider with a tick mark at every chapter start."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.chapter_times: list[int] = []  # ms

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if len(self.chapter_times) < 2 or self.maximum() <= 0:
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self)
        left = groove.left() + handle.width() / 2
        span = groove.width() - handle.width()
        color = QColor(self.palette().windowText().color())
        color.setAlpha(170)
        painter = QPainter(self)
        painter.setPen(QPen(color, 1))
        y = groove.center().y()
        for time in self.chapter_times[1:]:  # the first chapter starts at 0
            x = left + span * time / self.maximum()
            painter.drawLine(QPointF(x, y - 5), QPointF(x, y + 5))


class PlayerWidget(QWidget):
    errorOccurred = Signal(str)

    # mpv reports property changes on its own event thread; these signals
    # carry them over to the GUI thread.
    _positionChanged = Signal(object)
    _durationChanged = Signal(object)
    _pauseChanged = Signal(object)
    _tracksChanged = Signal(object)
    _trackSelected = Signal(str, object)  # (kind, mpv track id / False)
    _videoParamsChanged = Signal(object)
    _chaptersChanged = Signal(object)
    _seekFinished = Signal()

    # kind -> (mpv property, name for "nothing")
    TRACK_KINDS = {"video": ("vid", "No video"), "audio": ("aid", "No audio"), "sub": ("sid", "No subtitles")}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path: Path | None = None
        self._alive = True
        # mpv track ids per combo row; for subtitles row 0 is "Off" (None).
        self._track_ids: dict[str, list[int | None]] = {kind: [] for kind in self.TRACK_KINDS}

        # libmpv refuses to run (and crashes) unless LC_NUMERIC is "C". python-mpv
        # sets that on import, but QApplication switches back to the desktop
        # locale when it starts, so set it again right before creating mpv.
        locale.setlocale(locale.LC_NUMERIC, "C")
        self.mpv = mpv.MPV(
            vo="libmpv",
            hwdec="auto-safe",
            # The default intermediate buffer format (rgba16f) fails to be
            # created in Qt's GL context (GL_INVALID_ENUM); subtitles are blended
            # through that buffer, so turning them on blanked HDR video.
            # rgba16 keeps 16-bit precision for tone mapping and works.
            fbo_format="rgba16",
            keep_open="yes",
            keep_open_pause="no",  # reaching the end must not flip the player into pause
            idle="yes",
            pause=True,
            config=False,  # ignore ~/.config/mpv so user scripts don't interfere
            osc=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            audio_client_name="video-renamer",
            ytdl=False,
        )
        self.video = MpvVideoWidget(self.mpv, self)
        self.video.setMinimumSize(320, 180)
        # A file loaded before the window is shown would get no video output,
        # so loads wait until the render context exists.
        self._pending_load: tuple | None = None
        self.video.ready.connect(self._load_pending)

        self.play_button = QToolButton(self)
        self.play_button.setAutoRaise(True)
        self.play_button.setToolTip("Play/Pause (Space)")
        self.play_button.clicked.connect(self.toggle_play)

        self.slider = ChapterSlider(self)
        self.slider.setRange(0, 0)
        # Scrubbing: fast keyframe seeks while the bar moves, at most one in
        # flight (newer positions replace queued ones), then one exact seek
        # once it settles. Flooding mpv with exact seeks stalls/garbles 4K HEVC.
        self._seek_in_flight_since: float | None = None
        self._queued_seek: int | None = None
        self._settle_target: int | None = None
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(250)
        self._settle_timer.timeout.connect(self._seek_settled)
        self._seekFinished.connect(self._on_seek_finished)
        self.slider.sliderMoved.connect(self._scrub_to)
        self.slider.sliderReleased.connect(lambda: self._seek_absolute(self.slider.value(), exact=True))
        self.slider.actionTriggered.connect(self._on_slider_action)

        self.time_label = QLabel("0:00 / 0:00", self)
        self.video_info = QLabel(self)
        self.video_info.setEnabled(False)  # drawn in the muted text colour

        self.chapters = self._combo("Chapters (Ctrl+← / Ctrl+→)")
        self.chapters.activated.connect(self._on_chapter_chosen)
        self.video_tracks = self._combo("Video stream")
        self.audio_tracks = self._combo("Audio track")
        self.subtitle_tracks = self._combo("Subtitles")
        self._track_combos = {"video": self.video_tracks, "audio": self.audio_tracks, "sub": self.subtitle_tracks}
        for kind, combo in self._track_combos.items():
            combo.activated.connect(lambda index, kind=kind: self._on_track_chosen(kind, index))

        self.mute_button = QToolButton(self)
        self.mute_button.setAutoRaise(True)
        self.mute_button.setCheckable(True)
        self.mute_button.setToolTip("Mute")
        self.mute_button.toggled.connect(lambda muted: setattr(self.mpv, "mute", muted))
        self.mute_button.toggled.connect(self._update_icons)

        self.volume = QSlider(Qt.Orientation.Horizontal, self)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(100)
        self.volume.setToolTip("Volume")
        self.volume.valueChanged.connect(lambda v: setattr(self.mpv, "volume", v))
        self.volume.setValue(80)

        # Seek bar across the full width of the video, everything else below it.
        transport = QHBoxLayout()
        transport.addWidget(self.play_button)
        transport.addWidget(self.time_label)
        transport.addStretch(1)
        transport.addWidget(self.video_info)
        transport.addSpacing(12)
        transport.addWidget(self.mute_button)
        transport.addWidget(self.volume)

        options = QGridLayout()
        options.setColumnStretch(1, 1)
        options.setColumnStretch(3, 1)
        for row, column, text, combo in ((0, 0, "Chapter", self.chapters), (0, 2, "Video", self.video_tracks),
                                         (1, 0, "Audio", self.audio_tracks), (1, 2, "Subtitles", self.subtitle_tracks)):
            label = QLabel(text, self)
            label.setBuddy(combo)
            options.addWidget(label, row, column, Qt.AlignmentFlag.AlignRight)
            options.addWidget(combo, row, column + 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.slider)
        layout.addLayout(transport)
        layout.addLayout(options)

        self._positionChanged.connect(self._on_position)
        self._durationChanged.connect(self._on_duration)
        self._pauseChanged.connect(self._update_icons)
        self._tracksChanged.connect(self._on_tracks)
        self._trackSelected.connect(self._sync_track)
        self._videoParamsChanged.connect(self._update_video_info)
        self._chaptersChanged.connect(self._on_chapters)
        for name, signal in (
            ("time-pos", self._positionChanged),
            ("duration", self._durationChanged),
            ("pause", self._pauseChanged),
            ("eof-reached", self._pauseChanged),
            ("track-list", self._tracksChanged),
            ("video-params", self._videoParamsChanged),
            ("hwdec-current", self._videoParamsChanged),
            ("chapter-list", self._chaptersChanged),
        ):
            self.mpv.observe_property(name, lambda _name, value, signal=signal: signal.emit(value))
        for kind, (prop, _) in self.TRACK_KINDS.items():
            self.mpv.observe_property(prop, lambda _name, value, kind=kind: self._trackSelected.emit(kind, value))
        self.mpv.register_event_callback(self._on_mpv_event)

        self._on_tracks([])
        self._on_chapters([])
        self._update_icons()

    # --- public API -----------------------------------------------------

    def load(self, path: Path, position: int = 0, play: bool = False,
             audio_track: int = -1, video_track: int = -1, subtitle_track: int = -1) -> None:
        """Open a file, paused unless play is True. Track arguments are mpv
        track ids; -1 lets mpv choose, and subtitle_track 0 means off."""
        self.path = path
        self.slider.setRange(0, 0)
        self._reset_seeking()
        if not self.video.is_ready():
            self._pending_load = (path, position, play, audio_track, video_track, subtitle_track)
            return
        options = {"start": f"{position / 1000:.3f}", "pause": "no" if play else "yes"}
        if audio_track > 0:
            options["aid"] = str(audio_track)
        if video_track > 0:
            options["vid"] = str(video_track)
        if subtitle_track >= 0:
            options["sid"] = str(subtitle_track) if subtitle_track else "no"
        self.mpv.loadfile(str(path), **options)

    def unload(self) -> tuple[Path | None, int, bool, int, int, int]:
        """Release the current file. Returns (path, position, was_playing,
        audio_track, video_track, subtitle_track) to pass back to load()."""
        track = lambda value: value if isinstance(value, int) and not isinstance(value, bool) else -1
        sid = self.mpv.sid
        state = (
            self.path,
            int((self.mpv.time_pos or 0) * 1000),
            self.path is not None and not self.mpv.pause,
            track(self.mpv.aid),
            track(self.mpv.vid),
            track(sid) if sid not in (False, "no") else 0,
        )
        self._pending_load = None
        self._reset_seeking()
        self.mpv.command("stop")
        self.mpv.pause = True
        self.path = None
        self.slider.setRange(0, 0)
        self.time_label.setText("0:00 / 0:00")
        return state

    def _load_pending(self) -> None:
        if self._pending_load is not None:
            pending, self._pending_load = self._pending_load, None
            self.load(*pending)

    def shutdown(self) -> None:
        self._alive = False  # status messages still queued from mpv are ignored after this
        self.video.release()
        self.mpv.terminate()

    def toggle_play(self) -> None:
        if self.path is None:
            return
        if self.mpv.eof_reached:  # at the end: play again from the start
            self._seek_absolute(0, exact=True)
            self.mpv.pause = False
            return
        self.mpv.pause = not self.mpv.pause

    def seek(self, delta_ms: int) -> None:
        if self.path is not None:
            self.mpv.seek(delta_ms / 1000, "relative", "exact")

    def step_chapter(self, step: int) -> None:
        """Jump to the next (step > 0) or previous (step < 0) chapter start.

        Worked out from the chapter times rather than mpv's "chapter"
        property, which reads -1 at the very start of some files.
        """
        times = self.slider.chapter_times
        if self.path is None or not times:
            return
        position = int((self.mpv.time_pos or 0) * 1000)
        if step > 0:
            later = [t for t in times if t > position + 500]
            if later:
                self._seek_absolute(later[0], exact=True)
        else:
            # Just after a chapter start counts as "at" it, so go one further back.
            earlier = [t for t in times if t < position - 1500]
            self._seek_absolute(earlier[-1] if earlier else 0, exact=True)

    # --- internals ------------------------------------------------------

    def _clamp(self, ms: int) -> int:
        # Stay just short of the end: seeking onto the last frame ends the file.
        duration = self.slider.maximum()
        return max(0, min(ms, duration - 1000)) if duration > 2000 else max(0, ms)

    def _scrub_to(self, ms: int) -> None:
        if self.path is None:
            return
        ms = self._clamp(ms)
        self._settle_timer.start()
        if ms == self._settle_target:
            return  # same spot as the last request (e.g. dragging past the end)
        self._settle_target = ms
        busy = self._seek_in_flight_since is not None and time.monotonic() - self._seek_in_flight_since < 1.0
        if busy:
            self._queued_seek = ms
            return
        self._queued_seek = None
        self._seek_in_flight_since = time.monotonic()
        self.mpv.seek(ms / 1000, "absolute", "keyframes")

    def _on_seek_finished(self) -> None:
        self._seek_in_flight_since = None
        if self._queued_seek is not None:
            self._scrub_to(self._queued_seek)

    def _seek_settled(self) -> None:
        if self._settle_target is not None and not self.slider.isSliderDown():
            self._seek_absolute(self._settle_target, exact=True)

    def _seek_absolute(self, ms: int, exact: bool) -> None:
        if self.path is None:
            return
        if exact:  # supersedes any scrubbing still pending
            self._settle_timer.stop()
            self._settle_target = self._queued_seek = None
        self._seek_in_flight_since = time.monotonic()
        self.mpv.seek(self._clamp(ms) / 1000, "absolute", "exact" if exact else "keyframes")

    def _reset_seeking(self) -> None:
        self._settle_timer.stop()
        self._settle_target = self._queued_seek = self._seek_in_flight_since = None

    def _on_slider_action(self, _action) -> None:
        # Clicking the groove / paging with the keyboard: treated like
        # scrubbing, so rapid clicks don't pile up exact seeks.
        if not self.slider.isSliderDown():
            self._scrub_to(self.slider.sliderPosition())

    def _on_position(self, seconds) -> None:
        if seconds is not None and not self.slider.isSliderDown():
            self.slider.setValue(int(seconds * 1000))
            self._sync_chapter(int(seconds * 1000))
        self._update_time()

    def _on_duration(self, seconds) -> None:
        duration = int((seconds or 0) * 1000)
        self.slider.setRange(0, duration)
        self.slider.setPageStep(max(duration // 20, 1000))
        self._update_time()

    def _update_time(self) -> None:
        position = self.slider.value() if self.path else 0
        self.time_label.setText(f"{format_time(position)} / {format_time(self.slider.maximum())}")

    def _on_tracks(self, tracks) -> None:
        tracks = tracks or []
        labels = {"video": video_track_label, "audio": audio_track_label, "sub": subtitle_track_label}
        for kind, (_prop, nothing) in self.TRACK_KINDS.items():
            found = [t for t in tracks if t.get("type") == kind and not t.get("albumart")]
            ids: list[int | None] = [t["id"] for t in found]
            combo = self._track_combos[kind]
            combo.clear()
            if kind == "sub" and found:
                combo.addItem("Off")
                ids.insert(0, None)
            for number, track in enumerate(found, 1):
                combo.addItem(labels[kind](number, track))
            if not found:
                combo.addItem(nothing)
            combo.setEnabled(len(ids) > 1)
            self._track_ids[kind] = ids
            selected = next((t["id"] for t in found if t.get("selected")), None)
            self._sync_track(kind, selected)

    def _sync_track(self, kind: str, value) -> None:
        ids = self._track_ids[kind]
        if isinstance(value, bool) or value in (None, "no", "auto"):
            value = None  # nothing selected (subtitles off)
        if value in ids:
            combo = self._track_combos[kind]
            combo.setCurrentIndex(ids.index(value))
            combo.setToolTip(combo.currentText())

    def _on_track_chosen(self, kind: str, index: int) -> None:
        ids = self._track_ids[kind]
        if 0 <= index < len(ids):
            prop = self.TRACK_KINDS[kind][0]
            setattr(self.mpv, prop, "no" if ids[index] is None else ids[index])

    def _update_video_info(self, *_args) -> None:
        if not self._alive:
            return
        summary, details = video_info(self.mpv.video_params or {}, self.mpv.hwdec_current) if self.path else ("", "")
        self.video_info.setText(summary)
        self.video_info.setToolTip(details)

    def _on_chapters(self, chapters) -> None:
        chapters = chapters or []
        self.chapters.clear()
        for number, chapter in enumerate(chapters, 1):
            self.chapters.addItem(chapter_label(number, chapter))
        if not chapters:
            self.chapters.addItem("No chapters")
        self.chapters.setEnabled(bool(chapters))
        self.slider.chapter_times = [int(c.get("time", 0) * 1000) for c in chapters]
        self.slider.update()

    def _sync_chapter(self, position_ms: int) -> None:
        # Derived from the position (with a little slack) because mpv's own
        # "chapter" property lags when a seek lands a hair before a chapter start.
        times = self.slider.chapter_times
        if not times:
            return
        index = max((i for i, t in enumerate(times) if t <= position_ms + 100), default=0)
        if index != self.chapters.currentIndex():
            self.chapters.setCurrentIndex(index)

    def _on_chapter_chosen(self, index: int) -> None:
        if self.path is not None and 0 <= index < len(self.slider.chapter_times):
            self._seek_absolute(self.slider.chapter_times[index], exact=True)

    def _on_mpv_event(self, event) -> None:
        # Runs on mpv's event thread: only emit signals from here.
        if event.event_id.value in (mpv.MpvEventID.VIDEO_RECONFIG, mpv.MpvEventID.FILE_LOADED):
            # "video-params" isn't re-announced when the next file has the same format.
            self._videoParamsChanged.emit(None)
        if event.event_id.value == mpv.MpvEventID.PLAYBACK_RESTART:
            self._seekFinished.emit()
        if event.event_id.value == mpv.MpvEventID.END_FILE:
            data = event.data
            if data is not None and data.reason == mpv.MpvEventEndFile.ERROR:
                self.errorOccurred.emit(f"mpv could not play the file (error {data.error})")

    def _combo(self, tooltip: str) -> QComboBox:
        combo = QComboBox(self)
        combo.setToolTip(tooltip)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(10)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return combo

    def _update_icons(self, *_args) -> None:
        if not self._alive:
            return
        playing = self.path is not None and not self.mpv.pause and not self.mpv.eof_reached
        self.play_button.setIcon(self._icon(
            "media-playback-pause" if playing else "media-playback-start",
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay,
        ))
        muted = self.mute_button.isChecked()
        self.mute_button.setIcon(self._icon(
            "audio-volume-muted" if muted else "audio-volume-high",
            QStyle.StandardPixmap.SP_MediaVolumeMuted if muted else QStyle.StandardPixmap.SP_MediaVolume,
        ))

    def _icon(self, theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
        return QIcon.fromTheme(theme_name, self.style().standardIcon(fallback))
