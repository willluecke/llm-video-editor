import http.client
import json
import shutil
import subprocess
import threading
from io import BytesIO
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from video_pipeline.cli import build_parser
from video_pipeline.editor import EditorRequestHandler, OperatorBridge, _drawtext_escape, _ffmpeg_progress_seconds, _probe_cached, _render_asset_paths, build_render_command, create_project_from_media, discover_projects, duplicate_editor_project, import_operator_file, import_project_media, load_editor_state, resolve_operator_paths, save_editor_state, trash_project_media, validate_episode_template


def _metadata(duration: float = 12.5) -> dict:
    return {"duration": duration, "width": 1920, "height": 1080, "fps": 30.0, "has_audio": True}


def test_ui_command_has_local_defaults():
    args = build_parser().parse_args(["ui", "--no-open"])
    assert args.command == "ui"
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.no_open is True


def test_local_editor_api_requires_session_and_rejects_cross_origin_mutation(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), EditorRequestHandler)
    authority = f"127.0.0.1:{server.server_port}"
    server.editor_root = tmp_path
    server.session_token = "local-test-token"
    server.allowed_authorities = {authority}
    server.terminal_manager = type("TerminalStub", (), {"full_access": False})()
    server.render_manager = object()
    server.operator_bridge = OperatorBridge(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request("GET", "/api/projects", headers={"Host": authority})
        denied = connection.getresponse()
        assert denied.status == 403
        denied.read()

        connection.request("GET", "/api/session", headers={"Host": authority})
        session = connection.getresponse()
        assert session.status == 200
        cookie = session.getheader("Set-Cookie").split(";", 1)[0]
        session.read()

        connection.request("GET", "/api/projects", headers={"Host": authority, "Cookie": cookie})
        projects = connection.getresponse()
        assert projects.status == 200
        projects.read()

        connection.request(
            "POST",
            "/api/operator/context",
            body=b"{}",
            headers={
                "Host": authority,
                "Origin": "https://attacker.example",
                "Cookie": cookie,
                "Content-Type": "application/json",
                "Content-Length": "2",
            },
        )
        rejected = connection.getresponse()
        assert rejected.status == 403
        rejected.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_probe_accepts_na_frame_rate_for_audio_only_media(tmp_path: Path, monkeypatch):
    media = tmp_path / "voice-effect.mov"
    media.write_bytes(b"audio")

    class Result:
        stdout = json.dumps({
            "streams": [{"index": 0, "codec_type": "audio", "r_frame_rate": "N/A"}],
            "format": {"duration": "1.19"},
        })

    monkeypatch.setattr("video_pipeline.editor.shutil.which", lambda command: "/usr/bin/ffprobe")
    monkeypatch.setattr("video_pipeline.editor.subprocess.run", lambda *args, **kwargs: Result())

    metadata = _probe_cached(str(media), media.stat().st_mtime_ns)

    assert metadata["duration"] == 1.19
    assert metadata["fps"] == 0.0
    assert metadata["has_audio"] is True


def test_ffmpeg_progress_ignores_na_and_malformed_timestamps():
    assert _ffmpeg_progress_seconds("out_time_ms=N/A") is None
    assert _ffmpeg_progress_seconds("out_time_us=") is None
    assert _ffmpeg_progress_seconds("out_time_ms=not-a-number") is None
    assert _ffmpeg_progress_seconds("out_time_us=1500000") == 1.5
    assert _ffmpeg_progress_seconds("frame=2") is None


def test_discovers_existing_exports_without_manifest(tmp_path: Path):
    project_dir = tmp_path / "out" / "sample-edit"
    project_dir.mkdir(parents=True)
    (project_dir / "version_two.mp4").write_bytes(b"video")
    (project_dir / "notes.txt").write_text("ignore me")

    projects = discover_projects(tmp_path)

    assert projects[0]["id"] == "sample-edit"
    assert projects[0]["name"] == "Sample Edit"
    assert projects[0]["media"][0]["id"] == "version_two.mp4"


def test_state_is_initialized_and_saved_per_media(tmp_path: Path, monkeypatch):
    project_dir = tmp_path / "out" / "sample-edit"
    project_dir.mkdir(parents=True)
    media = project_dir / "preview.mp4"
    media.write_bytes(b"video")
    (project_dir / "newer-version.mp4").write_bytes(b"video")
    monkeypatch.setattr("video_pipeline.editor.probe", lambda path: _metadata())

    initial = load_editor_state(tmp_path, "sample-edit", media.name)
    assert initial["segments"][0]["source_end"] == 12.5
    assert initial["saved"] is False

    initial["segments"] = [
        {"id": "a", "label": "Opening", "source_start": 1, "source_end": 4.25, "speed": 10, "volume": 0.8},
        {"id": "b", "label": "Close", "source_start": 8, "source_end": 11, "volume": 1.1},
    ]
    saved = save_editor_state(tmp_path, "sample-edit", media.name, initial)

    assert [segment["label"] for segment in saved["segments"]] == ["Opening", "Close"]
    assert saved["segments"][0]["speed"] == 10
    assert (tmp_path / "work" / "sample-edit" / "edits" / "preview.json").is_file()
    project_file = tmp_path / "work" / "sample-edit" / "sample-edit.llmve"
    document = json.loads(project_file.read_text())
    assert document["format"] == "llm-video-editor-project"
    assert document["active_media"] == "preview.mp4"
    assert document["edits"]["preview.mp4"]["segments"][0]["label"] == "Opening"

    reopened = load_editor_state(tmp_path, "sample-edit")
    assert reopened["media"] == "preview.mp4"
    assert reopened["segments"][1]["label"] == "Close"


def test_render_command_cuts_and_concatenates_video_and_audio(tmp_path: Path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "manual.mp4"
    state = {
        "has_audio": True,
        "segments": [
            {"source_start": 1.0, "source_end": 3.0, "volume": 0.5, "enabled": True},
            {"source_start": 7.0, "source_end": 9.5, "volume": 1.0, "enabled": True},
        ],
    }

    command = build_render_command("ffmpeg", source, output, state)
    filters = command[command.index("-filter_complex") + 1]

    assert "trim=start=1.0:end=3.0" in filters
    assert "atrim=start=7.0:end=9.5" in filters
    assert "volume=0.5" in filters
    assert "concat=n=2:v=1:a=1[vout][aout]" in filters
    assert command[-1] == str(output)


def test_render_asset_paths_follows_enabled_timeline_instead_of_stale_registry(tmp_path: Path):
    project_id = "sample-edit"
    output = tmp_path / "out" / project_id
    media = tmp_path / "work" / project_id / "media"
    output.mkdir(parents=True)
    media.mkdir(parents=True)
    active_video = output / "active.mp4"
    replacement_audio = media / "dialogue-clean.mov"
    active_video.write_bytes(b"video")
    replacement_audio.write_bytes(b"audio")
    state = {
        "assets": [
            {"id": active_video.name},
            {"id": "deleted-old-export.mp4"},
        ],
        "segments": [
            {"kind": "video", "asset_id": active_video.name, "enabled": True},
            {"kind": "audio", "asset_id": replacement_audio.name, "enabled": True},
            {"kind": "video", "asset_id": "disabled-missing.mp4", "enabled": False},
        ],
    }

    paths = _render_asset_paths(tmp_path, project_id, state)

    assert paths == {
        active_video.name: active_video,
        replacement_audio.name: replacement_audio,
    }


def test_multitrack_render_includes_effects_text_audio_and_progress(tmp_path: Path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    output = tmp_path / "render.mp4"
    state = {
        "version": 2,
        "media": first.name,
        "resolution": [1920, 1080],
        "fps": 30,
        "has_audio": True,
        "assets": [
            {"id": first.name, "has_audio": True},
            {"id": second.name, "has_audio": True},
        ],
        "tracks": [
            {"id": "v1", "kind": "video", "muted": False},
            {"id": "v2", "kind": "video", "muted": False},
            {"id": "a1", "kind": "audio", "muted": False},
        ],
        "segments": [
            {"id": "one", "kind": "video", "asset_id": first.name, "track_id": "v1", "audio_track_id": "a1", "audio_linked": True, "timeline_start": 0, "source_start": 0, "source_end": 4, "speed": 1, "volume": .8, "audio_fade_in": .2, "audio_fade_out": .3, "transition_in": {"type": "fade", "duration": .25}, "transition_out": {"type": "none", "duration": 0}, "transform": {"scale": 1, "x": 0, "y": 0, "rotation": 90, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "crop_left": 0}, "color": {"brightness": .1, "contrast": 1.1, "saturation": .9, "temperature": .1}, "keyframes": []},
            {"id": "two", "kind": "video", "asset_id": second.name, "track_id": "v2", "audio_track_id": "a1", "audio_linked": True, "timeline_start": 1, "source_start": 2, "source_end": 6, "speed": 2, "volume": 1, "audio_fade_in": 0, "audio_fade_out": 0, "transition_in": {"type": "wipeleft", "duration": .5}, "transition_out": {"type": "none", "duration": 0}, "transform": {"scale": .5, "x": 20, "y": -10, "crop_top": 1, "crop_right": 2, "crop_bottom": 3, "crop_left": 4}, "color": {"brightness": 0, "contrast": 1, "saturation": 1, "temperature": 0}, "keyframes": []},
            {"id": "title", "kind": "title", "track_id": "v2", "timeline_start": .5, "duration": 2, "text": "Hello", "font_size": 64, "text_color": "#ffffff", "enabled": True},
        ],
    }

    command = build_render_command("ffmpeg", first, output, state, asset_paths={first.name: first, second.name: second}, preset="youtube", progress=True)
    filters = command[command.index("-filter_complex") + 1]

    assert command.count("-i") == 2
    assert "overlay=" in filters
    assert "drawtext=text='Hello'" in filters
    assert ":y=h*0.10:text_align=C:line_spacing=10" in filters
    assert "transpose=clock" in filters
    assert "tpad=stop_mode=clone:stop_duration=0.033333" in filters
    assert "enable='gte(t,0.000000)*lt(t,4.000000)'" in filters
    assert "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0" in filters
    assert "afade=t=in" in filters
    assert "1920x1080" in filters
    assert "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709" in filters
    assert "-progress" in command
    assert command[command.index("-color_primaries") + 1] == "bt709"
    assert command[command.index("-color_trc") + 1] == "bt709"


def test_multitrack_render_tone_maps_hdr_sources_only(tmp_path: Path, monkeypatch):
    hdr = tmp_path / "iphone-hlg.mov"
    sdr = tmp_path / "glasses-bt709.mov"
    hdr.write_bytes(b"hdr")
    sdr.write_bytes(b"sdr")
    output = tmp_path / "render.mp4"
    monkeypatch.setattr(
        "video_pipeline.editor.probe",
        lambda path: {"color_transfer": "arib-std-b67" if path == hdr else "bt709"},
    )
    state = {
        "version": 2,
        "media": hdr.name,
        "resolution": [1080, 1920],
        "fps": 30,
        "has_audio": False,
        "assets": [{"id": hdr.name}, {"id": sdr.name}],
        "tracks": [{"id": "v1", "kind": "video", "muted": False}],
        "segments": [
            {"id": "hdr", "kind": "video", "asset_id": hdr.name, "track_id": "v1", "timeline_start": 0, "source_start": 0, "source_end": 1, "speed": 1},
            {"id": "sdr", "kind": "video", "asset_id": sdr.name, "track_id": "v1", "timeline_start": 1, "source_start": 0, "source_end": 1, "speed": 1},
        ],
    }

    command = build_render_command(
        "ffmpeg",
        hdr,
        output,
        state,
        asset_paths={hdr.name: hdr, sdr.name: sdr},
        preset="vertical",
    )
    filters = command[command.index("-filter_complex") + 1]

    assert filters.count("zscale=transfer=linear:npl=100") == 1
    assert filters.count("tonemap=tonemap=hable:desat=0") == 1


def test_rendered_caption_wraps_inside_vertical_safe_area(tmp_path: Path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "render.mp4"
    state = {
        "version": 2,
        "media": source.name,
        "resolution": [1920, 1080],
        "fps": 30,
        "has_audio": False,
        "assets": [{"id": source.name, "has_audio": False}],
        "tracks": [{"id": "v1", "kind": "video", "muted": False}],
        "segments": [
            {"id": "video", "kind": "video", "asset_id": source.name, "track_id": "v1", "audio_linked": False, "timeline_start": 0, "source_start": 0, "source_end": 2, "speed": 1},
            {"id": "caption", "kind": "caption", "track_id": "v1", "timeline_start": 0, "duration": 2, "text": "a twin-turbo engine for the Z.", "font_size": 72, "text_color": "#ffffff", "enabled": True},
        ],
    }

    command = build_render_command("ffmpeg", source, output, state, asset_paths={source.name: source}, preset="vertical")
    filters = command[command.index("-filter_complex") + 1]

    assert "a twin-turbo engine\nfor the Z." in filters
    assert ":text_align=C:line_spacing=10:box=1:" in filters


def test_drawtext_escapes_apostrophes_and_percent_expansion():
    escaped = _drawtext_escape("It doesn't fit: 100%")

    assert escaped == "It doesn'\\\\\\''t fit\\: 100\\\\%"


def test_drawtext_apostrophe_matches_textfile_render(tmp_path: Path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    text = "When a part doesn't fit,\nwhat do you do?"
    textfile = tmp_path / "caption.txt"
    textfile.write_text(text)

    def frame_md5(filter_graph: str) -> str:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=white:s=640x180:d=0.1",
                "-vf", filter_graph, "-frames:v", "1", "-f", "md5", "-",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    common = ":fontsize=36:fontcolor=black:enable='between(t,0,1)'"
    escaped_render = frame_md5(f"drawtext=text='{_drawtext_escape(text)}'{common}")
    textfile_render = frame_md5(f"drawtext=textfile='{textfile}'{common}")

    assert escaped_render == textfile_render


def test_imports_project_media_and_operator_validates_actions(tmp_path: Path, monkeypatch):
    output = tmp_path / "out" / "sample-edit"
    output.mkdir(parents=True)
    (output / "preview.mp4").write_bytes(b"video")
    monkeypatch.setattr("video_pipeline.editor.probe", lambda path: _metadata())

    asset = import_project_media(tmp_path, "sample-edit", "b-roll.mov", BytesIO(b"new video"), 9)

    assert asset["id"] == "b-roll.mov"
    assert (tmp_path / "work" / "sample-edit" / "media" / "b-roll.mov").read_bytes() == b"new video"

    bridge = OperatorBridge()
    accepted = bridge.add({"actions": [{"type": "move_clip", "clip_id": "one", "timeline_start": 2}]})
    assert accepted["actions"][0]["cursor"] == 1
    assert bridge.poll(0)["actions"][0]["type"] == "move_clip"


def test_operator_bridge_owns_revisioned_state_and_rejects_stale_viewers():
    bridge = OperatorBridge()
    stale_state = {
        "project_id": "example-project",
        "media": "opening.mov",
        "segments": [
            {"id": "operator-old", "kind": "video", "source_start": 0, "source_end": 16.48, "speed": 1, "transform": {"rotation": 0}},
            {"id": "canonical", "kind": "video", "source_start": 0, "source_end": 7.84, "speed": 1, "transform": {"rotation": 0}},
            {"id": "old-title", "kind": "title", "duration": 4, "text": "Old title"},
        ],
        "tracks": [{"id": "v1", "kind": "video"}],
        "assets": [],
    }
    bridge.set_context({"project": {"id": "example-project"}, "revision": 0, "state": stale_state})

    result = bridge.add({"actions": [
        {"type": "delete_clip", "clip_id": "operator-old"},
        {"type": "delete_clip", "clip_id": "old-title"},
        {"type": "set_clip", "clip_id": "canonical", "patch": {"transform": {"rotation": 90}}},
    ]})

    assert result["revision"] == 1
    assert [segment["id"] for segment in result["state"]["segments"]] == ["canonical"]
    assert result["state"]["segments"][0]["transform"]["rotation"] == 90
    assert bridge.poll(0)["state"] == result["state"]

    bridge.set_context({"project": {"id": "example-project"}, "revision": 0, "state": stale_state})
    assert [segment["id"] for segment in bridge.context()["state"]["segments"]] == ["canonical"]


def test_operator_bridge_assigns_stable_ids_to_server_created_text():
    bridge = OperatorBridge()
    bridge.set_context({"state": {"segments": [], "tracks": [{"id": "v1", "kind": "video"}], "assets": []}})

    result = bridge.add({"actions": [{"type": "add_title", "text": "Hello", "timeline_start": 1, "duration": 2}]})

    action = result["actions"][0]
    assert action["result_clip_id"] == "operator-1-title"
    assert result["state"]["segments"][0]["id"] == action["result_clip_id"]


def test_operator_replaces_template_clip_and_matching_audio_without_changing_slot_duration():
    bridge = OperatorBridge()
    bridge.set_context({"state": {
        "segments": [
            {"id": "picture", "kind": "video", "asset_id": "old.mov", "track_id": "v1", "slot_id": "hook", "template_source": True, "timeline_start": 0, "source_start": 2, "source_end": 12, "speed": 5},
            {"id": "sound", "kind": "audio", "asset_id": "old.mov", "track_id": "a1", "slot_id": "hook", "template_source": True, "timeline_start": 0, "source_start": 2, "source_end": 12, "speed": 5},
        ],
        "tracks": [{"id": "v1", "kind": "video", "locked": False}, {"id": "a1", "kind": "audio", "locked": False}],
        "assets": [{"id": "old.mov", "name": "Old", "duration": 20, "has_audio": True}, {"id": "new.mov", "name": "New", "duration": 6, "has_audio": True}],
    }})

    result = bridge.add({"actions": [{"type": "replace_clip", "clip_id": "picture", "asset_id": "new.mov"}]})
    picture, sound = result["state"]["segments"]

    assert picture["asset_id"] == sound["asset_id"] == "new.mov"
    assert picture["template_source"] is sound["template_source"] is False
    assert picture["source_start"] == sound["source_start"] == 0
    assert picture["source_end"] == sound["source_end"] == 6
    assert picture["speed"] == sound["speed"] == 3
    assert (picture["source_end"] - picture["source_start"]) / picture["speed"] == 2


def test_operator_cannot_change_clips_on_protected_template_track():
    bridge = OperatorBridge()
    bridge.set_context({"state": {
        "segments": [{"id": "identity", "kind": "title", "track_id": "v2", "text": "Series identity", "timeline_start": 0, "duration": 2}],
        "tracks": [{"id": "v2", "kind": "video", "locked": True, "template_locked": True}],
        "assets": [],
    }})

    result = bridge.add({"actions": [
        {"type": "set_clip", "clip_id": "identity", "patch": {"text": "Changed"}},
        {"type": "delete_clip", "clip_id": "identity"},
    ]})

    assert len(result["state"]["segments"]) == 1
    assert result["state"]["segments"][0]["text"] == "Series identity"


def test_episode_template_validation_reports_original_replaced_and_missing_slots():
    state = {
        "episode_template": {
            "question_slot_id": "question", "payoff_slot_id": "payoff",
            "slots": [
                {"id": "hook", "label": "Hook", "required": True},
                {"id": "question", "label": "Question", "required": True},
                {"id": "payoff", "label": "Payoff", "required": True},
            ],
        },
        "segments": [
            {"kind": "video", "slot_id": "hook", "template_source": True, "enabled": True},
            {"kind": "video", "slot_id": "question", "template_source": False, "enabled": True},
        ],
    }

    status = validate_episode_template(state)

    assert status["valid"] is False
    assert status["missing_slot_ids"] == ["payoff"]
    assert [item["status"] for item in status["slots"]] == ["original", "replaced", "missing"]
    assert status["question_present"] is True
    assert status["payoff_present"] is False


def test_duplicates_encoded_episode_template_with_linked_media_and_next_part_number(tmp_path: Path):
    source_id = "example-episode-template"
    output = tmp_path / "out" / source_id
    media = tmp_path / "work" / source_id / "media"
    output.mkdir(parents=True)
    media.mkdir(parents=True)
    (output / "part-2.mp4").write_bytes(b"preview")
    (media / "camera.mov").write_bytes(b"camera")
    (tmp_path / "work" / source_id / "VIRAL_FORMAT.md").write_text("format")
    state = {
        "version": 2, "project_id": source_id, "media": "part-2.mp4", "assets": [], "tracks": [],
        "episode_template": {"episode_number": 2, "slots": [{"id": "hook"}]},
        "segments": [{"id": "part", "kind": "caption", "template_field": "episode_number", "text": "Part two.", "label": "Part two."}],
    }
    document = {
        "format": "llm-video-editor-project", "version": 1, "project_id": source_id,
        "name": "Example Episode Template", "active_media": "part-2.mp4", "edits": {"part-2.mp4": state},
    }
    (tmp_path / "work" / source_id / f"{source_id}.llmve").write_text(json.dumps(document))

    project = duplicate_editor_project(tmp_path, source_id, "Example Series — Part 3")
    duplicate = json.loads((tmp_path / "work" / project["id"] / f"{project['id']}.llmve").read_text())
    duplicated_state = duplicate["edits"]["part-2.mp4"]

    assert project["name"] == "Example Series — Part 3"
    assert (tmp_path / "out" / project["id"] / "part-2.mp4").is_symlink()
    assert (tmp_path / "work" / project["id"] / "media" / "camera.mov").is_symlink()
    assert (tmp_path / "work" / project["id"] / "VIRAL_FORMAT.md").read_text() == "format"
    assert duplicated_state["episode_template"]["episode_number"] == 3
    assert duplicated_state["segments"][0]["text"] == "Part 3"
    assert duplicate["template_origin"]["project_id"] == source_id


def _bridge_state(project_id: str, segment_ids: list[str]) -> dict:
    return {
        "version": 2,
        "project_id": project_id,
        "media": "opening.mov",
        "segments": [
            {"id": segment_id, "kind": "title", "timeline_start": index, "duration": 1, "text": segment_id}
            for index, segment_id in enumerate(segment_ids)
        ],
        "tracks": [{"id": "v1", "kind": "video"}],
        "assets": [],
    }


def _disk_backed_bridge(tmp_path: Path, monkeypatch, states: dict[str, dict]):
    saves = []

    def fake_load(_root, project_id, _media_id=None):
        return json.loads(json.dumps(states[project_id]))

    def fake_save(_root, project_id, _media_id, state):
        states[project_id] = json.loads(json.dumps(state))
        saves.append(project_id)
        return json.loads(json.dumps(state))

    monkeypatch.setattr("video_pipeline.editor.load_editor_state", fake_load)
    monkeypatch.setattr("video_pipeline.editor.save_editor_state", fake_save)
    return OperatorBridge(tmp_path), saves


def test_reload_and_second_tab_cannot_replace_saved_project_state(tmp_path: Path, monkeypatch):
    clean = _bridge_state("example-project", ["canonical"])
    stale = _bridge_state("example-project", ["canonical", "operator-obsolete"])
    bridge, _saves = _disk_backed_bridge(tmp_path, monkeypatch, {"example-project": clean})

    old_tab = bridge.set_context({
        "client_id": "browser", "page_id": "old-page", "focused": False,
        "project": {"id": "example-project"}, "state": stale,
    })
    refreshed_tab = bridge.set_context({
        "client_id": "browser", "page_id": "new-page", "focused": True,
        "project": {"id": "example-project"}, "state": clean,
    })
    bridge.set_context({
        "client_id": "browser", "page_id": "old-page", "focused": False,
        "project": {"id": "example-project"}, "state": stale,
    })

    assert [item["id"] for item in old_tab["state"]["segments"]] == ["canonical"]
    assert [item["id"] for item in refreshed_tab["state"]["segments"]] == ["canonical"]
    assert bridge.context()["page_id"] == "new-page"
    assert [item["id"] for item in bridge.context()["state"]["segments"]] == ["canonical"]


def test_operator_batch_is_saved_once_before_acknowledgement(tmp_path: Path, monkeypatch):
    bridge, saves = _disk_backed_bridge(tmp_path, monkeypatch, {"example-project": _bridge_state("example-project", [])})
    context = bridge.set_context({"page_id": "page", "project": {"id": "example-project"}})
    batch = {
        "project_id": "example-project", "base_revision": context["revision"], "batch_id": "add-opening-title",
        "actions": [{"type": "add_title", "text": "Opening", "timeline_start": 0, "duration": 2}],
    }

    first = bridge.add(batch)
    retry = bridge.add(batch)

    assert first == retry
    assert saves == ["example-project"]
    assert first["cursor"] == 1
    assert len(first["state"]["segments"]) == 1
    assert len(bridge.poll(0, "example-project")["actions"]) == 1


def test_failed_operator_save_does_not_advance_or_acknowledge(tmp_path: Path, monkeypatch):
    bridge, _saves = _disk_backed_bridge(tmp_path, monkeypatch, {"example-project": _bridge_state("example-project", ["canonical"])})
    context = bridge.set_context({"page_id": "page", "project": {"id": "example-project"}})
    monkeypatch.setattr("video_pipeline.editor.save_editor_state", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        bridge.add({
            "project_id": "example-project", "base_revision": context["revision"], "batch_id": "delete-canonical",
            "actions": [{"type": "delete_clip", "clip_id": "canonical"}],
        })

    polled = bridge.poll(0, "example-project")
    assert polled["cursor"] == 0
    assert polled["actions"] == []
    assert [item["id"] for item in polled["state"]["segments"]] == ["canonical"]
    assert polled["revision"] == context["revision"]


def test_project_switching_scopes_context_state_and_actions(tmp_path: Path, monkeypatch):
    states = {"example-project": _bridge_state("example-project", ["example-project-clip"]), "other": _bridge_state("other", ["other-clip"])}
    bridge, _saves = _disk_backed_bridge(tmp_path, monkeypatch, states)
    bridge.set_context({"page_id": "example-project-page", "focused": True, "project": {"id": "example-project"}})
    other = bridge.set_context({"page_id": "other-page", "focused": True, "project": {"id": "other"}})
    bridge.set_context({"page_id": "example-project-page", "focused": False, "project": {"id": "example-project"}})

    assert bridge.context()["project_id"] == "other"
    assert [item["id"] for item in bridge.context()["state"]["segments"]] == ["other-clip"]
    assert [item["id"] for item in bridge.poll(0, "example-project")["state"]["segments"]] == ["example-project-clip"]
    assert other["revision"] == bridge.poll(0, "other")["revision"]


def test_creates_a_project_from_media_in_an_empty_library(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("video_pipeline.editor.probe", lambda path: _metadata())

    project, asset = create_project_from_media(tmp_path, "My First Cut", "camera.mov", BytesIO(b"camera"), 6)

    assert project["id"] == "my-first-cut"
    assert asset["id"] == "camera.mov"
    assert (tmp_path / "out" / "my-first-cut" / "camera.mov").read_bytes() == b"camera"


def test_dropped_operator_file_is_copied_to_an_accessible_path(tmp_path: Path):
    uploaded = import_operator_file(tmp_path, "notes.txt", BytesIO(b"editing notes"), 13)

    assert Path(uploaded["path"]).read_bytes() == b"editing notes"
    assert Path(uploaded["path"]).parent == tmp_path / "work" / "operator-uploads"

    folder = tmp_path / "video folder"
    folder.mkdir()
    assert resolve_operator_paths(tmp_path, [folder.name])[folder.name] == str(folder)


def test_trash_project_media_preserves_work_files(tmp_path: Path, monkeypatch):
    output = tmp_path / "out" / "sample-edit"
    output.mkdir(parents=True)
    (output / "preview.mp4").write_bytes(b"video")
    work = tmp_path / "work" / "sample-edit"
    work.mkdir(parents=True)
    (work / "manifest.json").write_text("{}")
    trash = tmp_path / "Trash"
    trash.mkdir()
    monkeypatch.setattr("video_pipeline.editor.Path.home", lambda: tmp_path)
    (tmp_path / ".Trash").mkdir()

    destination = trash_project_media(tmp_path, "sample-edit")

    assert destination == tmp_path / ".Trash" / "sample-edit"
    assert (destination / "preview.mp4").is_file()
    assert not output.exists()
    assert (work / "manifest.json").is_file()
