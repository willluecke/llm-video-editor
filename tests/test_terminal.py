import base64
import re
import time
from pathlib import Path

from video_pipeline import terminal as terminal_module


def _fake_codex(tmp_path: Path) -> Path:
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"login status\" ]; then echo 'Logged in using ChatGPT'; exit 0; fi\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'codex-cli test'; exit 0; fi\n"
        "printf 'ARGS:%s\\r\\n' \"$*\"\n"
        "printf 'Do you trust this directory? 1. Yes, continue\\r\\n'\n"
        "IFS= read -r trust_answer\n"
        "printf 'TRUSTED\\r\\n'\n"
        "while IFS= read -r line; do printf 'ECHO:%s\\r\\n' \"$line\"; done\n"
    )
    executable.chmod(0o755)
    return executable


def _wait_for_output(session: terminal_module.TerminalSession, needle: bytes) -> bytes:
    deadline = time.monotonic() + 3
    output = b""
    while time.monotonic() < deadline:
        encoded = session.output(0)["data"]
        output = base64.b64decode(encoded)
        if needle in output:
            return output
        time.sleep(0.02)
    return output


def test_terminal_defaults_to_approval_aware_and_full_access_is_explicit(tmp_path: Path, monkeypatch):
    executable = _fake_codex(tmp_path)
    monkeypatch.setattr(terminal_module, "find_codex", lambda: executable)

    status = terminal_module.codex_status()
    assert status["authenticated"] is True
    assert status["label"] == "ChatGPT subscription"

    session = terminal_module.TerminalSession(tmp_path, columns=70, rows=22, api_token="test-token")
    try:
        output = _wait_for_output(session, b"check_for_update_on_startup=false")
        assert b"--yolo" not in output
        assert b"--config check_for_update_on_startup=false" in output
        assert b"TRUSTED" in _wait_for_output(session, b"TRUSTED")

        session.write("hello from the editor\r")
        assert b"ECHO:hello from the editor" in _wait_for_output(session, b"ECHO:hello from the editor")
    finally:
        session.stop()

    full_access = terminal_module.TerminalSession(tmp_path, columns=70, rows=22, full_access=True)
    try:
        assert b"--yolo" in _wait_for_output(full_access, b"check_for_update_on_startup=false")
    finally:
        full_access.stop()


def test_operator_prompt_understands_encoded_slot_replacement():
    assert "replace_clip(clip_id, asset_id)" in terminal_module.OPERATOR_INSTRUCTIONS
    assert "preserves the encoded slot timing" in terminal_module.OPERATOR_INSTRUCTIONS
    assert "Do not modify protected template tracks" in terminal_module.OPERATOR_INSTRUCTIONS


def test_terminal_manager_reattaches_until_explicit_restart(tmp_path: Path, monkeypatch):
    sessions = []

    class FakeSession:
        def __init__(self, root, columns, rows, base_url, api_token, full_access):
            self.id = f"session-{len(sessions) + 1}"
            self.alive = True
            self.resizes = [(columns, rows)]
            self.api_token = api_token
            self.full_access = full_access
            sessions.append(self)

        def resize(self, columns, rows):
            self.resizes.append((columns, rows))

        def stop(self):
            self.alive = False

    monkeypatch.setattr(terminal_module, "TerminalSession", FakeSession)
    manager = terminal_module.TerminalManager(tmp_path, api_token="secret", full_access=True)

    first, reused = manager.attach_or_start(70, 22)
    attached, attached_reused = manager.attach_or_start(90, 30)

    assert reused is False
    assert first.api_token == "secret"
    assert first.full_access is True
    assert attached_reused is True
    assert attached is first
    assert attached.resizes[-1] == (90, 30)

    replacement = manager.restart(80, 25)

    assert first.alive is False
    assert replacement.id != first.id
    assert replacement.alive is True
    manager.close()


def test_editor_tab_unload_does_not_stop_operator():
    app_source = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "app.js").read_text()

    assert 'addEventListener("pagehide"' not in app_source
    assert "sendBeacon(`/api/terminal/" not in app_source
    assert "/api/terminal/restart" in app_source


def test_program_monitor_preloads_and_swaps_the_next_clip():
    ui_root = Path(__file__).parents[1] / "src" / "video_pipeline" / "ui"
    html = (ui_root / "index.html").read_text()
    app_source = (ui_root / "app.js").read_text()

    assert html.count('class="preview-video') == 5
    assert html.count('preload="auto"') == 5
    assert "function preparePreviewQueue" in app_source
    assert "followingVideoItems(item)" in app_source
    assert "function activatePreparedPreview" in app_source
    assert "incoming.readyState < HTMLMediaElement.HAVE_CURRENT_DATA" in app_source
    assert "function primePreviewPoolPlayback" in app_source
    assert "function maybeStartPreviewPreroll" in app_source
    assert "incoming.volume = 0" in app_source
    assert "function findRenderedPreviewAsset" in app_source
    assert "function activateRenderedPreview" in app_source
    assert "previewState.usingProxy" in app_source
    assert "requestAnimationFrame(tick)" in app_source
    assert "video.addEventListener(\"ended\"" in app_source


