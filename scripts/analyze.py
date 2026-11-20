# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["yt-dlp>=2025.1.1", "faster-whisper>=1.1,<2", "av>=15,<17"]
# ///
"""Download a video, extract frames, and transcribe audio entirely locally."""

import argparse
import importlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def run(command, private=False):
    subprocess.run(command, check=True, capture_output=private)


def timestamp(value):
    parts = value.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        raise argparse.ArgumentTypeError("use seconds, MM:SS, or HH:MM:SS") from None
    if (not 1 <= len(parts) <= 3
            or any(not math.isfinite(number) or number < 0 for number in numbers)
            or (len(parts) > 1 and any(not part.isdigit() for part in parts[:-1]))
            or any(number >= 60 for number in numbers[1:])):
        raise argparse.ArgumentTypeError("use nonnegative seconds, MM:SS, or HH:MM:SS")
    return sum(number * 60 ** index for index, number in enumerate(reversed(numbers)))


def preflight(args):
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    modules = []
    if urlsplit(args.source).scheme in {"http", "https"}:
        modules.append("yt_dlp")
    if not args.frames_only:
        modules.extend(("faster_whisper", "av"))
    for module in modules:
        try:
            importlib.import_module(module)
        except (ImportError, OSError, RuntimeError):
            missing.append(module)
    if missing:
        raise RuntimeError("Missing or unusable prerequisites: " + ", ".join(missing)
                           + ". See SKILL.md for setup.")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def frame_times(duration, interval, maximum):
    count = min(maximum, max(1, math.ceil(duration / interval)))
    # Sample bin centers to avoid seeking exactly to EOF or a black opening frame.
    return [(index + 0.5) * duration / count for index in range(count)]


def srt_time(seconds):
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def save_transcript(output, segments, language, probability, offset=0):
    rows = [
        {"start": float(segment.start) + offset, "end": float(segment.end) + offset,
         "text": segment.text.strip()}
        for segment in segments
        if segment.text.strip()
    ]
    text = "\n".join(row["text"] for row in rows)
    (output / "transcript.txt").write_text(text + ("\n" if text else ""), encoding="utf-8")
    subtitles = "\n\n".join(
        f"{index}\n{srt_time(row['start'])} --> {srt_time(row['end'])}\n{row['text']}"
        for index, row in enumerate(rows, 1)
    )
    (output / "transcript.srt").write_text(
        subtitles + ("\n" if subtitles else ""), encoding="utf-8"
    )
    write_json(output / "transcript.json", {
        "language": language,
        "language_probability": float(probability),
        "segments": rows,
    })
    return len(rows)


def download_video(url, output, private=False, frames_only=False):
    # Keep the downloader's absolute-path handoff ephemeral in privacy mode.
    with tempfile.TemporaryDirectory(prefix=".download-", dir=output) as directory:
        handoff = Path(directory) / "path.txt" if private else output / "download-path.txt"
        run([
            sys.executable, "-m", "yt_dlp",
            "--ignore-config", "--no-playlist", "--no-progress",
            "--match-filter", "!is_live",
            "--restrict-filenames",
            "--format", ("bv*[height<=720]/bestvideo/best" if frames_only
                         else "bv*[height<=720]+ba/b[height<=720]/best"),
            "--merge-output-format", "mp4",
            "--output", str(output / "video.%(ext)s"),
            "--print-to-file", "after_move:filepath", str(handoff),
            "--", url,
        ], private=private)
        paths = handoff.read_text(encoding="utf-8").splitlines()
        if len(paths) != 1:
            raise RuntimeError(f"Expected one downloaded video, got {len(paths)}")
        video = Path(paths[0]).resolve()
        if video.parent != output or not video.is_file():
            raise RuntimeError("Downloader did not produce the expected video file")
        if private:
            (output / "download-path.txt").write_text(video.name + "\n", encoding="utf-8")
    return video, "url"


