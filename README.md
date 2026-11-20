# video-analyze

**Video in. Words, subtitles, and snapshots out.**

A Copilot skill for video links and local files. Uses **yt-dlp** to download,
**FFmpeg** to process media, and **faster-whisper** to transcribe locally.
These are third-party open-source tools, not hosted transcription services.

## Setup

Requires Git, FFmpeg (including `ffprobe`), and
[uv](https://docs.astral.sh/uv/getting-started/installation/).
Supports Python 3.10-3.13.

macOS with Homebrew:

```bash
brew install git ffmpeg uv
```

On Ubuntu/Debian, install `ffmpeg` and `git` with `apt`, then install `uv`
using the link above.

Install the skill (don't overwrite an existing installation):

```bash
mkdir -p "$HOME/.copilot/skills"
git clone https://github.com/nishchith-cp/video-analyze.git \
  "$HOME/.copilot/skills/video-analyze"
cd "$HOME/.copilot/skills/video-analyze"
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python \
  'yt-dlp>=2025.1.1' 'faster-whisper>=1.1,<2' 'av>=15,<17'
```

First setup downloads dependencies and model weights; later runs reuse them.
Restart your Copilot session if the skill isn't listed.

## Usage

```text
/video-analyze https://example.com/video
/video-analyze /absolute/path/to/video.mp4
```

Choose the full bundle or snapshots only when prompted; explicit choices skip
the prompt. Snapshots-only supports silent videos (`--frames-only`).
Transcription needs audio; live streams aren't supported.
Local originals stay untouched. By default, Copilot saves files without
reading transcripts or opening frames. Ask separately for content analysis.
CLI options include `--start 01:00 --end 02:30` (source-relative timestamps)
and `--privacy` (redacted input paths and quieter logs). Details: [SKILL.md](SKILL.md).

## Output

```text
your-output-folder/
  transcript.txt       Plain text
  transcript.srt       Subtitles
  transcript.json      Timed speech segments
  frames/              JPEG snapshots
  frames.json          When each snapshot was taken
  audio.wav            Extracted audio
  manifest.json        Status + output paths
```

URL inputs also save `video.<ext>` and `download-path.txt`.
Snapshots-only omits audio/transcripts. Completion links include files and
the output/snapshots folders.

[Browse fictional example outputs](examples/expected-output) (not real recordings).
Check `manifest.json` for completion; failed runs retain partial files.
`no_speech_detected` means no transcript text. Double-check important wording.

## Privacy and data handling

- Transcription runs locally. Downloads contact video providers; setup downloads
  packages and models. No browser cookies or login credentials are supplied.
- Outputs stay on disk without added encryption or automatic deletion.
  Logs may expose signed URLs; manifests include absolute file paths unless
  `--privacy` is selected. Privacy mode does not sanitize media or hide requests.
- Chat, tool output, and requested content analysis follow your assistant host's
  data policies. Review files before sharing; Git ignore rules aren't access controls.

Only process videos you're authorized to use. For advanced usage, defaults,
and safety guidance, see [SKILL.md](SKILL.md).

Offline checks: `python -m unittest discover -s scripts -p 'test_*.py'`
(requires FFmpeg; synthetic media and model stubs, no downloads). GitHub Actions
runs these checks on pushes and pull requests.
