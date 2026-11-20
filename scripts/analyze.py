# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["yt-dlp>=2025.1.1", "faster-whisper>=1.1,<2", "av>=15,<17"]
# ///
"""Download a video, extract frames, and transcribe audio entirely locally."""

import argparse
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


def run(command):
    subprocess.run(command, check=True)


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


def save_transcript(output, segments, language, probability):
    rows = [
        {"start": float(segment.start), "end": float(segment.end), "text": segment.text.strip()}
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


def download_video(url, output):
    run([
        sys.executable, "-m", "yt_dlp",
        "--ignore-config", "--no-playlist", "--no-progress",
        "--match-filter", "!is_live",
        "--restrict-filenames",
        "--format", "bv*[height<=720]+ba/b[height<=720]/best",
        "--merge-output-format", "mp4",
        "--output", str(output / "video.%(ext)s"),
        "--print-to-file", "after_move:filepath", str(output / "download-path.txt"),
        "--", url,
    ])
    paths = (output / "download-path.txt").read_text(encoding="utf-8").splitlines()
    if len(paths) != 1:
        raise RuntimeError(f"Expected one downloaded video, got {len(paths)}")
    video = Path(paths[0]).resolve()
    if video.parent != output or not video.is_file():
        raise RuntimeError("Downloader did not produce the expected video file")
    return video, "url"


def analyze(args, output, manifest):
    parsed = urlsplit(args.source)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        video, kind = download_video(args.source, output)
    elif "://" in args.source:
        raise ValueError("Only HTTP(S) video URLs or existing local files are supported")
    else:
        video, kind = Path(args.source).expanduser().resolve(), "local"
        if not video.is_file():
            raise ValueError(f"Local input does not exist: {video}")
    manifest.update({"input_kind": kind, "video": str(video), "stage": "probing"})
    write_json(output / "manifest.json", manifest)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)
    ], check=True, capture_output=True, text=True)
    media = json.loads(probe.stdout)
    streams = media.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise ValueError("Input has no video stream")
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise ValueError("Input has no audio stream to transcribe")
    duration = float(media.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Input must have a finite, positive duration (live streams unsupported)")
    manifest.update({"duration_seconds": duration, "stage": "extracting"})
    write_json(output / "manifest.json", manifest)

    frames_dir = output / "frames"
    frames_dir.mkdir()
    frames = []
    for index, timestamp in enumerate(frame_times(duration, args.interval, args.max_frames), 1):
        frame = frames_dir / f"frame-{index:04}.jpg"
        run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
            "-ss", f"{timestamp:.6f}", "-i", str(video), "-map", "0:v:0",
            "-frames:v", "1", "-vf", "scale=min(1280\\,iw):-2", "-q:v", "3", str(frame),
        ])
        if not frame.is_file() or frame.stat().st_size == 0:
            raise RuntimeError(f"No frame was decoded at {timestamp:.6f} seconds")
        frames.append({"timestamp_seconds": timestamp, "path": str(frame.relative_to(output))})
    write_json(output / "frames.json", frames)
    audio = output / "audio.wav"
    run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
        "-i", str(video), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", str(audio),
    ])
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
    count = save_transcript(output, segments, info.language, info.language_probability)
    manifest.update({
        "status": "complete", "stage": "complete",
        "transcription_status": "speech_detected" if count else "no_speech_detected",
        "segment_count": count, "language": info.language,
        "transcripts": ["transcript.txt", "transcript.srt", "transcript.json"],
    })
    write_json(output / "manifest.json", manifest)
    print(f"Saved video analysis: {output}")
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
    args = parser.parse_args(argv)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            parser.error(f"{tool} is required; install ffmpeg before running this skill")
    if args.output:
        output = args.output.expanduser().resolve()
        try:
            output.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            parser.error(f"Cannot create a new output directory: {error}")
    else:
        output = Path(tempfile.mkdtemp(prefix="video-analysis-", dir=Path.cwd())).resolve()
    manifest = {
        "status": "running", "stage": "acquiring",
        "model": args.model, "requested_language": args.language,
        "frame_interval_seconds": args.interval, "max_frames": args.max_frames,
    }
    write_json(output / "manifest.json", manifest)
    try:
        analyze(args, output, manifest)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        # Do not persist exception strings: download errors can contain signed URLs.
        manifest.update({"status": "failed", "error_type": type(error).__name__})
        write_json(output / "manifest.json", manifest)
        print(f"Analysis failed: {error}\nPartial artifacts: {output}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        manifest.update({"status": "interrupted"})
        write_json(output / "manifest.json", manifest)
        print(f"Interrupted; partial artifacts: {output}", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
