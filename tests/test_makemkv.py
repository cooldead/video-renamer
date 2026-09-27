import json
import tempfile
import unittest
from pathlib import Path

from video_renamer import makemkv
from video_renamer.makemkv import Track

FIXTURE = Path(__file__).parent / "fixtures" / "makemkv_info_uhd.txt"


class SplitFieldsTest(unittest.TestCase):
    def test_quoted_commas_and_escapes(self):
        self.assertEqual(makemkv.split_fields('0,2,0,"Hello, world"'), ["0", "2", "0", "Hello, world"])
        self.assertEqual(makemkv.split_fields('1,"say \\"hi\\"",""'), ["1", 'say "hi"', ""])
        self.assertEqual(makemkv.parse_line("PRGV:10,20,65536"), ("PRGV", ["10", "20", "65536"]))
        self.assertIsNone(makemkv.parse_line("not a record"))

    def test_duration(self):
        self.assertEqual(makemkv.duration_seconds("1:55:18"), 6918)
        self.assertEqual(makemkv.duration_seconds("0:02:33"), 153)


class ParseInfoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = FIXTURE.read_text().splitlines()
        cls.disc = makemkv.parse_info(cls.lines)

    def test_disc_and_titles(self):
        self.assertEqual(self.disc.name, "Indiana Jones and the Raiders of the Lost Ark")
        self.assertEqual(self.disc.kind, "Blu-ray disc")
        self.assertEqual([t.id for t in self.disc.titles], [0, 1, 2, 3])
        feature = self.disc.titles[0]
        self.assertEqual((feature.duration, feature.seconds, feature.chapters), ("1:55:18", 6918, 31))
        self.assertEqual(feature.size_bytes, 60809988096)
        self.assertEqual(feature.source, "00801.mpls")
        self.assertEqual(feature.output_file, "Indiana Jones and the Raiders of the Lost Ark_t00.mkv")

    def test_tracks(self):
        tracks = self.disc.titles[0].tracks
        self.assertEqual(len(tracks), 47)
        self.assertEqual([t.id for t in tracks], list(range(47)))
        video, truehd = tracks[0], tracks[1]
        self.assertEqual(video.kind, "video")
        self.assertIn("3840x2160", video.label())
        self.assertEqual((truehd.kind, truehd.codec_id, truehd.language, truehd.is_default), ("audio", "A_TRUEHD", "eng", True))
        self.assertEqual(truehd.label(), "TrueHD Atmos · English · 7.1 · Surround 7.1 · default")
        forced = [t for t in tracks if t.is_forced_only]
        self.assertTrue(forced and all(t.kind == "subtitle" for t in forced))
        self.assertIn("forced only", forced[0].label())

    def test_drives(self):
        drives = makemkv.parse_drives(self.lines)
        self.assertEqual(len(drives), 1)
        self.assertEqual((drives[0].index, drives[0].device, drives[0].disc_label), (0, "/dev/sr0", "INDIANA_JONES_LOST_ARK"))
        self.assertTrue(drives[0].has_disc)

    def test_default_selection_like_makemkv(self):
        tracks = self.disc.titles[0].tracks
        picked = [t for t in tracks if makemkv.default_selected(t, "eng")]
        self.assertTrue(all(t.kind == "video" or t.language == "eng" for t in picked))
        self.assertIn(tracks[1], picked)                                      # English TrueHD
        self.assertNotIn(next(t for t in tracks if t.language == "fra"), picked)
        self.assertTrue(all(makemkv.default_selected(t, "eng", "all") for t in tracks))
        both = [t.language for t in tracks if t.kind != "video" and makemkv.default_selected(t, ["eng", "jpn"])]
        self.assertEqual(set(both), {"eng", "jpn"})
        french = next(t for t in tracks if t.language == "fra")
        self.assertTrue(makemkv.default_selected(french, ["fre"]))   # old-style code works too
        self.assertEqual([t.kind for t in tracks if makemkv.default_selected(t, "eng", "video")], ["video"])

    def test_parse_languages(self):
        self.assertEqual(makemkv.parse_languages("eng, JPN  fre;eng"), (["eng", "jpn", "fre"], []))
        self.assertEqual(makemkv.parse_languages("english, jp"), ([], ["english", "jp"]))
        self.assertEqual(makemkv.parse_languages(""), ([], []))


class ProgressTest(unittest.TestCase):
    def test_progress_records(self):
        self.assertAlmostEqual(makemkv.parse_progress("PRGV:100,32768,65536").fraction, 0.5)
        self.assertEqual(makemkv.parse_progress('PRGT:5018,0,"Saving to MKV file"').task, "Saving to MKV file")
        self.assertEqual(makemkv.parse_progress('PRGC:5017,0,"Saving all titles"').step, "Saving all titles")
        self.assertEqual(makemkv.parse_progress('MSG:5036,0,1,"Copy complete. 1 titles saved.","x"').message, "Copy complete. 1 titles saved.")
        self.assertIsNone(makemkv.parse_progress("PRGV:1,1,0"))


