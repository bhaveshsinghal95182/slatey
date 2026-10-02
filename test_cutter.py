from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from cutter import CutPlan, cut_silences, plan_cuts, select_expression


class PlanCutsTests(unittest.TestCase):
    def test_middle_silence_keeps_padding_on_both_sides(self) -> None:
        result = plan_cuts([(2.0, 4.0)], 10.0, Fraction(10), 0.4)
        self.assertEqual(result.segments, [(Fraction(0), Fraction(11, 5)), (Fraction(19, 5), Fraction(10))])

    def test_start_silence_keeps_only_ending_padding(self) -> None:
        result = plan_cuts([(0.0, 2.0)], 10.0, Fraction(10), 0.4)
        self.assertEqual(result.segments, [(Fraction(9, 5), Fraction(10))])

    def test_end_silence_keeps_only_starting_padding(self) -> None:
        result = plan_cuts([(8.0, 10.0)], 10.0, Fraction(10), 0.4)
        self.assertEqual(result.segments, [(Fraction(0), Fraction(41, 5))])

    def test_short_silence_is_not_cut(self) -> None:
        with self.assertRaisesRegex(ValueError, r"No pauses longer than 0.3 s were found\."):
            plan_cuts([(2.0, 2.3)], 10.0, Fraction(10), 0.3)

    def test_boundaries_are_exact_frame_multiples(self) -> None:
        result = plan_cuts([(1.17, 2.83)], 5.01, Fraction(30000, 1001), 0.3)
        for segment in result.segments:
            for boundary in segment:
                self.assertEqual((boundary * Fraction(30000, 1001)).denominator, 1)

    def test_touching_segments_after_snapping_merge(self) -> None:
        result = plan_cuts([(0.21, 0.24), (0.26, 0.29)], 1.0, Fraction(10), 0)
        self.assertEqual(result.segments, [(Fraction(0), Fraction(1))])

    def test_zero_padding_removes_entire_silence(self) -> None:
        result = plan_cuts([(2.0, 4.0)], 6.0, Fraction(10), 0)
        self.assertEqual(result.segments, [(Fraction(0), Fraction(2)), (Fraction(4), Fraction(6))])

    def test_no_silences_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, r"No pauses longer than 0.5 s were found\."):
            plan_cuts([], 10.0, Fraction(30), 0.5)

    def test_whole_recording_silent_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, r"The whole recording is silent\."):
            plan_cuts([(0.0, 6.0)], 6.0, Fraction(60))

    def test_cut_plan_totals_and_clamp(self) -> None:
        result = CutPlan([(Fraction(0), Fraction(2)), (Fraction(4), Fraction(5))], 8.0)
        self.assertEqual(result.kept, 3.0)
        self.assertEqual(result.removed, 5.0)
        self.assertEqual(CutPlan([(Fraction(0), Fraction(10))], 8.0).removed, 0.0)

    def test_keep_pause_bounds(self) -> None:
        for value in (-0.1, 2.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan_cuts([(1.0, 3.0)], 6.0, Fraction(60), value)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.source = cls.root / "synthetic.mp4"
        audio = (
            "sine=frequency=440:sample_rate=48000:duration=6,"
            "volume=0:enable='between(t,1,2)+between(t,3,4)'"
        )
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=60:duration=6",
            "-f", "lavfi", "-i", audio,
            "-c:v", "libsvtav1", "-preset", "12", "-crf", "40", "-pix_fmt", "yuv420p",
            "-c:a", "flac", "-ac", "2", "-shortest", str(cls.source),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            cls.directory.cleanup()
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def test_select_expression_parses_with_thousands_of_segments(self) -> None:
        # A long recording yields over 1000 segments; a flat a+b+… chain fails past ~100.
        fps = Fraction(60)
        segments = [(Fraction(2 * i), Fraction(2 * i + 1)) for i in range(2000)]
        expression = select_expression(segments, fps)
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "nullsrc=size=16x16:rate=60:duration=4", "-vf", f"select='{expression}'", "-f", "null", "-"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_render_has_aligned_streams_and_progress(self) -> None:
        output = self.root / "rendered.mp4"
        updates: list[float] = []
        result = cut_silences(self.source, output, -40, 0.4, 0.3, updates.append)
        self.assertGreaterEqual(len(result.segments), 2)
        self.assertTrue(updates)
        self.assertEqual(updates[-1], 1.0)
        inspected = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(output)],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(inspected.stdout)
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        audio = next(stream for stream in data["streams"] if stream["codec_type"] == "audio")
        self.assertAlmostEqual(float(data["format"]["duration"]), result.kept, delta=1 / 30)
        self.assertAlmostEqual(float(video["duration"]), result.kept, delta=1 / 30)
        self.assertAlmostEqual(float(audio["duration"]), result.kept, delta=1 / 30)
        self.assertAlmostEqual(int(video["nb_frames"]), round(result.kept * 60), delta=1)

    def test_cli_dry_run_does_not_create_output(self) -> None:
        output = self.root / "dry-run.mp4"
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("cutter.py")), str(self.source),
             "-o", str(output), "--dry-run", "--min-silence", "0.4"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, re.compile(r"\d+ segments · kept \d+:\d{2}\.\d of \d+:\d{2}\.\d \(removed \d+:\d{2}\.\d\)"))
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
