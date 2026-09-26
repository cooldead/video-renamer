import subprocess
import tempfile
import unittest
from pathlib import Path

from video_renamer import export


class ExportTest(unittest.TestCase):
    def test_progress_lines(self):
        p = export.parse_rsync_progress("  1,234,567,890  45%  110.21MB/s    0:00:10 (xfr#1, to-chk=3/5)")
        self.assertEqual((p.bytes_done, p.percent, p.speed, p.eta), (1234567890, 45, "110.21MB/s", "0:00:10"))
        self.assertEqual(export.parse_rsync_progress("          2.50G  12%   80.00MB/s    0:04:00").bytes_done, int(2.5 * 1024 ** 3))
        self.assertIsNone(export.parse_rsync_progress("sending incremental file list"))

    def test_verify_and_real_rsync(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie (2026)"
            (source / "extras").mkdir(parents=True)
            (source / "Movie (2026).mkv").write_bytes(b"x" * 5000)
            (source / "extras" / "Menu.mkv").write_bytes(b"y" * 300)
            nas = Path(tmp) / "nas"
            nas.mkdir()
            target = nas / source.name
            self.assertEqual(len(export.verify_copy(source, target)), 2)   # nothing copied yet
            result = subprocess.run([export.RSYNC, *export.rsync_args(source, nas)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(export.verify_copy(source, target), [])
            (target / "extras" / "Menu.mkv").write_bytes(b"y" * 10)
            self.assertIn("10 of 300 bytes", export.verify_copy(source, target)[0])
            self.assertEqual(export.folder_size(source), 5300)


if __name__ == "__main__":
    unittest.main()