def test_selected_video_clip_edges_support_double_click_navigation():
    app_source = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "app.js").read_text()

    assert 'clip.addEventListener("dblclick"' in app_source
    assert 'item.segment.kind !== "video" || item.segment.id !== app.selectedId' in app_source
    assert "if (selectionChanged) renderTimeline();" in app_source
    assert "pointerX <= bounds.width / 2 ? item.start : item.end" in app_source


def test_drag_trim_completion_moves_playhead_to_changed_boundary():
    app_source = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "app.js").read_text()
    trim_source = app_source.split("function beginTrim", 1)[1].split("function renderInspector", 1)[0]

    assert "edge, changed: false" in trim_source
    assert "session.changed = true;" in trim_source
    assert 'const boundary = edge === "left"' in trim_source
    assert "Number(segment.timeline_start || 0) + durationOf(segment)" in trim_source
    assert "seekEditTime(boundary);" in trim_source


def test_same_track_video_overlap_is_prevented_for_move_add_trim_and_inspector():
    app_source = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "app.js").read_text()

    assert "function nonOverlappingVideoStart(segment, requestedStart" in app_source
    assert "function clampVideoSourceEnd(segment, requestedSourceEnd)" in app_source
    assert "nonOverlappingVideoStart(segment, timelineStart, videoTrackId)" in app_source
    assert "nonOverlappingVideoStart(segment, start, track.id)" in app_source
    assert "segment.source_end = clampVideoSourceEnd" in app_source
    assert 'toast("Video snapped beside the nearest clip")' in app_source
    assert 'segment.kind === "video" ? nonOverlappingVideoStart(segment, requested)' in app_source


def test_titles_and_captions_use_separate_timeline_subrows():
    styles = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "styles.css").read_text()
    app_source = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "app.js").read_text()

    assert ".track.video-track { height: 140px; }" in styles
    assert ".video-track .clip.video { top: 6px; height: 60px; }" in styles
    assert ".clip.title { top: 72px; }" in styles
    assert ".clip.caption { top: 106px; }" in styles
    assert ".clip.title, .clip.caption { height: 29px;" in styles
    assert 'track.kind === "video" ? 140 : 72' in app_source
    assert "function timelineDurationLabel(seconds)" in app_source
    assert "safe.toFixed(2)" in app_source


def test_modern_caption_style_is_consistent_in_web_native_and_rendered_output():
    root = Path(__file__).parents[1]
    styles = (root / "src" / "video_pipeline" / "ui" / "styles.css").read_text()
    app_source = (root / "src" / "video_pipeline" / "ui" / "app.js").read_text()
    editor_source = (root / "src" / "video_pipeline" / "editor.py").read_text()
    native_source = (root / "macos" / "NativeApp" / "Sources" / "LLMVideoEditorApp" / "NativePlayerView.swift").read_text()

    web_opacity = re.search(r"\.title-overlay\.caption \{[^}]*background: rgb\(0 0 0 / (\d+)%\)", styles)
    render_opacity = re.search(r"boxcolor=black@([0-9.]+):boxborderw", editor_source)
    native_opacity = re.search(r"captionBackgroundLayer\.backgroundColor = NSColor\.black\.withAlphaComponent\(([0-9.]+)\)", native_source)

    assert web_opacity and render_opacity and native_opacity
    assert float(web_opacity.group(1)) / 100 == float(render_opacity.group(1)) == float(native_opacity.group(1)) == 0.70
    assert ".title-overlay.caption { top: auto; bottom: 14%; min-height: 1.6em; padding: 8px 12px; border-radius: 8px;" in styles
    assert 'const previewCap = textClip.kind === "caption" ? 48 : 46;' in app_source
    assert 'font_size: kind === "title" ? 64 : 72' in app_source
    assert 'captionBackgroundLayer.cornerRadius = 8' in native_source
    assert 'y: backgroundY + verticalPadding' in native_source
    assert 'captionLayer.truncationMode = .none' in native_source
    assert 'paragraphStyle.lineBreakMode = .byWordWrapping' in native_source
    assert 'captionLayer.string = attributedText' in native_source


def test_real_timeline_gaps_are_selected_then_ripple_closed_with_delete():
    ui_root = Path(__file__).parents[1] / "src" / "video_pipeline" / "ui"
    app_source = (ui_root / "app.js").read_text()
    styles = (ui_root / "styles.css").read_text()

    assert "function mediaGapsForTrack(track)" in app_source
    assert "function closeTimelineGap(gap)" in app_source
    assert "mediaGapsForTrack(track).forEach(gap => lane.appendChild(gapElement" in app_source
    assert "app.selectedGap = { start: gap.start, end: gap.end };" in app_source
    assert "if (app.selectedGap) {\n    closeTimelineGap(app.selectedGap);" in app_source
    assert 'if (event.key === "Backspace" || event.key === "Delete") deleteSelected();' in app_source
    assert "setTimeout(() => closeTimelineGap" not in app_source
    assert ".timeline-gap.selected" in styles


