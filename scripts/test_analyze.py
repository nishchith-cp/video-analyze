"""Offline tests: real FFmpeg extraction, with the speech model stubbed."""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("analyze", Path(__file__).with_name("analyze.py"))
analyze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze)


class AnalyzeTests(unittest.TestCase):
    def test_frame_sampling_covers_entire_long_video_with_cap(self):
        times = analyze.frame_times(7200, 30, 60)
        self.assertEqual(len(times), 60)
        self.assertEqual(times[0], 60)
        self.assertEqual(times[-1], 7140)
        self.assertEqual(analyze.frame_times(0.1, 30, 60), [0.05])

    def test_srt_rounding_carries_into_next_hour(self):
        self.assertEqual(analyze.srt_time(3599.9996), "01:00:00,000")

    def test_invalid_interval_rejected(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    analyze.main(["unused", "--interval", value])

    def test_existing_output_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "keep.txt"
            marker.write_text("unchanged")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                analyze.main(["unused", "--output", directory])
            self.assertEqual(marker.read_text(), "unchanged")

    def test_real_extraction_and_transcript_serialization(self):
        for speech in (True, False):
            with self.subTest(speech=speech), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                video = root / "input.mp4"
                subprocess.run([
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
                    "-t", "2", "-c:v", "mpeg4", "-c:a", "aac", str(video),
                ], check=True)
                output = root / "result"
                segment = types.SimpleNamespace(start=0.1, end=1.5, text=" Test speech. ")
                model = types.SimpleNamespace(
                    transcribe=lambda *a, **k: (
                        iter([segment] if speech else []),
                        types.SimpleNamespace(language="en", language_probability=0.99),
                    )
                )
                module = types.SimpleNamespace(WhisperModel=lambda *a, **k: model)
                with patch.dict(sys.modules, {"faster_whisper": module}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = analyze.main([
                            str(video), "--output", str(output), "--interval", "0.5",
                            "--max-frames", "3",
                        ])
                self.assertEqual(result, 0)
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "complete")
                self.assertEqual(manifest["frame_count"], 3)
                expected_status = "speech_detected" if speech else "no_speech_detected"
                self.assertEqual(manifest["transcription_status"], expected_status)
                frames = json.loads((output / "frames.json").read_text())
                for frame in frames:
                    self.assertGreater((output / frame["path"]).stat().st_size, 0)
                self.assertGreater((output / "audio.wav").stat().st_size, 44)
                expected_text = "Test speech.\n" if speech else ""
                self.assertEqual((output / "transcript.txt").read_text(), expected_text)
                if speech:
                    self.assertIn("00:00:00,100 --> 00:00:01,500",
                                  (output / "transcript.srt").read_text())

    def test_missing_source_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            with contextlib.redirect_stderr(io.StringIO()):
                result = analyze.main([
                    str(Path(directory) / "missing.mp4"), "--output", str(output),
                ])
            self.assertEqual(result, 1)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["stage"], "acquiring")


if __name__ == "__main__":
    unittest.main()
