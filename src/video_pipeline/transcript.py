from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .process import CommandRunner


MEDIA_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
DEFAULT_PROMPT = ""

_SRT_TIME = re.compile(
    r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})\s+-->\s+"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})"
)


def _seconds(hours: str, minutes: str, seconds: str, milliseconds: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def parse_srt(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        match = _SRT_TIME.fullmatch(lines[time_index])
        if not match:
            continue
        values = match.groupdict()
        start = _seconds(values["sh"], values["sm"], values["ss"], values["sms"])
        end = _seconds(values["eh"], values["em"], values["es"], values["ems"])
        spoken = " ".join(lines[time_index + 1 :]).strip()
        if spoken:
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": spoken})
    return segments


def project_asset_paths(root: Path, project_id: str) -> list[Path]:
    safe_id = Path(project_id).name
    if safe_id != project_id:
        raise ValueError("Invalid project id")
    paths: dict[str, Path] = {}
    for directory in (root / "out" / project_id, root / "work" / project_id / "media"):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
                paths.setdefault(path.name, path)
    if not paths:
        raise FileNotFoundError(f"Unknown or empty editor project: {project_id}")
    return sorted(paths.values(), key=lambda path: path.name.lower())


@dataclass
class TranscriptResult:
    asset_id: str
    srt: Path
    text: Path
    timeline: Path
    segments: int
    cached: bool = False


class ProjectTranscriber:
    def __init__(self, root: Path, model: Path, runner: CommandRunner | None = None):
        self.root = root.resolve()
        self.model = model.expanduser().resolve()
        self.runner = runner or CommandRunner()

    def transcribe(
        self,
        project_id: str,
        asset_ids: list[str] | None = None,
        *,
        force: bool = False,
        prompt: str = DEFAULT_PROMPT,
    ) -> tuple[list[TranscriptResult], Path]:
        if not self.model.is_file() and not self.runner.dry_run:
            raise FileNotFoundError(f"Whisper model not found: {self.model}")
        assets = project_asset_paths(self.root, project_id)
        if asset_ids:
            requested = {Path(asset_id).name for asset_id in asset_ids}
            assets = [path for path in assets if path.name in requested]
            missing = sorted(requested - {path.name for path in assets})
            if missing:
                raise FileNotFoundError(f"Unknown project media: {', '.join(missing)}")

        output_dir = self.root / "work" / project_id / "transcripts"
        if not self.runner.dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
        results = [self._transcribe_asset(path, output_dir, force=force, prompt=prompt) for path in assets]
        index = output_dir / "index.json"
        if not self.runner.dry_run:
            payload = {
                "version": 1,
                "project_id": project_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": str(self.model),
                "assets": [
                    {
                        "asset_id": item.asset_id,
                        "srt": str(item.srt.relative_to(self.root)),
                        "text": str(item.text.relative_to(self.root)),
                        "timeline": str(item.timeline.relative_to(self.root)),
                        "segments": item.segments,
                    }
                    for item in results
                ],
            }
            index.write_text(json.dumps(payload, indent=2) + "\n")
        return results, index

    def _transcribe_asset(self, source: Path, output_dir: Path, *, force: bool, prompt: str) -> TranscriptResult:
        prefix = output_dir / source.stem
        srt = prefix.with_suffix(".srt")
        text = prefix.with_suffix(".txt")
        timeline = prefix.with_suffix(".json")
        fingerprint = {
            "source_size": source.stat().st_size,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "model": str(self.model),
        }
        if not force and srt.is_file() and text.is_file() and timeline.is_file():
            try:
                cached = json.loads(timeline.read_text())
                if all(cached.get(key) == value for key, value in fingerprint.items()):
                    return TranscriptResult(source.name, srt, text, timeline, len(cached.get("segments", [])), True)
            except (json.JSONDecodeError, OSError):
                pass

        audio = output_dir / f".{source.stem}.transcribing.wav"
        ffmpeg = self.runner.require("ffmpeg")
        whisper = self.runner.require("whisper-cli")
        try:
            self.runner.run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
            ])
            self.runner.run([
                whisper, "-m", str(self.model), "-f", str(audio), "-l", "en",
                "-osrt", "-otxt", "-of", str(prefix), "-sow", "-ml", "52",
                "--prompt", prompt, "-np",
            ])
        finally:
            if not self.runner.dry_run:
                audio.unlink(missing_ok=True)

        segments = [] if self.runner.dry_run else parse_srt(srt.read_text())
        if not self.runner.dry_run:
            timeline.write_text(json.dumps({**fingerprint, "asset_id": source.name, "segments": segments}, indent=2) + "\n")
        return TranscriptResult(source.name, srt, text, timeline, len(segments))