class KeyProblemTest(unittest.TestCase):
    def test_key_messages(self):
        for message in ("Evaluation period has expired, shareware functionality unavailable.",
                        "Evaluation version, evaluation period expired 3 day(s) ago",
                        "Your temporary key has expired and was removed. Please restart the application.",
                        "This application version is too old.  Please download the latest version at "
                        "https://www.makemkv.com/ or enter a registration key to continue using the current version."):
            self.assertTrue(makemkv.is_key_problem(message), message)
        for message in ("Failed to open disc", "Evaluation version, 25 day(s) out of 30 remaining"):
            self.assertFalse(makemkv.is_key_problem(message), message)

    def test_reg_args(self):
        self.assertEqual(makemkv.reg_args("  T-abc123\n"), ["reg", "T-abc123"])


class TrackMatchingTest(unittest.TestCase):
    @staticmethod
    def disc_track(i, kind, codec, lang):
        return Track(i, {1: {"video": "Video", "audio": "Audio", "subtitle": "Subtitles"}[kind], 5: codec, 3: lang})

    @staticmethod
    def file_track(i, kind, codec, lang):
        return {"id": i, "type": kind, "properties": {"codec_id": codec, "language": lang}}

    def test_in_order_with_skipped_and_extra_tracks(self):
        disc = [self.disc_track(0, "video", "V_MPEGH/ISO/HEVC", "eng"),
                self.disc_track(1, "audio", "A_TRUEHD", "eng"),
                self.disc_track(2, "audio", "A_AC3", "fra"),      # not in the file
                self.disc_track(3, "audio", "A_AC3", "eng"),
                self.disc_track(4, "subtitle", "S_HDMV/PGS", "eng")]
        file = [self.file_track(0, "video", "V_MPEGH/ISO/HEVC", "und"),
                self.file_track(1, "audio", "A_TRUEHD", "eng"),
                self.file_track(2, "audio", "A_AC3", "eng"),
                self.file_track(3, "subtitles", "S_TEXT/UTF8", "eng"),  # extra
                self.file_track(4, "subtitles", "S_HDMV/PGS", "eng")]
        self.assertEqual(makemkv.match_tracks(disc, file), {0: 0, 1: 1, 3: 2, 4: 4})

    def test_real_uhd_rip(self):
        """Tracks of the real rip of the fixture disc's title 0: B-style language
        codes in the file, and no "forced only" tracks (the disc has no forced
        subtitles). Every track actually in the file must be matched."""
        disc = makemkv.parse_info(FIXTURE.read_text().splitlines())
        file_tracks = json.loads((FIXTURE.parent / "mkvmerge_tracks_uhd.json").read_text())["tracks"]
        tracks = disc.titles[0].tracks
        mapping = makemkv.match_tracks(tracks, file_tracks)
        self.assertEqual(sorted(mapping.values()), [t["id"] for t in file_tracks])
        unmatched = [t for t in tracks if t.id not in mapping]
        self.assertTrue(unmatched and all(t.is_forced_only for t in unmatched))
        french = next(t for t in tracks if t.kind == "audio" and t.language == "fra")
        self.assertEqual(file_tracks[mapping[french.id]]["properties"]["language"], "fre")

    def test_language_codes(self):
        self.assertEqual(makemkv.normalize_language("fre"), "fra")
        self.assertEqual(makemkv.normalize_language("ENG"), "eng")

    def test_mkvmerge_args(self):
        file = [self.file_track(0, "video", "V", ""), self.file_track(1, "audio", "A", ""),
                self.file_track(2, "audio", "A", ""), self.file_track(3, "subtitles", "S", "")]
        args = makemkv.mkvmerge_keep_args(Path("in.mkv"), Path("out.mkv"), [0, 2], file)
        self.assertEqual(args, ["-o", "out.mkv", "--audio-tracks", "2", "--no-subtitles", "in.mkv"])
        self.assertEqual(makemkv.mkvmerge_keep_args(Path("i"), Path("o"), [0, 1, 2, 3], file), ["-o", "o", "i"])


class MiscTest(unittest.TestCase):
    def test_folder_name_and_profile(self):
        self.assertEqual(makemkv.safe_folder_name(" A/B  C. "), "A B C")
        with tempfile.TemporaryDirectory() as tmp:
            path = makemkv.write_profile(Path(tmp))
            self.assertIn('defaultSelection="+sel:all"', path.read_text())

    def test_rip_args(self):
        args = makemkv.rip_args(0, 3, Path("/out"), 120, Path("/p.xml"))
        self.assertEqual(args, ["-r", "--progress=-same", "--minlength=120", "--profile=/p.xml", "mkv", "disc:0", "3", "/out"])
        self.assertIn("--cache=256", makemkv.rip_args(0, 3, Path("/out"), 120, Path("/p.xml"), cache_mb=256))

    def test_preferred_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "settings.conf"
            conf.write_text('app_PreferredLanguage = "ger"\n')
            self.assertEqual(makemkv.preferred_language(conf), "ger")
            self.assertEqual(makemkv.preferred_language(Path(tmp) / "missing"), "eng")


if __name__ == "__main__":
    unittest.main()
