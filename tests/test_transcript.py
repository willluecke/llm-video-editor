import json
from pathlib import Path

from video_pipeline.process import CommandRunner
from video_pipeline.transcript import ProjectTranscriber, parse_srt, project_asset_paths


def test_parse_srt_returns_normalized_timeline():
    segments = parse_srt(
        "1\n00:00:01,250 --> 00:00:03,500\nA clear example.\n\n"
        "2\n00:00:04,000 --> 00:00:06,125\nThanks, Alex.\n"
    )
    assert segments == [
        {"start": 1.25, "end": 3.5, "text": "A clear example."},
        {"start": 4.0, "end": 6.125, "text": "Thanks, Alex."},
    ]


def test_project_assets_include_primary_and_imported_media(tmp_path: Path):
    primary = tmp_path / "out" / "example-project" / "primary.mov"
    imported = tmp_path / "work" / "example-project" / "media" / "pickup.mp4"
    primary.parent.mkdir(parents=True)
    imported.parent.mkdir(parents=True)
    primary.write_bytes(b"primary")
    imported.write_bytes(b"pickup")
    assert [path.name for path in project_asset_paths(tmp_path, "example-project")] == ["pickup.mp4", "primary.mov"]


def test_dry_run_builds_ffmpeg_and_whisper_commands(tmp_path: Path):
    source = tmp_path / "out" / "example-project" / "interview.mov"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    runner = CommandRunner(dry_run=True)
    results, _ = ProjectTranscriber(tmp_path, tmp_path / "model.bin", runner).transcribe("example-project")
    assert results[0].asset_id == "interview.mov"
    assert [Path(command[0]).name for command in runner.commands] == ["ffmpeg", "whisper-cli"]
    assert "--prompt" in runner.commands[1]
