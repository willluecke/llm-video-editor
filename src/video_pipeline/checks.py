from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .assets import derived_cache_root, load_asset_manifest, sync_asset_manifest


def segment_duration(segment: dict[str, Any]) -> float:
    if segment.get("kind") in {"title", "caption"}:
        return max(0.0, float(segment.get("duration", segment.get("source_end", 0))))
    return max(
        0.0,
        (float(segment.get("source_end", 0)) - float(segment.get("source_start", 0)))
        / max(0.0001, float(segment.get("speed", 1))),
    )


def _issue(
    code: str,
    severity: str,
    message: str,
    *,
    node: str | None = None,
    ranges: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        **({"node": node} if node else {}),
        **({"ranges": ranges} if ranges else {}),
    }


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if not merged or start > merged[-1][1] + 0.0005:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _gaps(window: tuple[float, float], intervals: list[tuple[float, float]], frame: float) -> list[dict[str, float]]:
    start, end = window
    cursor = start
    gaps: list[dict[str, float]] = []
    for left, right in _merge_intervals(intervals):
        left = max(start, left)
        right = min(end, right)
        if left - cursor >= frame:
            gaps.append({"start": round(cursor, 3), "end": round(left, 3)})
        cursor = max(cursor, right)
    if end - cursor >= frame:
        gaps.append({"start": round(cursor, 3), "end": round(end, 3)})
    return gaps


