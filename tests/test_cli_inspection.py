import json
from pathlib import Path
from subprocess import CompletedProcess

from video_pipeline.cli import build_parser
from video_pipeline.diffusion_bridge import DIFFUSION_EDITOR_PIN, generate_diffusion_sidecar, import_diffusion_render
from video_pipeline.inspection import filmstrip_media, grab_frames, probe_media
from video_pipeline.project_docs import ensure_project_docs


def test_agent_cli_parses_assets_media_capture_check_render_and_diffusion():
    parser = build_parser()
    assert parser.parse_args(["context", "example-episode"]).command == "context"
    assert parser.parse_args(["assets", "relink", "example-episode", "shot.mov", "/tmp/new.mov"]).asset_command == "relink"
    assert parser.parse_args(["media", "grab", "shot.mov", "--project", "example-episode", "--time", "1.5"]).time == [1.5]
    assert parser.parse_args(["capture", "example-episode", "--time", "0", "--time", "5"]).time == [0.0, 5.0]
    assert parser.parse_args(["check", "example-episode", "--deep"]).deep is True
    assert parser.parse_args(["render", "example-episode", "--preset", "vertical"]).preset == "vertical"
    assert parser.parse_args(["diffusion", "sidecar", "example-episode"]).diffusion_command == "sidecar"


def test_project_docs_encode_agent_workflow_and_format_contract(tmp_path: Path):
    state = {
        "resolution": [1080, 1920],
        "fps": 30,
        "episode_template": {
            "version": 2,
            "name": "Example Episode",
            "question_slot_id": "question",
            "payoff_slot_id": "payoff",
            "locked_track_ids": ["v2"],
            "reserved_track_ids": ["a2"],
            "constraints": {"resolution": [1080, 1920], "fps": 30, "slot_timing_tolerance": 0.15},
            "slots": [{"id": "question", "start": 1, "end": 2, "prompt": "Ask if it fits"}],
        },
    }
    agents, format_doc = ensure_project_docs(tmp_path, "example-episode", state)
    assert "video-pipeline check example-episode" in agents.read_text()
    assert "executes unsandboxed" in agents.read_text()
    assert "`question` · 1.000–2.000s" in format_doc.read_text()


def test_probe_and_frame_inspection_use_machine_readable_ffmpeg_workflow(tmp_path: Path, monkeypatch):
    media = tmp_path / "shot.mov"
    media.write_bytes(b"media")
    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> CompletedProcess[str]:
        commands.append(command)
        if "-show_streams" in command:
            payload = {
                "format": {"format_name": "mov,mp4", "duration": "4.0", "bit_rate": "8000"},
                "streams": [{"index": 0, "codec_name": "h264", "codec_type": "video", "width": 1080, "height": 1920}],
            }
            return CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("video_pipeline.inspection._tool", lambda name: name)
    monkeypatch.setattr("video_pipeline.inspection._run", fake_run)

    metadata = probe_media(media)
    frames = grab_frames(media, [0, 1.25], tmp_path / "frames")
    strip = filmstrip_media(media, tmp_path / "filmstrip.png", frames=6, cell_width=240)

    assert metadata["duration"] == 4.0
    assert metadata["streams"][0]["width"] == 1080
    assert [item["time"] for item in frames] == [0.0, 1.25]
    assert strip["frames"] == 6 and strip["times"][0] == 0.333
    assert any("tile=4x2" in value for command in commands for value in command)


def test_diffusion_sidecar_is_static_pinned_interchange(tmp_path: Path, monkeypatch):
    source = tmp_path / "shot.mov"
    source.write_bytes(b"video")
    state = {
        "project": {"name": "Example Episode"},
        "resolution": [1080, 1920],
        "tracks": [{"id": "v1"}],
        "segments": [{
            "id": "hook-printer",
            "kind": "video",
            "asset_id": "shot.mov",
            "track_id": "v1",
            "timeline_start": 0,
            "source_start": 2,
            "source_end": 5,
            "speed": 3,
            "transform": {"scale": 1, "x": 0, "y": 0},
            "enabled": True,
        }],
    }
    monkeypatch.setattr("video_pipeline.editor.load_editor_state", lambda root, project: state)
    monkeypatch.setattr("video_pipeline.editor._resolve_asset", lambda root, project, asset: source)

    result = generate_diffusion_sidecar(tmp_path, "example-episode")
    sidecar = Path(result["sidecar"])
    source_text = (sidecar / "index.tsx").read_text()
    pin = json.loads((sidecar / ".diffusion-pin.json").read_text())
    tsconfig = json.loads((sidecar / "tsconfig.json").read_text())

    assert result["nodes"] == 1
    assert "<video" in source_text and "hook-printer" in source_text
    assert "muted={true}" in source_text
    assert "eval(" not in source_text and "new Function" not in source_text
    assert pin["diffusion_editor_commit"] == DIFFUSION_EDITOR_PIN
    assert tsconfig["compilerOptions"]["moduleResolution"] == "bundler"
    assert tsconfig["compilerOptions"]["skipLibCheck"] is True
    assert "unsandboxed" in (sidecar / "README.md").read_text()


def test_diffusion_render_import_becomes_content_addressed_asset(tmp_path: Path, monkeypatch):
    project = "example-episode"
    (tmp_path / "out" / project).mkdir(parents=True)
    (tmp_path / "work" / project / "media").mkdir(parents=True)
    reference = tmp_path / "out" / project / "reference.mp4"
    reference.write_bytes(b"reference")
    rendered = tmp_path / "motion.mp4"
    rendered.write_bytes(b"motion")

    def fake_probe(path: Path) -> dict:
        return {"duration": 1.0, "width": 1080, "height": 1920, "fps": 30.0, "has_audio": False}

    def fake_load(root: Path, project_id: str) -> dict:
        assets = [
            {"id": path.name, "name": path.stem, "duration": 1.0, "resolution": [1080, 1920], "fps": 30, "has_audio": False}
            for path in (root / "work" / project_id / "media").iterdir()
            if path.is_file()
        ]
        return {"media": "reference.mp4", "assets": assets, "tracks": [], "segments": []}

    monkeypatch.setattr("video_pipeline.editor.probe", fake_probe)
    monkeypatch.setattr("video_pipeline.editor.load_editor_state", fake_load)
    monkeypatch.setattr("video_pipeline.editor.save_editor_state", lambda root, project_id, media, state: state)

    result = import_diffusion_render(tmp_path, project, rendered)
    assert result["asset"]["id"] == "motion.mp4"
    assert result["content_id"].startswith("sha256:")
    assert (tmp_path / "work" / project / "media" / "motion.mp4").read_bytes() == b"motion"
