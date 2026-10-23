from pathlib import Path

import yaml

from video_pipeline.pipeline import PipelineOptions, VideoPipeline, slugify
from video_pipeline.process import CommandRunner


def test_slugify_removes_raw_tag():
    assert slugify("Example Clip_raw") == "example-clip"


def test_dry_run_builds_both_exports(tmp_path: Path):
    config = {
        "input_dir": str(tmp_path), "match": ["*.mp4"],
        "tighten": {"threshold": "4%", "margin": "0.2s", "silent_speed": None},
        "transcribe": {"engine": "whisper-cli", "model": "small.en", "model_path": "~/models/ggml-small.en.bin", "burn_captions": True},
        "brand": {"watermark": "brand/watermark.png", "watermark_text": "LLM VIDEO EDITOR"},
        "targets": {"horizontal": {"w": 1920, "h": 1080, "caption_size": 12, "captions": "soft"}, "vertical": {"w": 1080, "h": 1920, "caption_size": 16, "captions": "burn"}},
        "export": {"loudness_lufs": -14, "video_bitrate": "12M", "audio_bitrate": "192k", "out_dir": "out"},
    }
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(config))
    runner = CommandRunner(dry_run=True)
    outputs = VideoPipeline(tmp_path, config_path, runner).cut(tmp_path / "demo_raw.mp4", PipelineOptions())
    assert outputs["horizontal"].name.endswith("youtube_1080p.mp4")
    assert outputs["vertical"].name.endswith("shorts_1080x1920.mp4")
    assert any(Path(command[0]).name == "auto-editor" for command in runner.commands)
    ffmpeg_commands = [command for command in runner.commands if Path(command[0]).name == "ffmpeg"]
    assert len(ffmpeg_commands) == 3
    assert any("pcm_s16le" in command for command in ffmpeg_commands)
    assert all("48000" in command for command in ffmpeg_commands[1:])
    assert all("drawtext=" in " ".join(command) for command in ffmpeg_commands[1:])
