import unittest

from video_renamer import ripstats


class RipStatsTest(unittest.TestCase):
    def test_meter(self):
        meter = ripstats.TransferMeter(window=10)
        for second in range(0, 31):
            # 10 MB/s for 20 s, then 30 MB/s
            total = second * 10_000_000 if second <= 20 else 200_000_000 + (second - 20) * 30_000_000
            meter.add(float(second), total)
        self.assertAlmostEqual(meter.rate(), 30_000_000)             # only the last 10 s
        self.assertAlmostEqual(meter.average(), 500_000_000 / 30)
        self.assertAlmostEqual(meter.remaining(800_000_000), 10.0)   # 300 MB left at 30 MB/s

    def test_empty_meter(self):
        meter = ripstats.TransferMeter()
        self.assertEqual((meter.rate(), meter.average(), meter.remaining(10)), (0.0, 0.0, None))

    def test_formatting(self):
        self.assertAlmostEqual(ripstats.speed_multiple(23_400_000, "Blu-ray disc"), 5.2)
        self.assertAlmostEqual(ripstats.speed_multiple(13_850_000, "DVD disc"), 10.0)
        self.assertEqual(ripstats.format_duration(3725), "1:02:05")
        self.assertEqual(ripstats.format_duration(65), "1:05")
        self.assertEqual(ripstats.format_rate(23_456_000), "23.5 MB/s")


if __name__ == "__main__":
    unittest.main()
