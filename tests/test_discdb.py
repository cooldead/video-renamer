import json
import unittest
from pathlib import Path

from video_renamer import discdb
from video_renamer.discdb import DbDisc, DbTitle, LocalFile

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return discdb.parse_search(json.loads((FIXTURES / name).read_text()))


class ParseTest(unittest.TestCase):
    def test_raiders(self):
        discs = load("discdb_raiders.json")
        uhd = next(d for d in discs if d.format == "UHD")
        self.assertEqual(uhd.base_name, "Indiana Jones and the Raiders of the Lost Ark (1981)")
        main = [t for t in uhd.titles if t.is_main]
        self.assertEqual([(t.seconds, t.size) for t in main], [(6918, 60809988096)])
        bonus = next(d for d in discs if d.format == "Blu-Ray")
        self.assertGreater(len(bonus.titles), 20)
        self.assertTrue(all(t.name for t in bonus.titles))


class MatchTest(unittest.TestCase):
    def test_real_rips(self):
        """The actual rips: 1:55:18.6 and 2:41:44 (mkvmerge durations)."""
        raiders = LocalFile(Path("/v/Indiana Jones/Indiana Jones and the Raiders of the Lost Ark_t00.mkv"), 6918.6)
        ranked = discdb.rank_discs([raiders], load("discdb_raiders.json"))
        disc, matches = ranked[0]
        self.assertEqual(disc.format, "UHD")
        self.assertTrue(matches[raiders.path].is_main)
        names = discdb.suggest_names(matches, disc)
        self.assertEqual(names[raiders.path], "Indiana Jones and the Raiders of the Lost Ark (1981)")

        battle = LocalFile(Path("/v/OBAA/One Battle After Another_t00.mkv"), 9704.2)
        clip = LocalFile(Path("/v/OBAA/One Battle After Another_t01.mkv"), 340.0)
        disc, matches = discdb.rank_discs([battle, clip], load("discdb_one_battle.json"))[0]
        self.assertEqual(list(matches), [battle.path])     # the 5:40 clip isn't named in TheDiscDB
        self.assertEqual(discdb.suggest_names(matches, disc)[battle.path], "One Battle After Another (2025)")

    def test_one_to_one_closest_first(self):
        disc = DbDisc("Show", 2020, "Series", "R", 0, "", "Blu-Ray", [
            DbTitle(0, 1300, 0, "", "Episode", "Pilot", 1, 1),
            DbTitle(1, 1302, 0, "", "Episode", "Second", 1, 2),
            DbTitle(2, 600, 0, "", "Extra", "Making of: The Show"),
        ])
        a, b, c, d = (LocalFile(Path(n), s) for n, s in (("a", 1301.9), ("b", 1300.2), ("c", 600.4), ("d", 999)))
        matches = discdb.match_files([a, b, c, d], disc)
        self.assertEqual({p.name: t.name for p, t in matches.items()}, {"a": "Second", "b": "Pilot", "c": "Making of: The Show"})
        names = discdb.suggest_names(matches, disc)
        self.assertEqual(names[b.path], "Season 01/Show S01E01 - Pilot")
        self.assertEqual(names[c.path], "extras/Making of - The Show")

    def test_several_main_versions_and_duplicates(self):
        disc = DbDisc("Film", 1999, "Movie", "R", 0, "", "Blu-Ray", [
            DbTitle(0, 7000, 0, "", "MainMovie", "Film"),
            DbTitle(1, 7600, 0, "", "MainMovie", "Extended Cut"),
            DbTitle(2, 100, 0, "", "Trailer", "Trailer"),
            DbTitle(3, 120, 0, "", "Trailer", "Trailer"),
        ])
        files = [LocalFile(Path(f"f{i}"), s) for i, s in enumerate((7000, 7600, 100, 120))]
        names = discdb.suggest_names(discdb.match_files(files, disc), disc)
        self.assertEqual(names[Path("f0")], "Film (1999)")
        self.assertEqual(names[Path("f1")], "Film (1999) - Extended Cut")
        self.assertEqual(sorted([names[Path("f2")], names[Path("f3")]]), ["extras/Trailer", "extras/Trailer (2)"])


class TextTest(unittest.TestCase):
    def test_safe_name_and_search_text(self):
        self.assertEqual(discdb.safe_name("Travel: Locations / Trivia?"), "Travel - Locations - Trivia")
        self.assertEqual(discdb.search_text("Indiana_Jones (2)"), "Indiana Jones")
        self.assertEqual(discdb.search_text("One Battle After Another"), "One Battle After Another")


if __name__ == "__main__":
    unittest.main()