def _template_issues(state: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    from .editor import validate_episode_template

    template = state.get("episode_template")
    if not isinstance(template, dict):
        return []
    issues: list[dict[str, Any]] = []
    status = validate_episode_template(state) or {}
    for slot_id in status.get("missing_slot_ids", []):
        issues.append(_issue("missing-template-slot", "error", f"Required story slot is missing: {slot_id}", node=slot_id))
    if template.get("question_slot_id") and not status.get("question_present"):
        issues.append(_issue("missing-question", "error", "The reusable mechanism has no question/open-loop footage"))
    if template.get("payoff_slot_id") and not status.get("payoff_present"):
        issues.append(_issue("missing-payoff", "error", "The reusable mechanism has no payoff/result footage"))

    slots = {str(item.get("id")): item for item in template.get("slots", []) if isinstance(item, dict)}
    question = slots.get(str(template.get("question_slot_id")))
    payoff = slots.get(str(template.get("payoff_slot_id")))
    if question and payoff and float(question.get("start", 0)) >= float(payoff.get("start", 0)):
        issues.append(_issue("question-after-payoff", "error", "The question must begin before the payoff"))

    constraints = template.get("constraints") if isinstance(template.get("constraints"), dict) else {}
    resolution = constraints.get("resolution")
    if isinstance(resolution, list) and len(resolution) == 2 and list(state.get("resolution", [])) != resolution:
        issues.append(_issue("format-resolution", "error", f"Expected {resolution[0]}×{resolution[1]}, got {state.get('resolution')}"))
    expected_fps = constraints.get("fps")
    if expected_fps is not None and abs(float(state.get("fps", 0)) - float(expected_fps)) > 0.01:
        issues.append(_issue("format-fps", "error", f"Expected {expected_fps} fps, got {state.get('fps')}"))
    runtime = constraints.get("runtime") if isinstance(constraints.get("runtime"), dict) else {}
    minimum = float(runtime.get("min", 0))
    maximum = float(runtime.get("max", 0))
    if minimum and duration < minimum:
        issues.append(_issue("runtime-short", "warning", f"Runtime {duration:.3f}s is below the {minimum:.3f}s format floor"))
    if maximum and duration > maximum:
        issues.append(_issue("runtime-long", "warning", f"Runtime {duration:.3f}s exceeds the {maximum:.3f}s format ceiling"))

    track_map = {str(item.get("id")): item for item in state.get("tracks", [])}
    for track_id in constraints.get("required_locked_track_ids", template.get("locked_track_ids", [])):
        if not track_map.get(str(track_id), {}).get("locked"):
            issues.append(_issue("identity-track-unlocked", "error", f"Series identity track {track_id} must remain locked", node=str(track_id)))
    for track_id in constraints.get("required_reserved_track_ids", template.get("reserved_track_ids", [])):
        if not track_map.get(str(track_id), {}).get("template_reserved"):
            issues.append(_issue("reserved-track-changed", "warning", f"Reusable audio track {track_id} is no longer reserved", node=str(track_id)))

    tolerance = max(0.0, float(constraints.get("slot_timing_tolerance", 0.25)))
    for slot in template.get("slots", []):
        if not isinstance(slot, dict):
            continue
        clips = [
            item for item in state.get("segments", [])
            if item.get("enabled", True) and item.get("kind") == "video" and item.get("slot_id") == slot.get("id")
        ]
        if not clips:
            continue
        actual_start = min(float(item.get("timeline_start", 0)) for item in clips)
        actual_end = max(float(item.get("timeline_start", 0)) + segment_duration(item) for item in clips)
        expected_start = float(slot.get("start", actual_start))
        expected_end = float(slot.get("end", actual_end))
        if abs(actual_start - expected_start) > tolerance or abs(actual_end - expected_end) > tolerance:
            issues.append(_issue(
                "slot-pacing-drift",
                "warning",
                f"{slot.get('label', slot.get('id'))} spans {actual_start:.3f}–{actual_end:.3f}s; expected {expected_start:.3f}–{expected_end:.3f}s ±{tolerance:.3f}s",
                node=str(slot.get("id")),
            ))
    return issues


def check_state(
    root: Path,
    project_id: str,
    state: dict[str, Any],
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = manifest or load_asset_manifest(root, project_id)
    records = {str(item.get("library_id")): item for item in manifest.get("assets", [])}
    issues: list[dict[str, Any]] = []
    tracks = {str(item.get("id")): item for item in state.get("tracks", [])}
    enabled = [item for item in state.get("segments", []) if item.get("enabled", True)]
    duration = max(
        (float(item.get("timeline_start", 0)) + segment_duration(item) for item in enabled),
        default=0.0,
    )
    frame = 1 / max(1.0, float(state.get("fps", 30)))

    visual_intervals: list[tuple[float, float]] = []
    by_track: dict[str, list[tuple[float, float, dict[str, Any]]]] = {}
    referenced: set[str] = set()
    for segment in enabled:
        node = str(segment.get("id", "")) or None
        kind = str(segment.get("kind", ""))
        start = float(segment.get("timeline_start", 0))
        clip_duration = segment_duration(segment)
        end = start + clip_duration
        if clip_duration < frame:
            issues.append(_issue("zero-duration", "error", "Clip is shorter than one frame", node=node))
        if kind in {"video", "audio"}:
            asset_id = str(segment.get("asset_id", ""))
            referenced.add(asset_id)
            record = records.get(asset_id)
            if not record or record.get("status") != "online":
                issues.append(_issue("source-missing", "error", f"Media is missing: {asset_id}", node=node))
            source_start = float(segment.get("source_start", 0))
            source_end = float(segment.get("source_end", 0))
            if source_start < 0 or source_end <= source_start:
                issues.append(_issue("invalid-source-range", "error", f"Invalid source range {source_start:.3f}–{source_end:.3f}s", node=node))
            available = float((record or {}).get("probe", {}).get("duration") or 0)
            if available and source_end > available + frame:
                issues.append(_issue("source-out-of-bounds", "error", f"Source out {source_end:.3f}s exceeds {asset_id} duration {available:.3f}s", node=node))
            if float(segment.get("speed", 1)) <= 0:
                issues.append(_issue("invalid-speed", "error", "Playback speed must be greater than zero", node=node))
        track = tracks.get(str(segment.get("track_id", "")), {})
        if kind in {"video", "title", "caption"} and not track.get("muted"):
            visual_intervals.append((start, end))
        if kind == "video" and not track.get("muted"):
            by_track.setdefault(str(segment.get("track_id", "")), []).append((start, end, segment))
        if (segment.get("locked") or track.get("locked")) and not segment.get("template_source", False) and kind != "title":
            issues.append(_issue("protected-content-changed", "warning", "A clip on a protected layer is marked as replacement content", node=node))

    for asset_id, record in records.items():
        if record.get("status") == "missing" and asset_id in referenced:
            continue
        if record.get("status") == "missing":
            issues.append(_issue("unused-source-missing", "warning", f"Unused library media is missing: {asset_id}", node=asset_id))

    gaps = _gaps((0.0, duration), visual_intervals, frame) if duration else []
    if not visual_intervals:
        issues.append(_issue("no-visuals", "error", "The timeline has no enabled visual content"))
    elif gaps:
        issues.append(_issue("black-frame-schedule", "error", f"No visual is scheduled in {len(gaps)} timeline span(s)", ranges=gaps))

    for track_id, clips in by_track.items():
        ordered = sorted(clips, key=lambda item: item[0])
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] < previous[1] - frame / 2:
                issues.append(_issue(
                    "same-track-overlap",
                    "warning",
                    f"Video clips overlap on {track_id} from {current[0]:.3f}s to {min(previous[1], current[1]):.3f}s",
                    node=str(current[2].get("id", "")),
                ))

    issues.extend(_template_issues(state, duration))
    errors = sum(item["severity"] == "error" for item in issues)
    warnings = sum(item["severity"] == "warning" for item in issues)
    return {
        "project_id": project_id,
        "valid": errors == 0,
        "stats": {
            "assets": len(records),
            "referenced_assets": len(referenced),
            "tracks": len(tracks),
            "segments": len(enabled),
            "duration": round(duration, 3),
            "errors": errors,
            "warnings": warnings,
        },
        "issues": issues,
    }


_BLACK = re.compile(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)")
_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")
_LOUDNESS = re.compile(r"^\s*I:\s*(-?[0-9.]+)\s+LUFS", re.MULTILINE)
_PEAK = re.compile(r"^\s*Peak:\s*(-?[0-9.]+)\s+dBFS", re.MULTILINE)


def deep_media_checks(render: Path) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for deep validation")
    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-nostats", "-i", str(render),
            "-vf", "blackdetect=d=0.12:pix_th=0.10",
            "-af", "silencedetect=n=-45dB:d=1.5,ebur128=peak=true",
            "-f", "null", "-",
        ],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError("FFmpeg deep validation failed")
    black = [
        {"start": round(float(match.group(1)), 3), "end": round(float(match.group(2)), 3)}
        for match in _BLACK.finditer(result.stderr)
    ]
    silence: list[dict[str, float]] = []
    pending: float | None = None
    for line in result.stderr.splitlines():
        start = _SILENCE_START.search(line)
        end = _SILENCE_END.search(line)
        if start:
            pending = float(start.group(1))
        if end and pending is not None:
            silence.append({"start": round(pending, 3), "end": round(float(end.group(1)), 3)})
            pending = None
    loudness = [float(value) for value in _LOUDNESS.findall(result.stderr)]
    peaks = [float(value) for value in _PEAK.findall(result.stderr)]
    return {
        "black_ranges": black,
        "silence_ranges": silence,
        "integrated_lufs": loudness[-1] if loudness else None,
        "true_peak_dbfs": peaks[-1] if peaks else None,
    }


