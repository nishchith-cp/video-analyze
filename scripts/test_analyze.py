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
import wave
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("analyze", Path(__file__).with_name("analyze.py"))
analyze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze)


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        # Dependencies are checked independently; never load a real speech model.
        self.imports = patch.object(analyze.importlib, "import_module", return_value=object())
        self.imports.start()
        self.addCleanup(self.imports.stop)

    def make_video(self, root, audio=True):
        video = root / "input.mp4"
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
        ]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000"]
        command += ["-t", "2", "-c:v", "mpeg4"]
        if audio:
            command += ["-c:a", "aac"]
        subprocess.run([*command, str(video)], check=True)
        return video

    def test_timestamp_formats_and_invalid_ranges(self):
        for value, expected in (("60.5", 60.5), ("01:00", 60), ("1:02:03.5", 3723.5)):
            self.assertEqual(analyze.timestamp(value), expected)
        invalid = ("-1", "nan", "inf", "1:60", "1:02:60", "1::2", "1.5:00", "1:2:3:4")
        for value in invalid:
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    analyze.main(["unused", "--start", value])
        for start, end in (("2", "2"), ("3", "2"), ("0", "0")):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                analyze.main(["unused", "--start", start, "--end", end])

    def test_preflight_reports_all_missing_before_download(self):
        errors = io.StringIO()
        with patch.object(analyze.shutil, "which", return_value=None), \
                patch.object(analyze.importlib, "import_module", side_effect=ImportError), \
                patch.object(analyze, "download_video") as download, \
                contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            analyze.main(["https://example.invalid/video"])
        for name in ("ffmpeg", "ffprobe", "yt_dlp", "faster_whisper", "av"):
            self.assertIn(name, errors.getvalue())
        download.assert_not_called()

    def test_frames_only_needs_no_python_media_dependencies(self):
        args = types.SimpleNamespace(source="local.mp4", frames_only=True)
        with patch.object(analyze.shutil, "which", return_value="/tool"), \
                patch.object(analyze.importlib, "import_module") as imports:
            analyze.preflight(args)
        imports.assert_not_called()
        args.source = "https://example.invalid/video"
        with patch.object(analyze.shutil, "which", return_value="/tool"), \
                patch.object(analyze.importlib, "import_module") as imports:
            analyze.preflight(args)
        imports.assert_called_once_with("yt_dlp")

    def test_frames_only_silent_video_range_and_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = self.make_video(root, audio=False)
            output = root / "result"
            logs = io.StringIO()
            with patch.dict(sys.modules, {"faster_whisper": None}), \
                    contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
                result = analyze.main([
                    str(video), "--output", str(output), "--frames-only", "--privacy",
                    "--start", "00:00.5", "--end", "00:01.5", "--interval", "0.5",
                ])
            self.assertEqual(result, 0)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["video"], "[redacted]")
            self.assertEqual(manifest["transcription_status"], "skipped")
            frames = json.loads((output / "frames.json").read_text())
            self.assertEqual([frame["timestamp_seconds"] for frame in frames], [0.75, 1.25])
            self.assertTrue(all((output / frame["path"]).stat().st_size for frame in frames))
            self.assertFalse((output / "audio.wav").exists())
            self.assertFalse((output / "transcript.txt").exists())
            self.assertNotIn(str(root), logs.getvalue())

    def test_silent_video_requires_opt_in_and_range_must_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = self.make_video(root, audio=False)
            cases = ([], ["--frames-only", "--start", "2"],
                     ["--frames-only", "--end", "3"])
            for index, flags in enumerate(cases):
                output = root / f"result-{index}"
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(analyze.main([
                        str(video), "--output", str(output), *flags,
                    ]), 1)
                self.assertFalse((output / "frames").exists())

    def test_range_trims_audio_and_offsets_transcripts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = self.make_video(root)
            output = root / "result"

            def transcribe(audio, **kwargs):
                with wave.open(audio) as stream:
                    self.assertAlmostEqual(stream.getnframes() / stream.getframerate(), 1, places=2)
                return (iter([types.SimpleNamespace(start=0.1, end=0.8, text=" Speech ")]),
                        types.SimpleNamespace(language="en", language_probability=1))

            model = types.SimpleNamespace(transcribe=transcribe)
            module = types.SimpleNamespace(WhisperModel=lambda *a, **k: model)
            with patch.dict(sys.modules, {"faster_whisper": module}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(analyze.main([
                    str(video), "--output", str(output), "--start", "0.5", "--end", "1.5",
                ]), 0)
            transcript = json.loads((output / "transcript.json").read_text())
            self.assertAlmostEqual(transcript["segments"][0]["start"], 0.6)
            self.assertIn("00:00:00,600 --> 00:00:01,300",
                          (output / "transcript.srt").read_text())
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["time_range"]["timestamp_origin"], "source")

    def test_private_download_redacts_handoff_and_cleans_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                output = Path(directory).resolve()

                def download(command, **kwargs):
                    self.assertTrue(kwargs["capture_output"])
                    self.assertEqual(command[command.index("--format") + 1],
                                     "bv*[height<=720]/bestvideo/best")
                    handoff = Path(command[command.index("--print-to-file") + 2])
                    video = output / "video.mp4"
                    video.write_bytes(b"synthetic")
                    handoff.write_text(str(video) + "\n")
                    if fail:
                        raise subprocess.CalledProcessError(1, command)

                with patch.object(analyze.subprocess, "run", side_effect=download):
                    if fail:
                        with self.assertRaises(subprocess.CalledProcessError):
                            analyze.download_video(
                                "https://example.invalid/?token=secret", output, True, True)
                    else:
                        analyze.download_video(
                            "https://example.invalid/?token=secret", output, True, True)
                        self.assertEqual((output / "download-path.txt").read_text(), "video.mp4\n")
                self.assertFalse(list(output.glob(".download-*")))

    def test_privacy_failure_suppresses_sensitive_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            logs = io.StringIO()
            with patch.object(analyze, "download_video",
                              side_effect=RuntimeError("https://example.invalid/?token=secret")), \
                    contextlib.redirect_stderr(logs):
                self.assertEqual(analyze.main([
                    "https://example.invalid/video", "--privacy", "--output", str(output),
                ]), 1)
            self.assertNotIn("secret", logs.getvalue())
            self.assertNotIn(directory, logs.getvalue())
            self.assertIn("acquiring", logs.getvalue())
            self.assertEqual(json.loads((output / "manifest.json").read_text())["status"], "failed")

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