def test_shortening_video_trims_only_captions_still_inside_its_new_end():
    app_source = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "app.js").read_text()

    assert "function trimCaptionsAtVideoEnd(video, previousTimelineEnd)" in app_source
    assert "const stillIncluded = captionStart >= videoStart - .001 && captionStart < videoEnd - .001;" in app_source
    assert "if (stillIncluded && crossedTrimmedEnd)" in app_source
    assert "trimCaptionsAtVideoEnd(segment, session.originalTimeline + session.originalDuration);" in app_source


def test_native_macos_monitor_uses_avfoundation_composition():
    root = Path(__file__).parents[1]
    app_source = (root / "src" / "video_pipeline" / "ui" / "app.js").read_text()
    swift_root = root / "macos" / "NativeApp" / "Sources" / "LLMVideoEditorApp"
    composition = (swift_root / "CompositionEngine.swift").read_text()
    delegate = (swift_root / "AppDelegate.swift").read_text()

    assert 'get("native") === "1"' in app_source
    assert 'type: "timeline"' in app_source
    assert "AVMutableComposition()" in composition
    assert "AVPlayerItem(asset: composition)" in composition
    assert "scaleTimeRange" in composition
    assert "AVMutableAudioMix" in composition
    assert "VideoCompositionValidator" in composition
    assert "(finite * 1_000_000).rounded()" in composition
    assert "captureDiagnosticFrame" in composition
    assert 'userContent.add(self, name: "nativeEditor")' in delegate
    assert "playerView.configure(snapshot)" in delegate


def test_viewer_refresh_reloads_project_and_operator_reconciliation_is_retryable():
    app_source = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "app.js").read_text()

    assert "async function refreshCurrentProject()" in app_source
    assert "await selectProject(projectId, mediaId);" in app_source
    assert 'els.refresh.addEventListener("click", refreshCurrentProject);' in app_source

    reconcile = app_source.split("function reconcileOperatorState", 1)[1].split("async function pollOperatorActions", 1)[0]
    assert reconcile.index("syncNativeTimeline(revision);") < reconcile.index("operatorState.revision = revision;")
    assert "Viewer could not apply edit revision ${revision}; retrying" in reconcile

    polling = app_source.split("async function pollOperatorActions", 1)[1].split("async function initializeOperatorBridge", 1)[0]
    assert "if (reconciled || Number(data?.revision || 0) <= operatorState.revision) operatorState.cursor = data.cursor;" in polling

    initialize = app_source.split("async function initializeApp", 1)[1]
    assert initialize.index("await initializeOperatorBridge();") < initialize.index("await startTerminal();")


def test_operator_context_updates_are_throttled_without_starving_trim_dirty_state():
    app_source = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "app.js").read_text()
    scheduler = app_source.split("function scheduleOperatorContext()", 1)[1].split("async function syncOperatorContext", 1)[0]

    assert "if (operatorState.contextTimer) return;" in scheduler
    assert "operatorState.contextTimer = null;" in scheduler
    assert "clearTimeout(operatorState.contextTimer);" not in scheduler


def test_encoded_episode_templates_expose_slots_replacement_validation_and_duplication():
    ui_root = Path(__file__).parents[1] / "src" / "video_pipeline" / "ui"
    html = (ui_root / "index.html").read_text()
    app_source = (ui_root / "app.js").read_text()
    styles = (ui_root / "styles.css").read_text()

    assert 'id="duplicate-episode-button"' in html
    assert 'id="template-contract"' in html
    assert 'id="replacement-media"' in html
    assert 'id="replace-media-button"' in html
    assert "function currentTemplateStatus()" in app_source
    assert "function renderTemplateContract()" in app_source
    assert "function fitReplacementToSegment(segment, asset)" in app_source
    assert "function replaceSelectedMedia()" in app_source
    assert "async function duplicateEpisode()" in app_source
    assert "This episode is missing required story beats" in app_source
    assert 'action.type === "replace_clip"' in app_source
    assert ".template-slot.missing" in styles
    assert ".clip.template-original" in styles


def test_native_monitor_position_retries_after_layout_is_ready():
    app_source = (Path(__file__).parents[1] / "src" / "video_pipeline" / "ui" / "app.js").read_text()

    assert "function ensureNativeMonitorRect(remainingAttempts = 20)" in app_source
    assert "if (rect.width <= 1 || rect.height <= 1) return false;" in app_source
    assert "setTimeout(() => ensureNativeMonitorRect(remainingAttempts - 1), 100)" in app_source
    assert "new ResizeObserver(() => ensureNativeMonitorRect()).observe(els.videoFrame)" in app_source


def test_operator_is_forbidden_from_managing_editor_tabs():
    assert "Never open, close, reload, or manage browser tabs" in terminal_module.OPERATOR_INSTRUCTIONS