def check_project(
    root: Path,
    project_id: str,
    *,
    media_id: str | None = None,
    deep: bool = False,
    refresh_capture: bool = False,
) -> dict[str, Any]:
    from .editor import load_editor_state, probe

    state = load_editor_state(root, project_id, media_id)
    manifest = sync_asset_manifest(root, project_id, state=state, probe_media=probe)
    report = check_state(root, project_id, state, manifest=manifest)
    if deep:
        from .inspection import capture_project

        capture = capture_project(root, project_id, media_id=media_id, refresh=refresh_capture)
        rendered = deep_media_checks(Path(capture["render"]))
        report["deep"] = {**rendered, "capture": capture}
        if rendered["black_ranges"]:
            report["issues"].append(_issue("rendered-black-frames", "error", "Rendered output contains black frames", ranges=rendered["black_ranges"]))
        for item in rendered["silence_ranges"]:
            if item["end"] - item["start"] >= 2.0:
                report["issues"].append(_issue("long-silence", "warning", "Rendered output contains a long silent span", ranges=[item]))
        lufs = rendered["integrated_lufs"]
        if lufs is not None and not -18.0 <= lufs <= -9.0:
            report["issues"].append(_issue("loudness", "warning", f"Integrated loudness is {lufs:.1f} LUFS; expected roughly -18 to -9 LUFS"))
        peak = rendered["true_peak_dbfs"]
        if peak is not None and peak > -0.5:
            report["issues"].append(_issue("audio-peak", "warning", f"True peak is {peak:.1f} dBFS and may clip after platform encoding"))
        report["stats"]["errors"] = sum(item["severity"] == "error" for item in report["issues"])
        report["stats"]["warnings"] = sum(item["severity"] == "warning" for item in report["issues"])
        report["valid"] = report["stats"]["errors"] == 0

    report["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()[:20]
    cache = derived_cache_root(root, project_id) / "validation"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{digest}.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    latest = cache / "latest.json"
    latest.write_text(json.dumps(report, indent=2) + "\n")
    report["report"] = str(path)
    return report


def blocking_preflight_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
    blocking = {"source-missing", "invalid-source-range", "source-out-of-bounds", "invalid-speed", "no-visuals"}
    return [item for item in report.get("issues", []) if item.get("severity") == "error" and item.get("code") in blocking]