def analyze(args, output, manifest):
    parsed = urlsplit(args.source)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        video, kind = download_video(args.source, output, args.privacy, args.frames_only)
    elif "://" in args.source:
        raise ValueError("Only HTTP(S) video URLs or existing local files are supported")
    else:
        video, kind = Path(args.source).expanduser().resolve(), "local"
        if not video.is_file():
            raise ValueError(f"Local input does not exist: {video}")
    video_reference = ("[redacted]" if kind == "local" else video.name) if args.privacy else str(video)
    manifest.update({"input_kind": kind, "video": video_reference, "stage": "probing"})
    write_json(output / "manifest.json", manifest)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)
    ], check=True, capture_output=True, text=True)
    media = json.loads(probe.stdout)
    streams = media.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise ValueError("Input has no video stream")
    if not args.frames_only and not any(s.get("codec_type") == "audio" for s in streams):
        raise ValueError("Input has no audio stream to transcribe")
    duration = float(media.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Input must have a finite, positive duration (live streams unsupported)")
    end = duration if args.end is None else args.end
    if args.start >= duration or end > duration:
        raise ValueError("Selected range must lie within the input duration")
    selected_duration = end - args.start
    manifest.update({"duration_seconds": duration, "stage": "extracting"})
    if args.start or args.end is not None:
        manifest["time_range"] = {
            "start_seconds": args.start, "end_seconds": end,
            "timestamp_origin": "source",
        }
    write_json(output / "manifest.json", manifest)

    frames_dir = output / "frames"
    frames_dir.mkdir()
    frames = []
    for index, relative_time in enumerate(
            frame_times(selected_duration, args.interval, args.max_frames), 1):
        timestamp = args.start + relative_time
        frame = frames_dir / f"frame-{index:04}.jpg"
        run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
            "-ss", f"{timestamp:.6f}", "-i", str(video), "-map", "0:v:0",
            "-frames:v", "1", "-vf", "scale=min(1280\\,iw):-2", "-q:v", "3", str(frame),
        ], private=args.privacy)
        if not frame.is_file() or frame.stat().st_size == 0:
            raise RuntimeError(f"No frame was decoded at {timestamp:.6f} seconds")
        frames.append({"timestamp_seconds": timestamp, "path": str(frame.relative_to(output))})
    write_json(output / "frames.json", frames)
    manifest.update({"frame_count": len(frames), "frames_index": "frames.json"})
    if args.frames_only:
        manifest.update({
            "status": "complete", "stage": "complete", "transcription_status": "skipped",
        })
        write_json(output / "manifest.json", manifest)
        print("Saved video snapshots." if args.privacy else f"Saved video snapshots: {output}")
        return
    audio = output / "audio.wav"
    audio_range = []
    if args.start or args.end is not None:
        audio_range = ["-ss", f"{args.start:.6f}", "-t", f"{selected_duration:.6f}"]
    run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
        "-i", str(video), *audio_range, "-map", "0:a:0",
        "-vn", "-ac", "1", "-ar", "16000", str(audio),
    ], private=args.privacy)
    manifest.update({
        "stage": "transcribing", "frame_count": len(frames),
        "audio": "audio.wav", "frames_index": "frames.json",
    })
    write_json(output / "manifest.json", manifest)

    from faster_whisper import WhisperModel

    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(audio), language=args.language, beam_size=5,
        condition_on_previous_text=False, vad_filter=True,
    )
    count = save_transcript(output, segments, info.language, info.language_probability, args.start)
    manifest.update({
        "status": "complete", "stage": "complete",
        "transcription_status": "speech_detected" if count else "no_speech_detected",
        "segment_count": count, "language": info.language,
        "transcripts": ["transcript.txt", "transcript.srt", "transcript.json"],
    })
    write_json(output / "manifest.json", manifest)
    print("Saved video analysis." if args.privacy else f"Saved video analysis: {output}")
    if not count:
        print("No speech detected; transcript files are empty.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="HTTP(S) video URL or local video file")
    parser.add_argument("--output", type=Path, help="New output directory; must not already exist")
    parser.add_argument("--interval", type=positive_float, default=30, help="Target frame spacing in seconds")
    parser.add_argument("--max-frames", type=positive_int, default=60)
    parser.add_argument("--model", choices=["tiny", "base", "small", "medium", "large-v3"], default="base")
    parser.add_argument("--language", help="Language code, such as en; default: auto-detect")
    parser.add_argument("--frames-only", action="store_true", help="Skip audio and transcription")
    parser.add_argument("--start", type=timestamp, default=0,
                        help="Range start: seconds, MM:SS or HH:MM:SS (default: 0)")
    parser.add_argument("--end", type=timestamp, help="Exclusive range end (default: video end)")
    parser.add_argument("--privacy", action="store_true",
                        help="Redact saved input paths and suppress media-tool output")
    args = parser.parse_args(argv)
    if args.end is not None and args.end <= args.start:
        parser.error("--end must be greater than --start")
    try:
        preflight(args)
    except RuntimeError as error:
        parser.error(str(error))
    if args.output:
        output = args.output.expanduser().resolve()
        try:
            output.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            parser.error("Cannot create a new output directory" if args.privacy
                         else f"Cannot create a new output directory: {error}")
    else:
        output = Path(tempfile.mkdtemp(prefix="video-analysis-", dir=Path.cwd())).resolve()
    manifest = {
        "status": "running", "stage": "acquiring",
        "model": args.model, "requested_language": args.language,
        "frame_interval_seconds": args.interval, "max_frames": args.max_frames,
    }
    if args.privacy:
        manifest["privacy"] = True
    write_json(output / "manifest.json", manifest)
    try:
        analyze(args, output, manifest)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        # Do not persist exception strings: download errors can contain signed URLs.
        manifest.update({"status": "failed", "error_type": type(error).__name__})
        write_json(output / "manifest.json", manifest)
        if args.privacy:
            print(f"Analysis failed during {manifest['stage']} ({type(error).__name__}); "
                  "partial artifacts retained.", file=sys.stderr)
        else:
            print(f"Analysis failed: {error}\nPartial artifacts: {output}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        manifest.update({"status": "interrupted"})
        write_json(output / "manifest.json", manifest)
        print("Interrupted; partial artifacts retained." if args.privacy
              else f"Interrupted; partial artifacts: {output}", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
