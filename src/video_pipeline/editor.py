from __future__ import annotations

import json
import mimetypes
import re
import secrets
import shutil
import subprocess
import textwrap
import threading
import uuid
import webbrowser
from datetime import datetime
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .assets import attach_manifest_to_state, sync_asset_manifest
from .checks import blocking_preflight_issues, check_state
from .project_docs import ensure_project_docs
from .terminal import TerminalManager, codex_status


UI_DIR = Path(__file__).with_name("ui")
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".aac", ".flac"}
MEDIA_SUFFIXES = VIDEO_SUFFIXES | AUDIO_SUFFIXES
PROJECT_FILE_VERSION = 1
EXPORT_PRESETS = {"source", "youtube", "vertical", "draft"}


def _title(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def _slug(value: str, fallback: str = "project") -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or fallback


def _project_path(root: Path, project_id: str) -> Path:
    return root / "work" / project_id / f"{project_id}.llmve"


def _read_project_file(root: Path, project_id: str) -> dict[str, Any]:
    path = _project_path(root, project_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=128)
def _probe_cached(path: str, modified_ns: int) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to inspect editor media")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration:stream=index,codec_type,width,height,r_frame_rate,"
                "color_range,color_space,color_transfer,color_primaries"
            ),
            "-of",
            "json",
            path,
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    rate = str(video.get("r_frame_rate") or "0/1")
    numerator, separator, denominator = rate.partition("/")
    try:
        rate_numerator = float(numerator)
        rate_denominator = float(denominator) if separator else 1.0
        fps = rate_numerator / rate_denominator if rate_denominator else 0.0
    except (TypeError, ValueError):
        # ffprobe reports frame rates such as ``N/A`` for audio-only assets.
        # Those files are valid timeline inputs and simply have no video FPS.
        fps = 0.0
    return {
        "duration": round(float(data.get("format", {}).get("duration") or 0), 3),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 3),
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
        "color_range": str(video.get("color_range") or ""),
        "color_space": str(video.get("color_space") or ""),
        "color_transfer": str(video.get("color_transfer") or ""),
        "color_primaries": str(video.get("color_primaries") or ""),
    }


def probe(path: Path) -> dict[str, Any]:
    return _probe_cached(str(path), path.stat().st_mtime_ns)


def _needs_hdr_to_sdr(path: Path | None) -> bool:
    """Return whether a source needs tone mapping before BT.709 compositing."""
    if path is None or not path.is_file():
        return False
    try:
        transfer = str(probe(path).get("color_transfer") or "").lower()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return False
    return transfer in {"arib-std-b67", "smpte2084"}


def discover_projects(root: Path) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    out_dir = root / "out"
    if not out_dir.is_dir():
        return projects
    for directory in sorted((path for path in out_dir.iterdir() if path.is_dir()), key=lambda path: path.name):
        media = sorted(
            (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not media:
            continue
        manifest_path = root / "work" / directory.name / "manifest.json"
        source = None
        if manifest_path.is_file():
            try:
                source = json.loads(manifest_path.read_text()).get("source")
            except (json.JSONDecodeError, OSError):
                pass
        project_file = _read_project_file(root, directory.name)
        projects.append(
            {
                "id": directory.name,
                "name": str(project_file.get("name") or _title(directory.name)),
                "source": source,
                "active_media": project_file.get("active_media"),
                "has_project_file": bool(project_file),
                "media": [
                    {
                        "id": path.name,
                        "name": path.stem.replace("_", " "),
                        "size": path.stat().st_size,
                    }
                    for path in media
                ],
            }
        )
    return projects


def _resolve_media(root: Path, project_id: str, media_id: str | None = None) -> tuple[dict[str, Any], Path]:
    project = next((item for item in discover_projects(root) if item["id"] == project_id), None)
    if not project:
        raise FileNotFoundError(f"Unknown editor project: {project_id}")
    available = {item["id"]: item for item in project["media"]}
    preferred = _read_project_file(root, project_id).get("active_media")
    selected = media_id or (preferred if preferred in available else project["media"][0]["id"])
    if selected not in available:
        raise FileNotFoundError(f"Unknown media version: {selected}")
    return project, root / "out" / project_id / selected


def _resolve_asset(root: Path, project_id: str, asset_id: str) -> Path:
    safe_name = Path(asset_id).name
    if safe_name != asset_id or Path(safe_name).suffix.lower() not in MEDIA_SUFFIXES:
        raise FileNotFoundError("Invalid project media path")
    candidates = [root / "out" / project_id / safe_name, root / "work" / project_id / "media" / safe_name]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Unknown project media: {asset_id}")


def _render_asset_paths(root: Path, project_id: str, state: dict[str, Any]) -> dict[str, Path]:
    """Resolve only media referenced by enabled timeline segments.

    Project asset lists can legitimately lag behind timeline edits: removed exports
    may remain registered, while generated replacement audio may already be in use
    before the media bin refreshes. Rendering should depend on the timeline, not on
    either stale form of bookkeeping.
    """
    asset_ids = dict.fromkeys(
        str(segment["asset_id"])
        for segment in state.get("segments", [])
        if segment.get("enabled", True)
        and segment.get("kind") in {"video", "audio"}
        and segment.get("asset_id")
    )
    return {asset_id: _resolve_asset(root, project_id, asset_id) for asset_id in asset_ids}


def discover_assets(root: Path, project_id: str) -> list[dict[str, Any]]:
    paths: dict[str, Path] = {}
    for directory in (root / "out" / project_id, root / "work" / project_id / "media"):
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
                    paths.setdefault(path.name, path)
    assets: list[dict[str, Any]] = []
    for path in sorted(paths.values(), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            assets.append(_asset_record(project_id, path, probe(path)))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            continue
    return assets


def import_project_media(root: Path, project_id: str, filename: str, source: Any, length: int) -> dict[str, Any]:
    _resolve_media(root, project_id)
    safe_name = Path(filename).name
    if not safe_name or Path(safe_name).suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("Choose a supported video or audio file")
    directory = root / "work" / project_id / "media"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / safe_name
    if destination.exists() or (root / "out" / project_id / safe_name).exists():
        stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
        index = 2
        while destination.exists() or (root / "out" / project_id / destination.name).exists():
            destination = directory / f"{stem}-{index}{suffix}"
            index += 1
    temporary = destination.with_suffix(destination.suffix + ".uploading")
    remaining = length
    try:
        with temporary.open("wb") as handle:
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Media upload ended early")
                handle.write(chunk)
                remaining -= len(chunk)
        temporary.replace(destination)
        asset = _asset_record(project_id, destination, probe(destination))
        manifest = sync_asset_manifest(root, project_id, probe_media=probe)
        attach_manifest_to_state({"project_id": project_id, "assets": [asset]}, manifest)
        return asset
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def create_project_from_media(root: Path, name: str, filename: str, source: Any, length: int) -> tuple[dict[str, Any], dict[str, Any]]:
    safe_name = Path(filename).name
    if not safe_name or Path(safe_name).suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError("Choose an MP4, MOV, M4V, or WebM file")
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or re.sub(r"[^a-z0-9]+", "-", Path(safe_name).stem.lower()).strip("-") or "new-project"
    project_id = base_slug
    index = 2
    while (root / "out" / project_id).exists() or (root / "work" / project_id).exists():
        project_id = f"{base_slug}-{index}"
        index += 1
    directory = root / "out" / project_id
    directory.mkdir(parents=True)
    destination = directory / safe_name
    try:
        remaining = length
        with destination.open("wb") as handle:
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Media upload ended early")
                handle.write(chunk)
                remaining -= len(chunk)
        asset = _asset_record(project_id, destination, probe(destination))
        manifest = sync_asset_manifest(root, project_id, probe_media=probe)
        attach_manifest_to_state({"project_id": project_id, "assets": [asset]}, manifest)
        project = next(item for item in discover_projects(root) if item["id"] == project_id)
        return project, asset
    except Exception:
        destination.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        raise


def duplicate_editor_project(root: Path, source_project_id: str, name: str | None = None) -> dict[str, Any]:
    """Create a lightweight editable episode from an encoded template project."""
    source_project, _ = _resolve_media(root, source_project_id)
    source_document = _read_project_file(root, source_project_id)
    active_media = str(source_document.get("active_media") or source_project.get("active_media") or "")
    source_state = source_document.get("edits", {}).get(active_media) if isinstance(source_document.get("edits"), dict) else None
    if not isinstance(source_state, dict) or not isinstance(source_state.get("episode_template"), dict):
        raise ValueError("This project is not an encoded episode template")

    template = source_state["episode_template"]
    next_episode = max(1, int(template.get("episode_number", 1)) + 1)
    project_name = (name or f"{source_project['name']} — Part {next_episode}").strip()
    base_id = _slug(project_name, f"episode-{next_episode}")
    project_id = base_id
    suffix = 2
    while (root / "out" / project_id).exists() or (root / "work" / project_id).exists():
        project_id = f"{base_id}-{suffix}"
        suffix += 1

    source_out = root / "out" / source_project_id
    source_work = root / "work" / source_project_id
    destination_out = root / "out" / project_id
    destination_work = root / "work" / project_id
    destination_out.mkdir(parents=True)
    destination_work.mkdir(parents=True)
    try:
        for source_media in source_out.iterdir():
            if source_media.is_file() and source_media.suffix.lower() in VIDEO_SUFFIXES:
                (destination_out / source_media.name).symlink_to(source_media.resolve())
        source_media_dir = source_work / "media"
        if source_media_dir.is_dir():
            destination_media_dir = destination_work / "media"
            destination_media_dir.mkdir()
            for source_media in source_media_dir.iterdir():
                if source_media.is_file() and source_media.suffix.lower() in MEDIA_SUFFIXES:
                    (destination_media_dir / source_media.name).symlink_to(source_media.resolve())
        for support_name in ("VIRAL_FORMAT.md", "SERIES_FORMAT.md", "premiere-import-report.json"):
            support = source_work / support_name
            if support.is_file():
                shutil.copy2(support, destination_work / support_name)

        state = json.loads(json.dumps(source_state))
        state["project_id"] = project_id
        state["episode_template"]["episode_number"] = next_episode
        for segment in state.get("segments", []):
            if segment.get("template_field") == "episode_number":
                replacement = f"Part {next_episode}"
                segment["text"] = replacement
                segment["label"] = replacement

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        preserved_metadata = {
            key: value
            for key, value in source_document.items()
            if key not in {"format", "version", "project_id", "name", "active_media", "created_at", "updated_at", "edits", "template_origin"}
        }
        document = {
            **preserved_metadata,
            "format": "llm-video-editor-project",
            "version": PROJECT_FILE_VERSION,
            "project_id": project_id,
            "name": project_name,
            "active_media": active_media,
            "created_at": now,
            "updated_at": now,
            "template_origin": {
                "project_id": source_project_id,
                "episode_number": int(template.get("episode_number", 1)),
                "duplicated_at": now,
            },
            "edits": {active_media: state},
        }
        _project_path(root, project_id).write_text(json.dumps(document, indent=2) + "\n")
        manifest = sync_asset_manifest(root, project_id, state=state, probe_media=probe)
        attach_manifest_to_state(state, manifest)
        document["edits"][active_media] = state
        _project_path(root, project_id).write_text(json.dumps(document, indent=2) + "\n")
        ensure_project_docs(root, project_id, state)
    except Exception:
        shutil.rmtree(destination_out, ignore_errors=True)
        shutil.rmtree(destination_work, ignore_errors=True)
        raise
    return next(item for item in discover_projects(root) if item["id"] == project_id)


def import_operator_file(root: Path, filename: str, source: Any, length: int, project_id: str | None = None) -> dict[str, Any]:
    safe_name = Path(filename).name
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError("The dropped file has an invalid name")
    if project_id and any(item["id"] == project_id for item in discover_projects(root)):
        directory = root / "work" / project_id / "operator-uploads"
    else:
        directory = root / "work" / "operator-uploads"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / safe_name
    if destination.exists():
        stem, suffix = destination.stem, destination.suffix
        index = 2
        while destination.exists():
            destination = directory / f"{stem}-{index}{suffix}"
            index += 1
    temporary = destination.with_suffix(destination.suffix + ".uploading")
    remaining = length
    try:
        with temporary.open("wb") as handle:
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("File upload ended early")
                handle.write(chunk)
                remaining -= len(chunk)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {"name": destination.name, "path": str(destination.resolve()), "size": destination.stat().st_size}


def resolve_operator_paths(root: Path, names: list[str]) -> dict[str, str]:
    search_roots = [Path.home() / "Desktop", Path.home() / "Downloads", Path.home() / "Documents", Path.home() / "Movies", root]
    resolved: dict[str, str] = {}
    for raw_name in names:
        safe_name = Path(str(raw_name)).name
        if not safe_name:
            continue
        matches = [(directory / safe_name).resolve() for directory in search_roots if (directory / safe_name).exists()]
        if len(matches) == 1:
            resolved[safe_name] = str(matches[0])
    return resolved


def _state_path(root: Path, project_id: str, media_id: str) -> Path:
    safe_stem = Path(media_id).stem
    return root / "work" / project_id / "edits" / f"{safe_stem}.json"


def _asset_record(project_id: str, media: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": media.name,
        "name": media.stem.replace("_", " "),
        "url": f"/media/{quote(project_id)}/{quote(media.name)}",
        "duration": metadata["duration"],
        "resolution": [metadata["width"], metadata["height"]],
        "fps": metadata["fps"],
        "has_audio": metadata["has_audio"],
    }


def _default_tracks(has_audio: bool = True) -> list[dict[str, Any]]:
    tracks = [{"id": "v1", "kind": "video", "name": "V1", "muted": False, "locked": False}]
    if has_audio:
        tracks.append({"id": "a1", "kind": "audio", "name": "A1", "muted": False, "locked": False})
    return tracks


def _initial_state(project_id: str, media: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    duration = metadata["duration"]
    return {
        "version": 2,
        "project_id": project_id,
        "media": media.name,
        "duration": duration,
        "resolution": [metadata["width"], metadata["height"]],
        "fps": metadata["fps"],
        "has_audio": metadata["has_audio"],
        "assets": [_asset_record(project_id, media, metadata)],
        "tracks": _default_tracks(metadata["has_audio"]),
        "segments": [
            {
                "id": "clip-1",
                "label": "Generated edit",
                "kind": "video",
                "asset_id": media.name,
                "track_id": "v1",
                "audio_track_id": "a1" if metadata["has_audio"] else None,
                "audio_linked": metadata["has_audio"],
                "timeline_start": 0.0,
                "source_start": 0.0,
                "source_end": duration,
                "speed": 1.0,
                "volume": 1.0,
                "audio_fade_in": 0.0,
                "audio_fade_out": 0.0,
                "transition_in": {"type": "none", "duration": 0.0},
                "transition_out": {"type": "none", "duration": 0.0},
                "transform": {"scale": 1.0, "x": 0.0, "y": 0.0, "rotation": 0, "crop_top": 0.0, "crop_right": 0.0, "crop_bottom": 0.0, "crop_left": 0.0},
                "color": {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "temperature": 0.0},
                "keyframes": [],
                "enabled": True,
            }
        ],
    }


def load_editor_state(root: Path, project_id: str, media_id: str | None = None) -> dict[str, Any]:
    project, media = _resolve_media(root, project_id, media_id)
    metadata = probe(media)
    path = _state_path(root, project_id, media.name)
    project_path = _project_path(root, project_id)
    project_file = _read_project_file(root, project_id)
    project_state = project_file.get("edits", {}).get(media.name) if isinstance(project_file.get("edits"), dict) else None
    if isinstance(project_state, dict):
        try:
            state = _normalize_state(project_state, project_id, media.name, metadata)
        except (ValueError, TypeError):
            state = _initial_state(project_id, media, metadata)
    elif path.is_file():
        try:
            state = _normalize_state(json.loads(path.read_text()), project_id, media.name, metadata)
        except (json.JSONDecodeError, ValueError, TypeError):
            state = _initial_state(project_id, media, metadata)
    else:
        state = _initial_state(project_id, media, metadata)
    state["project"] = project
    known_assets = {item["id"] for item in state["assets"]}
    state["assets"].extend(item for item in discover_assets(root, project_id) if item["id"] not in known_assets)
    manifest = sync_asset_manifest(root, project_id, state=state, probe_media=probe)
    attach_manifest_to_state(state, manifest)
    state["media_url"] = f"/media/{quote(project_id)}/{quote(media.name)}"
    state["project_file"] = str(project_path.relative_to(root)) if project_path.is_file() else None
    state["saved"] = isinstance(project_state, dict) or path.is_file()
    if state.get("episode_template"):
        ensure_project_docs(root, project_id, state)
    return state


def _normalize_episode_template(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    slots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_slot in enumerate(raw.get("slots", [])):
        if not isinstance(raw_slot, dict):
            continue
        slot_id = re.sub(r"[^a-z0-9_-]", "", str(raw_slot.get("id", "")).lower())[:48]
        if not slot_id or slot_id in seen:
            continue
        seen.add(slot_id)
        start = max(0.0, float(raw_slot.get("start", 0)))
        end = max(start + 0.1, float(raw_slot.get("end", start + 1)))
        slots.append(
            {
                "id": slot_id,
                "label": str(raw_slot.get("label") or slot_id.replace("_", " ").title())[:80],
                "start": round(start, 3),
                "end": round(end, 3),
                "required": bool(raw_slot.get("required", True)),
                "prompt": str(raw_slot.get("prompt") or "")[:300],
            }
        )
    if not slots:
        return None
    placeholder_asset_ids = [
        Path(str(asset_id)).name
        for asset_id in raw.get("placeholder_asset_ids", [])
        if Path(str(asset_id)).name
    ]
    constraints_raw = raw.get("constraints") if isinstance(raw.get("constraints"), dict) else {}
    resolution = constraints_raw.get("resolution")
    constraints: dict[str, Any] = {
        "slot_timing_tolerance": round(max(0.0, min(float(constraints_raw.get("slot_timing_tolerance", 0.25)), 5.0)), 3),
        "question_before_payoff": bool(constraints_raw.get("question_before_payoff", True)),
        "require_visual_coverage": bool(constraints_raw.get("require_visual_coverage", True)),
        "max_opening_visual_delay": round(max(0.0, min(float(constraints_raw.get("max_opening_visual_delay", 0.1)), 10.0)), 3),
        "caption_safe_bottom_percent": round(max(0.0, min(float(constraints_raw.get("caption_safe_bottom_percent", 14)), 50.0)), 2),
        "required_locked_track_ids": [str(item) for item in constraints_raw.get("required_locked_track_ids", raw.get("locked_track_ids", []))][:20],
        "required_reserved_track_ids": [str(item) for item in constraints_raw.get("required_reserved_track_ids", raw.get("reserved_track_ids", []))][:20],
    }
    if isinstance(resolution, list) and len(resolution) == 2:
        constraints["resolution"] = [max(16, int(resolution[0])), max(16, int(resolution[1]))]
    if constraints_raw.get("fps") is not None:
        constraints["fps"] = round(max(1.0, min(float(constraints_raw["fps"]), 240.0)), 3)
    runtime = constraints_raw.get("runtime") if isinstance(constraints_raw.get("runtime"), dict) else {}
    if runtime:
        minimum = max(0.0, float(runtime.get("min", 0)))
        constraints["runtime"] = {
            "min": round(minimum, 3),
            "max": round(max(minimum, float(runtime.get("max", minimum))), 3),
        }
    return {
        "version": max(1, int(raw.get("version", 1))),
        "id": re.sub(r"[^a-z0-9_-]", "", str(raw.get("id", "episode-template")).lower())[:80] or "episode-template",
        "name": str(raw.get("name") or "Episode template")[:120],
        "episode_number": max(1, int(raw.get("episode_number", 1))),
        "slots": slots,
        "question_slot_id": str(raw.get("question_slot_id") or "") if str(raw.get("question_slot_id") or "") in seen else None,
        "payoff_slot_id": str(raw.get("payoff_slot_id") or "") if str(raw.get("payoff_slot_id") or "") in seen else None,
        "locked_track_ids": [str(track_id) for track_id in raw.get("locked_track_ids", [])][:20],
        "reserved_track_ids": [str(track_id) for track_id in raw.get("reserved_track_ids", [])][:20],
        "placeholder_asset_ids": list(dict.fromkeys(placeholder_asset_ids))[:200],
        "constraints": constraints,
    }


def validate_episode_template(state: dict[str, Any]) -> dict[str, Any] | None:
    template = state.get("episode_template")
    if not isinstance(template, dict):
        return None
    slot_results: list[dict[str, Any]] = []
    missing: list[str] = []
    for slot in template.get("slots", []):
        videos = [
            segment
            for segment in state.get("segments", [])
            if segment.get("enabled", True)
            and segment.get("kind") == "video"
            and segment.get("slot_id") == slot["id"]
        ]
        replaced = sum(not bool(segment.get("template_source", False)) for segment in videos)
        status = "missing" if not videos else "replaced" if replaced == len(videos) else "mixed" if replaced else "original"
        if slot.get("required", True) and not videos:
            missing.append(slot["id"])
        slot_results.append(
            {
                "id": slot["id"],
                "label": slot["label"],
                "status": status,
                "video_clips": len(videos),
                "replaced_clips": replaced,
            }
        )
    return {
        "valid": not missing,
        "missing_slot_ids": missing,
        "slots": slot_results,
        "question_present": any(item["id"] == template.get("question_slot_id") and item["status"] != "missing" for item in slot_results),
        "payoff_present": any(item["id"] == template.get("payoff_slot_id") and item["status"] != "missing" for item in slot_results),
    }


def _normalize_state(data: dict[str, Any], project_id: str, media_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    duration = float(metadata["duration"])
    episode_template = _normalize_episode_template(data.get("episode_template"))
    template_slot_ids = {item["id"] for item in episode_template.get("slots", [])} if episode_template else set()
    template_locked_tracks = set(episode_template.get("locked_track_ids", [])) if episode_template else set()
    template_reserved_tracks = set(episode_template.get("reserved_track_ids", [])) if episode_template else set()
    assets: list[dict[str, Any]] = []
    for raw_asset in data.get("assets", []):
        asset_id = Path(str(raw_asset.get("id", ""))).name
        if not asset_id or asset_id in {item["id"] for item in assets}:
            continue
        resolution = raw_asset.get("resolution", [metadata["width"], metadata["height"]])
        assets.append(
            {
                "id": asset_id,
                "name": str(raw_asset.get("name") or Path(asset_id).stem.replace("_", " "))[:100],
                "url": f"/media/{quote(project_id)}/{quote(asset_id)}",
                "duration": max(0.0, float(raw_asset.get("duration", duration))),
                "resolution": [int(resolution[0]), int(resolution[1])],
                "fps": max(1.0, float(raw_asset.get("fps", metadata["fps"]))),
                "has_audio": bool(raw_asset.get("has_audio", True)),
            }
        )
    if media_id not in {item["id"] for item in assets}:
        assets.insert(0, _asset_record(project_id, Path(media_id), metadata))

    tracks: list[dict[str, Any]] = []
    for raw_track in data.get("tracks", _default_tracks(metadata["has_audio"])):
        kind = str(raw_track.get("kind", "video"))
        if kind not in {"video", "audio"}:
            continue
        track_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(raw_track.get("id", "")))
        if not track_id or track_id in {item["id"] for item in tracks}:
            continue
        tracks.append(
            {
                "id": track_id,
                "kind": kind,
                "name": str(raw_track.get("name") or track_id.upper())[:20],
                "muted": bool(raw_track.get("muted", False)),
                "locked": bool(raw_track.get("locked", False) or track_id in template_locked_tracks or raw_track.get("template_locked", False)),
                "template_locked": bool(track_id in template_locked_tracks or raw_track.get("template_locked", False)),
                "template_reserved": bool(track_id in template_reserved_tracks or raw_track.get("template_reserved", False)),
            }
        )
    if not any(item["kind"] == "video" for item in tracks):
        tracks.insert(0, _default_tracks(False)[0])

    segments: list[dict[str, Any]] = []
    legacy_cursor = 0.0
    for index, raw in enumerate(data.get("segments", [])):
        kind = str(raw.get("kind", "video"))
        if kind not in {"video", "audio", "title", "caption"}:
            kind = "video"
        if kind in {"title", "caption"}:
            clip_duration = max(0.1, min(float(raw.get("duration", 3.0)), 86400.0))
            start = 0.0
            end = clip_duration
        else:
            asset = next((item for item in assets if item["id"] == Path(str(raw.get("asset_id", media_id))).name), assets[0])
            asset_duration = float(asset["duration"])
            start = max(0.0, min(float(raw.get("source_start", 0)), asset_duration))
            end = max(start, min(float(raw.get("source_end", asset_duration)), asset_duration))
            if end - start < 0.04:
                continue
            clip_duration = end - start
        speed = max(0.1, min(float(raw.get("speed", 1.0)), 100.0))
        timeline_start = max(0.0, float(raw.get("timeline_start", legacy_cursor)))
        transform = raw.get("transform") if isinstance(raw.get("transform"), dict) else {}
        color = raw.get("color") if isinstance(raw.get("color"), dict) else {}
        transition_in = raw.get("transition_in") if isinstance(raw.get("transition_in"), dict) else {}
        transition_out = raw.get("transition_out") if isinstance(raw.get("transition_out"), dict) else {}
        track_kind = "audio" if kind == "audio" else "video"
        default_track = next((item["id"] for item in tracks if item["kind"] == track_kind), "a1" if track_kind == "audio" else "v1")
        segments.append(
            {
                "id": str(raw.get("id") or f"clip-{index + 1}"),
                "label": str(raw.get("label") or f"Clip {index + 1}")[:80],
                "kind": kind,
                "asset_id": Path(str(raw.get("asset_id", media_id))).name if kind not in {"title", "caption"} else None,
                "track_id": str(raw.get("track_id") or default_track),
                "audio_track_id": str(raw.get("audio_track_id") or "a1") if kind == "video" and raw.get("audio_linked", metadata["has_audio"]) else None,
                "audio_linked": bool(raw.get("audio_linked", metadata["has_audio"])) if kind == "video" else False,
                "timeline_start": round(timeline_start, 3),
                "source_start": round(start, 3),
                "source_end": round(end, 3),
                "duration": round(clip_duration, 3) if kind in {"title", "caption"} else None,
                "speed": round(speed, 3),
                "volume": round(max(0.0, min(float(raw.get("volume", 1)), 2.0)), 2),
                "audio_fade_in": round(max(0.0, float(raw.get("audio_fade_in", 0))), 3),
                "audio_fade_out": round(max(0.0, float(raw.get("audio_fade_out", 0))), 3),
                "transition_in": {"type": str(transition_in.get("type", "none")), "duration": round(max(0.0, float(transition_in.get("duration", 0))), 3)},
                "transition_out": {"type": str(transition_out.get("type", "none")), "duration": round(max(0.0, float(transition_out.get("duration", 0))), 3)},
                "transform": {
                    "scale": round(max(0.1, min(float(transform.get("scale", 1)), 4.0)), 3),
                    "x": round(float(transform.get("x", 0)), 2),
                    "y": round(float(transform.get("y", 0)), 2),
                    "rotation": int(round(float(transform.get("rotation", 0)) / 90.0) * 90) % 360,
                    "crop_top": round(max(0.0, min(float(transform.get("crop_top", 0)), 49.0)), 2),
                    "crop_right": round(max(0.0, min(float(transform.get("crop_right", 0)), 49.0)), 2),
                    "crop_bottom": round(max(0.0, min(float(transform.get("crop_bottom", 0)), 49.0)), 2),
                    "crop_left": round(max(0.0, min(float(transform.get("crop_left", 0)), 49.0)), 2),
                },
                "color": {
                    "brightness": round(max(-1.0, min(float(color.get("brightness", 0)), 1.0)), 3),
                    "contrast": round(max(0.0, min(float(color.get("contrast", 1)), 3.0)), 3),
                    "saturation": round(max(0.0, min(float(color.get("saturation", 1)), 3.0)), 3),
                    "temperature": round(max(-1.0, min(float(color.get("temperature", 0)), 1.0)), 3),
                },
                "text": str(raw.get("text", raw.get("label", "")))[:500],
                "font_size": max(12, min(int(raw.get("font_size", 72 if kind == "caption" else 64)), 240)),
                "text_color": str(raw.get("text_color", "#ffffff"))[:16],
                "keyframes": raw.get("keyframes", []) if isinstance(raw.get("keyframes"), list) else [],
                "enabled": bool(raw.get("enabled", True)),
                "slot_id": str(raw.get("slot_id")) if str(raw.get("slot_id")) in template_slot_ids else None,
                "template_role": str(raw.get("template_role") or "")[:32] or None,
                "template_field": str(raw.get("template_field") or "")[:48] or None,
                "template_source": bool(raw.get("template_source", False)),
                "locked": bool(raw.get("locked", False)),
            }
        )
        if "timeline_start" not in raw and kind == "video":
            legacy_cursor += clip_duration / speed
    state = {
        "version": 2,
        "project_id": project_id,
        "media": media_id,
        "duration": duration,
        "resolution": [metadata["width"], metadata["height"]],
        "fps": metadata["fps"],
        "has_audio": metadata["has_audio"],
        "assets": assets,
        "tracks": tracks,
        "export_preset": str(data.get("export_preset", "source")) if str(data.get("export_preset", "source")) in EXPORT_PRESETS else "source",
        "segments": segments,
    }
    if episode_template:
        state["episode_template"] = episode_template
        state["template_status"] = validate_episode_template(state)
    return state


def save_editor_state(root: Path, project_id: str, media_id: str, data: dict[str, Any]) -> dict[str, Any]:
    project, media = _resolve_media(root, project_id, media_id)
    state = _normalize_state(data, project_id, media.name, probe(media))
    manifest = sync_asset_manifest(root, project_id, state=state, probe_media=probe)
    attach_manifest_to_state(state, manifest)
    path = _state_path(root, project_id, media.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")
    project_path = _project_path(root, project_id)
    project_file = _read_project_file(root, project_id)
    edits = project_file.get("edits") if isinstance(project_file.get("edits"), dict) else {}
    edits[media.name] = state
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    preserved_metadata = {
        key: value
        for key, value in project_file.items()
        if key not in {"format", "version", "project_id", "name", "active_media", "created_at", "updated_at", "edits"}
    }
    project_file = {
        **preserved_metadata,
        "format": "llm-video-editor-project",
        "version": PROJECT_FILE_VERSION,
        "project_id": project_id,
        "name": project["name"],
        "active_media": media.name,
        "created_at": project_file.get("created_at", now),
        "updated_at": now,
        "edits": edits,
    }
    project_path.write_text(json.dumps(project_file, indent=2) + "\n")
    return {**state, "project_file": str(project_path.relative_to(root))}


def trash_project_media(root: Path, project_id: str) -> Path:
    project = next((item for item in discover_projects(root) if item["id"] == project_id), None)
    if not project:
        raise FileNotFoundError(f"Unknown editor project: {project_id}")
    source = root / "out" / project_id
    trash = Path.home() / ".Trash"
    trash.mkdir(parents=True, exist_ok=True)
    destination = trash / source.name
    if destination.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = trash / f"{source.name}-{timestamp}"
    shutil.move(str(source), str(destination))
    _probe_cached.cache_clear()
    return destination


def _build_legacy_render_command(ffmpeg: str, source: Path, output: Path, state: dict[str, Any]) -> list[str]:
    segments = [item for item in state["segments"] if item.get("enabled", True)]
    if not segments:
        raise ValueError("The timeline has no enabled clips to render")
    filters: list[str] = []
    concat_inputs: list[str] = []
    has_audio = bool(state.get("has_audio"))
    for index, segment in enumerate(segments):
        start, end = segment["source_start"], segment["source_end"]
        filters.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]")
        concat_inputs.append(f"[v{index}]")
        if has_audio:
            volume = segment.get("volume", 1)
            filters.append(
                f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,volume={volume}[a{index}]"
            )
            concat_inputs.append(f"[a{index}]")
    filters.append(
        "".join(concat_inputs)
        + f"concat=n={len(segments)}:v=1:a={1 if has_audio else 0}[vout]"
        + ("[aout]" if has_audio else "")
    )
    command = [ffmpeg, "-y", "-i", str(source), "-filter_complex", ";".join(filters), "-map", "[vout]"]
    if has_audio:
        command += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    return command + ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart", str(output)]


def _atempo(speed: float) -> str:
    values: list[float] = []
    remaining = speed
    while remaining > 2.0:
        values.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        values.append(0.5)
        remaining /= 0.5
    values.append(remaining)
    return ",".join(f"atempo={value:.5f}" for value in values)


def _drawtext_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "'\\\\\\''").replace(":", "\\:").replace("%", "\\\\%")


def _wrap_overlay_text(value: str, frame_width: int, font_size: int) -> str:
    """Wrap overlay copy inside the same horizontal safe area as the previews."""
    safe_text_width = frame_width * 0.82
    average_glyph_width = max(1.0, font_size * 0.56)
    line_width = max(8, int(safe_text_width / average_glyph_width))
    wrapped_lines: list[str] = []
    for source_line in value.splitlines() or [""]:
        wrapped_lines.extend(
            textwrap.wrap(
                source_line,
                width=line_width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )
    return "\n".join(wrapped_lines)


def _keyframe_expression(segment: dict[str, Any], group: str | None, key: str, default: float, time_offset: float = 0.0) -> str:
    points: list[tuple[float, float]] = [(time_offset, default)]
    for keyframe in sorted(segment.get("keyframes", []), key=lambda item: float(item.get("time", 0))):
        container = keyframe.get(group, {}) if group else keyframe
        if isinstance(container, dict) and key in container:
            point = (time_offset + max(0.0, float(keyframe.get("time", 0))), float(container[key]))
            if abs(point[0] - points[-1][0]) < 0.0001:
                points[-1] = point
            else:
                points.append(point)
    expression = f"{points[-1][1]:.5f}"
    for left, right in reversed(list(zip(points, points[1:]))):
        left_time, left_value = left
        right_time, right_value = right
        interpolation = f"{left_value:.5f}+({right_value-left_value:.5f})*(t-{left_time:.5f})/{max(.0001, right_time-left_time):.5f}"
        expression = f"if(lt(t,{right_time:.5f}),{interpolation},{expression})"
    return expression


def _preset_settings(state: dict[str, Any], preset: str) -> tuple[int, int, str, str]:
    source_width, source_height = state.get("resolution", [1920, 1080])
    draft = (720, 1280, "veryfast", "28") if source_height > source_width else (1280, 720, "veryfast", "28")
    presets = {
        "source": (int(source_width), int(source_height), "medium", "18"),
        "youtube": (1920, 1080, "medium", "18"),
        "vertical": (1080, 1920, "medium", "19"),
        "draft": draft,
    }
    return presets.get(preset, presets["source"])


def build_render_command(
    ffmpeg: str,
    source: Path,
    output: Path,
    state: dict[str, Any],
    *,
    asset_paths: dict[str, Path] | None = None,
    preset: str = "source",
    progress: bool = False,
) -> list[str]:
    if int(state.get("version", 1)) < 2 or not state.get("tracks"):
        return _build_legacy_render_command(ffmpeg, source, output, state)

    segments = [item for item in state["segments"] if item.get("enabled", True)]
    media_segments = [item for item in segments if item.get("kind") in {"video", "audio"}]
    video_segments = [item for item in media_segments if item.get("kind") == "video"]
    if not video_segments:
        raise ValueError("The timeline has no enabled video clips to render")
    asset_paths = asset_paths or {str(state.get("media")): source}
    used_assets = list(dict.fromkeys(str(item.get("asset_id")) for item in media_segments))
    missing = [asset_id for asset_id in used_assets if asset_id not in asset_paths]
    if missing:
        raise FileNotFoundError(f"Missing project media: {', '.join(missing)}")
    input_index = {asset_id: index for index, asset_id in enumerate(used_assets)}
    width, height, encoder_preset, crf = _preset_settings(state, preset)
    frame_rate = max(1.0, float(state.get("fps", 30)))
    sequence_duration = max(
        float(item.get("timeline_start", 0))
        + (float(item.get("duration", 0)) if item.get("kind") in {"title", "caption"} else (float(item["source_end"]) - float(item["source_start"])) / max(0.1, float(item.get("speed", 1))))
        for item in segments
    )
    filters: list[str] = [f"color=c=black:s={width}x{height}:r={frame_rate}:d={sequence_duration:.3f}[base0]"]
    video_track_order = {track["id"]: index for index, track in enumerate(state.get("tracks", [])) if track.get("kind") == "video" and not track.get("muted")}
    ordered_video = sorted(
        (item for item in video_segments if item.get("track_id") in video_track_order),
        key=lambda item: (video_track_order[item["track_id"]], float(item.get("timeline_start", 0))),
    )
    base_label = "base0"
    for index, segment in enumerate(ordered_video):
        asset_id = str(segment["asset_id"])
        speed = max(0.1, float(segment.get("speed", 1)))
        clip_duration = (float(segment["source_end"]) - float(segment["source_start"])) / speed
        start_time = float(segment.get("timeline_start", 0))
        transform = segment.get("transform", {})
        color = segment.get("color", {})
        crop_left = max(0.0, min(float(transform.get("crop_left", 0)), 49.0))
        crop_right = max(0.0, min(float(transform.get("crop_right", 0)), 49.0))
        crop_top = max(0.0, min(float(transform.get("crop_top", 0)), 49.0))
        crop_bottom = max(0.0, min(float(transform.get("crop_bottom", 0)), 49.0))
        scale = max(0.1, min(float(transform.get("scale", 1)), 4.0))
        rotation = int(round(float(transform.get("rotation", 0)) / 90.0) * 90) % 360
        temperature = max(-1.0, min(float(color.get("temperature", 0)), 1.0))
        scale_expression = _keyframe_expression(segment, "transform", "scale", scale)
        brightness_expression = _keyframe_expression(segment, "color", "brightness", float(color.get("brightness", 0)))
        contrast_expression = _keyframe_expression(segment, "color", "contrast", float(color.get("contrast", 1)))
        saturation_expression = _keyframe_expression(segment, "color", "saturation", float(color.get("saturation", 1)))
        chain = [
            f"[{input_index[asset_id]}:v]trim=start={float(segment['source_start']):.3f}:end={float(segment['source_end']):.3f}",
            f"setpts=(PTS-STARTPTS)/{speed:.5f}",
        ]
        if rotation == 90:
            chain.append("transpose=clock")
        elif rotation == 180:
            chain.extend(["hflip", "vflip"])
        elif rotation == 270:
            chain.append("transpose=cclock")
        chain.extend([
            f"crop=iw*{1-(crop_left+crop_right)/100:.6f}:ih*{1-(crop_top+crop_bottom)/100:.6f}:iw*{crop_left/100:.6f}:ih*{crop_top/100:.6f}",
        ])
        if _needs_hdr_to_sdr(asset_paths.get(asset_id)):
            # iPhone HDR video is BT.2020 HLG (or occasionally PQ). Convert its
            # transfer function and gamut before mixing it with SDR sources.
            # Merely tagging HLG samples as BT.709 lifts the midtones and drains
            # saturation, which is the familiar washed-out export failure.
            chain.extend([
                "zscale=transfer=linear:npl=100",
                "format=gbrpf32le",
                "zscale=primaries=bt709",
                "tonemap=tonemap=hable:desat=0",
                "zscale=transfer=bt709:matrix=bt709:range=tv",
            ])
        chain.extend([
            f"eq=brightness='{brightness_expression}':contrast='{contrast_expression}':saturation='{saturation_expression}':eval=frame",
            (
                f"colorbalance=rs={temperature * .15:.4f}:rm={temperature * .15:.4f}:rh={temperature * .08:.4f}:"
                f"bs={temperature * -.22:.4f}:bm={temperature * -.22:.4f}:bh={temperature * -.12:.4f}"
            ),
            f"scale=w='trunc(iw*min({width}/iw\\,{height}/ih)*({scale_expression})/2)*2':h='trunc(ih*min({width}/iw\\,{height}/ih)*({scale_expression})/2)*2':eval=frame",
            "format=rgba",
            # Source and sequence frame grids often differ (for example,
            # 29.97 fps iPhone footage on a 30 fps timeline). Supply one
            # cloned boundary frame so framesync cannot expose the black base
            # for a frame at an otherwise contiguous edit point. The overlay
            # enable window below still stops at the clip's nominal end, so
            # intentional timeline gaps remain black.
            f"tpad=stop_mode=clone:stop_duration={1/frame_rate:.6f}",
        ])
        transition_in = segment.get("transition_in", {})
        transition_out = segment.get("transition_out", {})
        if transition_in.get("type") in {"fade", "dissolve"} and float(transition_in.get("duration", 0)) > 0:
            chain.append(f"fade=t=in:st=0:d={min(clip_duration, float(transition_in['duration'])):.3f}:alpha=1")
        if transition_out.get("type") in {"fade", "dissolve"} and float(transition_out.get("duration", 0)) > 0:
            fade_duration = min(clip_duration, float(transition_out["duration"]))
            chain.append(f"fade=t=out:st={max(0, clip_duration-fade_duration):.3f}:d={fade_duration:.3f}:alpha=1")
        chain.append(f"setpts=PTS+{start_time:.3f}/TB[v{index}]")
        filters.append(",".join(chain))
        next_base = f"base{index + 1}"
        x_expression = _keyframe_expression(segment, "transform", "x", float(transform.get("x", 0)), start_time)
        y_expression = _keyframe_expression(segment, "transform", "y", float(transform.get("y", 0)), start_time)
        overlay_x = f"(W-w)/2+({x_expression})"
        if transition_in.get("type") == "wipeleft" and float(transition_in.get("duration", 0)) > 0:
            wipe_duration = min(clip_duration, float(transition_in["duration"]))
            overlay_x = f"if(lt(t,{start_time+wipe_duration:.3f}),W-(t-{start_time:.3f})*W/{wipe_duration:.3f},{overlay_x})"
        if transition_out.get("type") == "wipeleft" and float(transition_out.get("duration", 0)) > 0:
            wipe_duration = min(clip_duration, float(transition_out["duration"]))
            wipe_start = start_time + clip_duration - wipe_duration
            overlay_x = f"if(gt(t,{wipe_start:.3f}),({overlay_x})-(t-{wipe_start:.3f})*W/{wipe_duration:.3f},{overlay_x})"
        clip_end = start_time + clip_duration
        filters.append(
            f"[{base_label}][v{index}]overlay=x='{overlay_x}':y='(H-h)/2+({y_expression})':"
            f"eof_action=pass:repeatlast=0:enable='gte(t,{start_time:.6f})*lt(t,{clip_end:.6f})'[{next_base}]"
        )
        base_label = next_base

    text_index = 0
    for segment in sorted((item for item in segments if item.get("kind") in {"title", "caption"}), key=lambda item: float(item.get("timeline_start", 0))):
        start_time = float(segment.get("timeline_start", 0))
        end_time = start_time + float(segment.get("duration", 3))
        color = str(segment.get("text_color", "#ffffff")).removeprefix("#")
        size = int(segment.get("font_size", 54))
        text = _drawtext_escape(
            _wrap_overlay_text(
                str(segment.get("text") or segment.get("label") or ""),
                width,
                size,
            )
        )
        y = "h-text_h-h*0.14" if segment.get("kind") == "caption" else "h*0.10"
        # FreeType's glyph box carries more visual weight below its midpoint.
        # Give rendered captions extra room above the glyphs so the text sits
        # optically centered in the background, matching both editor previews.
        box = ":box=1:boxcolor=black@0.70:boxborderw=18|14|10|14" if segment.get("kind") == "caption" else ""
        next_base = f"text{text_index}"
        filters.append(f"[{base_label}]drawtext=text='{text}':fontcolor=0x{color}:fontsize={size}:x=(w-text_w)/2:y={y}:text_align=C:line_spacing=10{box}:enable='between(t,{start_time:.3f},{end_time:.3f})'[{next_base}]")
        base_label = next_base
        text_index += 1
    filters.append(
        f"[{base_label}]format=yuv420p,"
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709[vout]"
    )

    audio_tracks = {track["id"] for track in state.get("tracks", []) if track.get("kind") == "audio" and not track.get("muted")}
    audio_labels: list[str] = []
    for index, segment in enumerate(media_segments):
        if segment.get("kind") == "video":
            if not segment.get("audio_linked") or segment.get("audio_track_id") not in audio_tracks:
                continue
        elif segment.get("track_id") not in audio_tracks:
            continue
        asset = next((item for item in state.get("assets", []) if item.get("id") == segment.get("asset_id")), {})
        if not asset.get("has_audio", state.get("has_audio", True)):
            continue
        speed = max(0.1, float(segment.get("speed", 1)))
        clip_duration = (float(segment["source_end"]) - float(segment["source_start"])) / speed
        fade_in = min(clip_duration, float(segment.get("audio_fade_in", 0)))
        fade_out = min(clip_duration, float(segment.get("audio_fade_out", 0)))
        delay = max(0, round(float(segment.get("timeline_start", 0)) * 1000))
        chain = [
            f"[{input_index[str(segment['asset_id'])]}:a]atrim=start={float(segment['source_start']):.3f}:end={float(segment['source_end']):.3f}",
            "asetpts=PTS-STARTPTS",
            _atempo(speed),
            f"volume='{_keyframe_expression(segment, None, 'volume', float(segment.get('volume', 1)))}':eval=frame",
        ]
        if fade_in > 0:
            chain.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            chain.append(f"afade=t=out:st={max(0, clip_duration-fade_out):.3f}:d={fade_out:.3f}")
        chain.append(f"adelay={delay}|{delay}[a{index}]")
        filters.append(",".join(chain))
        audio_labels.append(f"[a{index}]")
    if audio_labels:
        filters.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0:normalize=0,atrim=duration={sequence_duration:.3f}[aout]")

    command = [ffmpeg, "-y"]
    if progress:
        command += ["-progress", "pipe:1", "-nostats"]
    for asset_id in used_assets:
        command += ["-i", str(asset_paths[asset_id])]
    command += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if audio_labels:
        command += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    return command + [
        "-c:v", "libx264", "-preset", encoder_preset, "-crf", crf,
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
        "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        "-movflags", "+faststart+write_colr", str(output),
    ]


def render_editor_state(root: Path, project_id: str, media_id: str, data: dict[str, Any], preset: str | None = None) -> Path:
    state = save_editor_state(root, project_id, media_id, data)
    preflight = check_state(root, project_id, state)
    blocked = blocking_preflight_issues(preflight)
    if blocked:
        raise ValueError("Render preflight failed: " + "; ".join(item["message"] for item in blocked))
    _, source = _resolve_media(root, project_id, media_id)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to render an edit")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    selected_preset = preset or str(state.get("export_preset", "source"))
    if selected_preset not in EXPORT_PRESETS:
        raise ValueError("Unknown export preset")
    output = root / "out" / project_id / f"{timestamp}_{project_id}_{selected_preset}-edit.mp4"
    asset_paths = _render_asset_paths(root, project_id, state)
    subprocess.run(build_render_command(ffmpeg, source, output, state, asset_paths=asset_paths, preset=selected_preset), check=True)
    return output


def _sequence_duration(state: dict[str, Any]) -> float:
    return max(
        (
            float(item.get("timeline_start", 0))
            + (
                float(item.get("duration", 0))
                if item.get("kind") in {"title", "caption"}
                else (float(item.get("source_end", 0)) - float(item.get("source_start", 0))) / max(0.1, float(item.get("speed", 1)))
            )
        )
        for item in state.get("segments", [])
        if item.get("enabled", True)
    )


def _ffmpeg_progress_seconds(line: str) -> float | None:
    """Parse FFmpeg's machine-readable progress timestamp when it is numeric."""
    if not line.startswith(("out_time_ms=", "out_time_us=")):
        return None
    value = line.partition("=")[2].strip()
    if not value or value.upper() == "N/A":
        return None
    try:
        return float(value) / 1_000_000
    except ValueError:
        return None


class RenderJob:
    def __init__(self, root: Path, project_id: str, media_id: str, data: dict[str, Any], preset: str, preflight: dict[str, Any]):
        self.id = uuid.uuid4().hex
        self.root = root
        self.project_id = project_id
        self.media_id = media_id
        self.data = data
        self.preset = preset
        self.preflight = preflight
        self.status = "queued"
        self.progress = 0.0
        self.error: str | None = None
        self.output: Path | None = None
        self.process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=f"editor-render-{self.id[:8]}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            state = save_editor_state(self.root, self.project_id, self.media_id, self.data)
            _, source = _resolve_media(self.root, self.project_id, self.media_id)
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("ffmpeg is required to render an edit")
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            output = self.root / "out" / self.project_id / f"{timestamp}_{self.project_id}_{self.preset}-edit.mp4"
            asset_paths = _render_asset_paths(self.root, self.project_id, state)
            command = build_render_command(ffmpeg, source, output, state, asset_paths=asset_paths, preset=self.preset, progress=True)
            total = max(0.001, _sequence_duration(state))
            with self._lock:
                self.status = "rendering"
            self.process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            messages: list[str] = []
            assert self.process.stdout is not None
            for raw_line in self.process.stdout:
                line = raw_line.strip()
                rendered = _ffmpeg_progress_seconds(line)
                if rendered is not None:
                    with self._lock:
                        self.progress = max(self.progress, min(0.99, rendered / total))
                elif line and not line.startswith(("frame=", "fps=", "bitrate=", "total_size=", "out_time=", "dup_frames=", "drop_frames=", "speed=", "progress=")):
                    messages.append(line)
                    messages = messages[-20:]
            return_code = self.process.wait()
            if return_code:
                raise RuntimeError("FFmpeg render failed" + (f": {' | '.join(messages[-4:])}" if messages else ""))
            _probe_cached.cache_clear()
            with self._lock:
                self.status = "complete"
                self.progress = 1.0
                self.output = output
        except Exception as error:
            with self._lock:
                self.status = "failed"
                self.error = str(error)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "progress": round(self.progress, 4),
                "error": self.error,
                "output": str(self.output) if self.output else None,
                "name": self.output.name if self.output else None,
                "preflight": self.preflight,
            }

    def stop(self) -> None:
        process = self.process
        if process and process.poll() is None:
            process.terminate()


class RenderManager:
    def __init__(self, root: Path):
        self.root = root
        self._jobs: dict[str, RenderJob] = {}
        self._lock = threading.Lock()

    def start(self, project_id: str, media_id: str, data: dict[str, Any], preset: str) -> RenderJob:
        if preset not in EXPORT_PRESETS:
            raise ValueError("Unknown export preset")
        _, media = _resolve_media(self.root, project_id, media_id)
        state = _normalize_state(data, project_id, media.name, probe(media))
        manifest = sync_asset_manifest(self.root, project_id, state=state, probe_media=probe)
        attach_manifest_to_state(state, manifest)
        preflight = check_state(self.root, project_id, state, manifest=manifest)
        blocked = blocking_preflight_issues(preflight)
        if blocked:
            raise ValueError("Render preflight failed: " + "; ".join(item["message"] for item in blocked))
        job = RenderJob(self.root, project_id, media_id, state, preset, preflight)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> RenderJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            raise FileNotFoundError("Render job not found")
        return job

    def close(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.stop()


def _operator_duration(segment: dict[str, Any]) -> float:
    if segment.get("kind") in {"title", "caption"}:
        return max(0.1, float(segment.get("duration", segment.get("source_end", 3))))
    return max(0.0, (float(segment.get("source_end", 0)) - float(segment.get("source_start", 0))) / max(0.1, float(segment.get("speed", 1))))


def _operator_segment_locked(state: dict[str, Any], segment: dict[str, Any]) -> bool:
    track = next((item for item in state.get("tracks", []) if item.get("id") == segment.get("track_id")), None)
    return bool(segment.get("locked") or (track and track.get("locked")))


def _fit_replacement_to_segment(segment: dict[str, Any], asset: dict[str, Any]) -> None:
    target_duration = max(0.04, _operator_duration(segment))
    previous_speed = max(0.1, float(segment.get("speed", 1)))
    available = max(0.04, float(asset.get("duration", target_duration)))
    source_duration = min(available, target_duration * previous_speed)
    segment.update(
        {
            "asset_id": asset["id"],
            "label": str(asset.get("name") or asset["id"]),
            "source_start": 0.0,
            "source_end": source_duration,
            "speed": max(0.1, min(100.0, source_duration / target_duration)),
            "template_source": False,
        }
    )


def _replace_operator_clip(state: dict[str, Any], segment: dict[str, Any], asset: dict[str, Any]) -> None:
    old_asset_id = segment.get("asset_id")
    timeline_start = float(segment.get("timeline_start", 0))
    slot_id = segment.get("slot_id")
    _fit_replacement_to_segment(segment, asset)
    companions = [
        item
        for item in state.get("segments", [])
        if item is not segment
        and item.get("kind") == "audio"
        and item.get("asset_id") == old_asset_id
        and item.get("slot_id") == slot_id
        and abs(float(item.get("timeline_start", 0)) - timeline_start) <= 0.01
    ]
    for companion in companions:
        if asset.get("has_audio", False):
            _fit_replacement_to_segment(companion, asset)
            companion["enabled"] = True
        else:
            companion["enabled"] = False


def _apply_operator_action(state: dict[str, Any], action: dict[str, Any], cursor: int) -> None:
    segments = state.setdefault("segments", [])
    tracks = state.setdefault("tracks", [])
    action_type = action["type"]
    segment = next((item for item in segments if item.get("id") == action.get("clip_id")), None)
    if action_type == "add_clip":
        asset = next((item for item in state.get("assets", []) if item.get("id") == action.get("asset_id")), None)
        track = next((item for item in tracks if item.get("id") == action.get("track_id")), None)
        if not asset or not track or track.get("locked"):
            return
        start = max(0.0, float(action.get("source_start", 0)))
        end = min(float(asset.get("duration", start)), float(action.get("source_end", asset.get("duration", start))))
        if end - start < 0.04:
            return
        kind = "audio" if track.get("kind") == "audio" else "video"
        clip_id = str(action.get("result_clip_id") or f"operator-{cursor}-clip")
        action["result_clip_id"] = clip_id
        segments.append({
            "id": clip_id, "label": str(asset.get("name") or asset.get("id")), "kind": kind,
            "asset_id": asset["id"], "track_id": track["id"],
            "audio_track_id": "a1" if kind == "video" and asset.get("has_audio", False) else None,
            "audio_linked": kind == "video" and bool(asset.get("has_audio", False)),
            "timeline_start": max(0.0, float(action.get("timeline_start", 0))),
            "source_start": start, "source_end": end, "speed": 1, "volume": 1,
            "transform": {"scale": 1, "x": 0, "y": 0, "rotation": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "crop_left": 0},
        })
    elif action_type == "replace_clip" and segment and not _operator_segment_locked(state, segment):
        asset = next((item for item in state.get("assets", []) if item.get("id") == action.get("asset_id")), None)
        if asset and segment.get("kind") in {"video", "audio"}:
            _replace_operator_clip(state, segment, asset)
    elif action_type == "move_clip" and segment and not _operator_segment_locked(state, segment):
        if action.get("track_id") and any(item.get("id") == action["track_id"] for item in tracks):
            segment["track_id"] = action["track_id"]
        if action.get("timeline_start") is not None:
            segment["timeline_start"] = max(0.0, float(action["timeline_start"]))
    elif action_type == "trim_clip" and segment and not _operator_segment_locked(state, segment) and segment.get("kind") not in {"title", "caption"}:
        if action.get("source_start") is not None:
            segment["source_start"] = max(0.0, min(float(action["source_start"]), float(segment.get("source_end", 0)) - 0.04))
        if action.get("source_end") is not None:
            segment["source_end"] = max(float(segment.get("source_start", 0)) + 0.04, float(action["source_end"]))
    elif action_type == "delete_clip" and segment and not _operator_segment_locked(state, segment):
        state["segments"] = [item for item in segments if item.get("id") != segment.get("id")]
    elif action_type == "set_clip" and segment and not _operator_segment_locked(state, segment):
        nested = {"transform", "color", "transition_in", "transition_out"}
        for key, value in action.get("patch", {}).items():
            if key in {"id", "project_id"}:
                continue
            if key in nested and isinstance(value, dict):
                segment[key] = {**(segment.get(key) or {}), **value}
            else:
                segment[key] = value
    elif action_type == "split_clip" and segment and not _operator_segment_locked(state, segment) and segment.get("kind") not in {"title", "caption"}:
        split_time = float(action.get("time", 0))
        timeline_start = float(segment.get("timeline_start", 0))
        duration = _operator_duration(segment)
        if timeline_start + 0.04 < split_time < timeline_start + duration - 0.04:
            source_time = float(segment.get("source_start", 0)) + (split_time - timeline_start) * max(0.1, float(segment.get("speed", 1)))
            right = json.loads(json.dumps(segment))
            right_id = str(action.get("result_clip_id") or f"operator-{cursor}-split")
            action["result_clip_id"] = right_id
            right.update({"id": right_id, "source_start": source_time, "timeline_start": split_time})
            segment["source_end"] = source_time
            segments.append(right)
    elif action_type == "add_track" and action.get("kind") in {"video", "audio"}:
        kind = action["kind"]
        count = len([item for item in tracks if item.get("kind") == kind]) + 1
        track_id = str(action.get("result_track_id") or f"{'v' if kind == 'video' else 'a'}{count}")
        action["result_track_id"] = track_id
        if not any(item.get("id") == track_id for item in tracks):
            tracks.append({"id": track_id, "kind": kind, "name": track_id.upper(), "muted": False, "locked": False})
    elif action_type == "unlink_audio" and segment and segment.get("kind") == "video" and segment.get("audio_linked"):
        audio = json.loads(json.dumps(segment))
        audio_id = str(action.get("result_clip_id") or f"operator-{cursor}-audio")
        action["result_clip_id"] = audio_id
        audio.update({"id": audio_id, "kind": "audio", "label": f"{segment.get('label', 'Clip')} audio", "track_id": segment.get("audio_track_id") or "a1", "audio_track_id": None, "audio_linked": False})
        segment["audio_track_id"] = None
        segment["audio_linked"] = False
        segments.append(audio)
    elif action_type in {"add_title", "add_caption"}:
        kind = "title" if action_type == "add_title" else "caption"
        clip_id = str(action.get("result_clip_id") or f"operator-{cursor}-{kind}")
        action["result_clip_id"] = clip_id
        duration = max(0.1, float(action.get("duration", 3)))
        text = str(action.get("text") or ("New title" if kind == "title" else "New caption"))
        segments.append({"id": clip_id, "label": text, "kind": kind, "asset_id": None, "track_id": "v1", "timeline_start": max(0.0, float(action.get("timeline_start", 0))), "source_start": 0, "source_end": duration, "duration": duration, "text": text, "font_size": 64 if kind == "title" else 72, "text_color": "#ffffff"})
    elif action_type == "set_export_preset" and action.get("preset") in EXPORT_PRESETS:
        state["export_preset"] = action["preset"]


class OperatorBridge:
    ACTIONS = {
        "add_clip", "replace_clip", "move_clip", "trim_clip", "delete_clip", "set_clip", "split_clip",
        "add_track", "unlink_audio", "add_title", "add_caption", "set_export_preset",
    }

    def __init__(self, root: Path | None = None):
        self._root = root
        self._clients: dict[str, dict[str, Any]] = {}
        self._active_client_id: str | None = None
        self._project_states: dict[str, dict[str, Any]] = {}
        self._project_revisions: dict[str, int] = {}
        self._batch_results: dict[tuple[str, str], dict[str, Any]] = {}
        self._actions: list[dict[str, Any]] = []
        self._cursor = 0
        self._lock = threading.Lock()

    def set_context(self, context: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            incoming = json.loads(json.dumps(context))
            project = incoming.get("project") if isinstance(incoming.get("project"), dict) else {}
            incoming_state = incoming.get("state") if isinstance(incoming.get("state"), dict) else None
            project_id = str(project.get("id") or (incoming_state or {}).get("project_id") or "")
            if not project_id and self._root:
                raise ValueError("Operator context requires a project id")
            if not project_id:
                project_id = "__legacy__"

            if self._root:
                media_id = str(incoming.get("media") or (incoming_state or {}).get("media") or "") or None
                self._ensure_project_locked(project_id, media_id)
            elif incoming_state is not None and project_id not in self._project_states:
                self._project_states[project_id] = incoming_state
                self._project_revisions[project_id] = max(0, int(incoming.get("revision", 0)))

            client_id = str(incoming.get("client_id") or "legacy")[:120]
            page_id = str(incoming.get("page_id") or client_id)[:120]
            record = {
                "client_id": client_id,
                "page_id": page_id,
                "project": {"id": project_id, "name": str(project.get("name") or project_id)},
                "playhead": max(0.0, float(incoming.get("playhead", 0))),
                "selected_clip_id": incoming.get("selected_clip_id"),
                "visible": bool(incoming.get("visible", True)),
                "focused": bool(incoming.get("focused", True)),
                "dirty": bool(incoming.get("dirty", False)),
            }
            self._clients[page_id] = record
            if record["focused"] or self._active_client_id is None or self._active_client_id == page_id:
                self._active_client_id = page_id
            return self._snapshot_locked(project_id, record)

    def context(self) -> dict[str, Any]:
        with self._lock:
            record = self._active_record_locked()
            if not record:
                return {}
            return self._snapshot_locked(record["project"]["id"], record)

    def publish_state(self, project_id: str, state: dict[str, Any], page_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            normalized = json.loads(json.dumps(state))
            self._project_states[project_id] = normalized
            revision = self._project_revisions.get(project_id, 0) + 1
            self._project_revisions[project_id] = revision
            record = self._clients.get(page_id or "") or self._active_record_locked()
            if record and record["project"]["id"] != project_id:
                record = None
            if record:
                record["dirty"] = False
            return self._snapshot_locked(project_id, record)

    def add(self, data: dict[str, Any]) -> dict[str, Any]:
        raw_actions = data.get("actions", [data])
        if not isinstance(raw_actions, list) or not raw_actions:
            raise ValueError("Provide a non-empty actions array")
        with self._lock:
            record = self._active_record_locked()
            project_id = str(data.get("project_id") or (record or {}).get("project", {}).get("id") or "")
            if self._root and not project_id:
                raise ValueError("Operator actions require project_id")
            if not project_id:
                project_id = "__legacy__"
            if self._root:
                self._ensure_project_locked(project_id)
                if record and record["project"]["id"] == project_id and record.get("dirty"):
                    raise ValueError("Save manual timeline changes before applying Operator actions")

            current_revision = self._project_revisions.get(project_id, 0)
            if self._root:
                batch_id = str(data.get("batch_id") or "")[:160]
                if not batch_id:
                    raise ValueError("Operator actions require a stable batch_id")
                batch_key = (project_id, batch_id)
                if batch_key in self._batch_results:
                    return json.loads(json.dumps(self._batch_results[batch_key]))
                if "base_revision" not in data:
                    raise ValueError("Operator actions require base_revision from /api/operator/context")
                if int(data["base_revision"]) != current_revision:
                    raise ValueError(f"Stale Operator revision {data['base_revision']}; current revision is {current_revision}")
            else:
                batch_id = str(data.get("batch_id") or data.get("sync_id") or uuid.uuid4().hex)[:160]
                batch_key = (project_id, batch_id)
                if batch_key in self._batch_results:
                    return json.loads(json.dumps(self._batch_results[batch_key]))

            working_state = json.loads(json.dumps(self._project_states.get(project_id))) if project_id in self._project_states else None
            accepted: list[dict[str, Any]] = []
            for index, raw in enumerate(raw_actions, start=1):
                if not isinstance(raw, dict) or raw.get("type") not in self.ACTIONS:
                    raise ValueError(f"Unknown Operator action: {raw.get('type') if isinstance(raw, dict) else raw}")
                cursor = self._cursor + index
                action = {
                    **raw,
                    "project_id": project_id,
                    "batch_id": batch_id,
                    "base_revision": current_revision,
                    "cursor": cursor,
                }
                if working_state is not None:
                    _apply_operator_action(working_state, action, cursor)
                accepted.append(action)

            if working_state is not None and self._root:
                media_id = str(working_state.get("media") or "")
                if not media_id:
                    raise ValueError("Authoritative project state has no media version")
                working_state = save_editor_state(self._root, project_id, media_id, working_state)

            self._cursor += len(accepted)
            self._actions.extend(accepted)
            self._actions = self._actions[-500:]
            if working_state is not None:
                self._project_states[project_id] = working_state
            revision = current_revision + 1
            self._project_revisions[project_id] = revision
            result = {
                "ok": True,
                "project_id": project_id,
                "batch_id": batch_id,
                "actions": accepted,
                "cursor": self._cursor,
                "revision": revision,
                "state": json.loads(json.dumps(working_state)),
            }
            self._batch_results[batch_key] = json.loads(json.dumps(result))
            return result

    def poll(self, cursor: int, project_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            record = self._active_record_locked()
            resolved_project_id = str(project_id or (record or {}).get("project", {}).get("id") or "")
            if resolved_project_id and self._root:
                self._ensure_project_locked(resolved_project_id)
            actions = [
                item for item in self._actions
                if item["cursor"] > cursor and (not resolved_project_id or item.get("project_id") == resolved_project_id)
            ]
            state = self._project_states.get(resolved_project_id) if resolved_project_id else None
            revision = self._project_revisions.get(resolved_project_id, 0)
            return {
                "actions": json.loads(json.dumps(actions)),
                "cursor": self._cursor,
                "project_id": resolved_project_id or None,
                "revision": revision,
                "state": json.loads(json.dumps(state)),
            }

    def _ensure_project_locked(self, project_id: str, media_id: str | None = None) -> None:
        if project_id in self._project_states:
            return
        if not self._root:
            self._project_states[project_id] = {}
            self._project_revisions[project_id] = 0
            return
        self._project_states[project_id] = load_editor_state(self._root, project_id, media_id)
        self._project_revisions[project_id] = 1

    def _active_record_locked(self) -> dict[str, Any] | None:
        if self._active_client_id and self._active_client_id in self._clients:
            return self._clients[self._active_client_id]
        if not self._clients:
            return None
        return self._clients[next(reversed(self._clients))]

    def _snapshot_locked(self, project_id: str, record: dict[str, Any] | None) -> dict[str, Any]:
        state = self._project_states.get(project_id)
        snapshot = {
            "project_id": project_id,
            "revision": self._project_revisions.get(project_id, 0),
            "state": json.loads(json.dumps(state)),
        }
        if record:
            snapshot.update(json.loads(json.dumps(record)))
        if state:
            snapshot["sequence_duration"] = max(
                (float(item.get("timeline_start", 0)) + _operator_duration(item) for item in state.get("segments", [])),
                default=0.0,
            )
        return snapshot


class EditorRequestHandler(BaseHTTPRequestHandler):
    server_version = "LLMVideoEditor/0.1"

    @property
    def editor_root(self) -> Path:
        return self.server.editor_root  # type: ignore[attr-defined]

    @property
    def terminal_manager(self) -> TerminalManager:
        return self.server.terminal_manager  # type: ignore[attr-defined]

    @property
    def render_manager(self) -> RenderManager:
        return self.server.render_manager  # type: ignore[attr-defined]

    @property
    def operator_bridge(self) -> OperatorBridge:
        return self.server.operator_bridge  # type: ignore[attr-defined]

    @property
    def session_token(self) -> str:
        return self.server.session_token  # type: ignore[attr-defined]

    @property
    def allowed_authorities(self) -> set[str]:
        return self.server.allowed_authorities  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[editor] {format % args}")

    def _json(self, data: Any, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: Exception, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"error": str(error)}, status)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("Editor state is too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _validate_host(self) -> None:
        authority = self.headers.get("Host", "").lower().rstrip(".")
        if authority not in self.allowed_authorities:
            raise PermissionError("Unrecognized editor Host header")

    def _request_token(self) -> str:
        header = self.headers.get("X-LLMVE-Token", "")
        if header:
            return header
        for part in self.headers.get("Cookie", "").split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == "llmve_session":
                return value
        return ""

    def _require_session(self) -> None:
        if not secrets.compare_digest(self._request_token(), self.session_token):
            raise PermissionError("Editor session authentication required")

    def _validate_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        parsed = urlparse(origin)
        if parsed.scheme != "http" or parsed.netloc.lower().rstrip(".") not in self.allowed_authorities:
            raise PermissionError("Cross-origin editor request rejected")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            self._validate_host()
            if parsed.path == "/api/session":
                self._json(
                    {"ok": True},
                    headers={"Set-Cookie": f"llmve_session={self.session_token}; HttpOnly; SameSite=Strict; Path=/"},
                )
                return
            if parsed.path.startswith("/api/") or parsed.path.startswith("/media/"):
                self._require_session()
            if parsed.path == "/api/terminal/status":
                self._json({**codex_status(), "full_access": self.terminal_manager.full_access})
                return
            if parsed.path == "/api/operator/context":
                self._json(self.operator_bridge.context())
                return
            if parsed.path == "/api/operator/actions":
                cursor = int(parse_qs(parsed.query).get("cursor", ["0"])[0])
                project_id = unquote(parse_qs(parsed.query).get("project_id", [""])[0]) or None
                self._json(self.operator_bridge.poll(cursor, project_id))
                return
            if parsed.path.startswith("/api/terminal/") and parsed.path.endswith("/output"):
                parts = parsed.path.strip("/").split("/")
                session = self.terminal_manager.get(parts[2])
                cursor = int(parse_qs(parsed.query).get("cursor", ["0"])[0])
                self._json(session.output(cursor))
                return
            if parsed.path == "/api/projects":
                self._json({"projects": discover_projects(self.editor_root)})
                return
            if parsed.path.startswith("/api/render/"):
                job_id = parsed.path.removeprefix("/api/render/")
                self._json(self.render_manager.get(job_id).snapshot())
                return
            if parsed.path.startswith("/api/projects/"):
                suffix = parsed.path.removeprefix("/api/projects/")
                if suffix.endswith("/check"):
                    project_id = unquote(suffix.removesuffix("/check"))
                    media_id = parse_qs(parsed.query).get("media", [None])[0]
                    from .checks import check_project

                    self._json(check_project(self.editor_root, project_id, media_id=media_id))
                    return
                project_id = unquote(suffix)
                media_id = parse_qs(parsed.query).get("media", [None])[0]
                self._json(load_editor_state(self.editor_root, project_id, media_id))
                return
            if parsed.path.startswith("/media/"):
                parts = parsed.path.split("/", 3)
                if len(parts) != 4:
                    raise FileNotFoundError("Invalid media path")
                media = _resolve_asset(self.editor_root, unquote(parts[2]), unquote(parts[3]))
                self._serve_media(media)
                return
            self._serve_static(parsed.path)
        except FileNotFoundError as error:
            self._error(error, HTTPStatus.NOT_FOUND)
        except PermissionError as error:
            self._error(error, HTTPStatus.FORBIDDEN)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            self._error(error)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            self._validate_host()
            self._validate_origin()
            self._require_session()
            if parsed.path == "/api/terminal/start":
                data = self._body()
                session, reused = self.terminal_manager.attach_or_start(data.get("columns", 50), data.get("rows", 30))
                self._json({"ok": True, "id": session.id, "reused": reused})
                return
            if parsed.path == "/api/terminal/restart":
                data = self._body()
                session = self.terminal_manager.restart(data.get("columns", 50), data.get("rows", 30))
                self._json({"ok": True, "id": session.id, "reused": False})
                return
            if parsed.path == "/api/operator/context":
                context = self.operator_bridge.set_context(self._body())
                self._json({"ok": True, **context})
                return
            if parsed.path == "/api/operator/actions":
                self._json(self.operator_bridge.add(self._body()), HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/operator/resolve-paths":
                data = self._body()
                names = data.get("names", [])
                if not isinstance(names, list):
                    raise ValueError("names must be an array")
                self._json({"paths": resolve_operator_paths(self.editor_root, [str(item) for item in names[:100]])})
                return
            if parsed.path == "/api/operator/files":
                query = parse_qs(parsed.query)
                filename = unquote(query.get("filename", [""])[0])
                project_id = unquote(query.get("project", [""])[0]) or None
                length = int(self.headers.get("Content-Length") or self.headers.get("X-File-Size", "0"))
                if length <= 0:
                    raise ValueError("The dropped file is empty")
                uploaded = import_operator_file(self.editor_root, filename, self.rfile, length, project_id)
                self._json({"ok": True, "file": uploaded}, HTTPStatus.CREATED)
                return
            if parsed.path == "/api/projects/new":
                query = parse_qs(parsed.query)
                filename = unquote(query.get("filename", [""])[0])
                name = unquote(query.get("name", [Path(filename).stem])[0])
                length = int(self.headers.get("Content-Length") or self.headers.get("X-File-Size", "0"))
                if length <= 0:
                    raise ValueError("The uploaded media file is empty")
                project, asset = create_project_from_media(self.editor_root, name, filename, self.rfile, length)
                self._json({"ok": True, "project": project, "asset": asset}, HTTPStatus.CREATED)
                return
            if parsed.path.startswith("/api/terminal/"):
                parts = parsed.path.strip("/").split("/")
                if len(parts) != 4:
                    raise FileNotFoundError("Unknown terminal route")
                session_id, action = parts[2], parts[3]
                if action == "stop":
                    self.terminal_manager.stop(session_id)
                else:
                    session = self.terminal_manager.get(session_id)
                    data = self._body()
                    if action == "input":
                        session.write(str(data.get("data", "")))
                    elif action == "resize":
                        session.resize(data.get("columns", 50), data.get("rows", 30))
                    elif action == "interrupt":
                        session.interrupt()
                    else:
                        raise FileNotFoundError("Unknown terminal route")
                self._json({"ok": True})
                return
            parts = parsed.path.strip("/").split("/")
            if len(parts) not in (3, 4) or parts[:2] != ["api", "projects"]:
                raise FileNotFoundError("Unknown API route")
            project_id = unquote(parts[2])
            if len(parts) == 4 and parts[3] == "check":
                media_id = parse_qs(parsed.query).get("media", [None])[0]
                if not media_id:
                    raise ValueError("A media version is required")
                _, media = _resolve_media(self.editor_root, project_id, media_id)
                state = _normalize_state(self._body(), project_id, media.name, probe(media))
                manifest = sync_asset_manifest(self.editor_root, project_id, state=state, probe_media=probe)
                attach_manifest_to_state(state, manifest)
                self._json(check_state(self.editor_root, project_id, state, manifest=manifest))
                return
            if len(parts) == 4 and parts[3] == "duplicate":
                data = self._body()
                project = duplicate_editor_project(self.editor_root, project_id, str(data.get("name") or "") or None)
                self._json({"ok": True, "project": project}, HTTPStatus.CREATED)
                return
            if len(parts) == 4 and parts[3] == "media":
                filename = unquote(parse_qs(parsed.query).get("filename", [""])[0])
                length = int(self.headers.get("Content-Length") or self.headers.get("X-File-Size", "0"))
                if length <= 0:
                    raise ValueError("The uploaded media file is empty")
                asset = import_project_media(self.editor_root, project_id, filename, self.rfile, length)
                self._json({"ok": True, "asset": asset}, HTTPStatus.CREATED)
                return
            media_id = parse_qs(parsed.query).get("media", [None])[0]
            if len(parts) == 4 and parts[3] == "trash":
                destination = trash_project_media(self.editor_root, project_id)
                self._json({"ok": True, "trashed": str(destination)})
                return
            if not media_id:
                raise ValueError("A media version is required")
            data = self._body()
            if len(parts) == 4 and parts[3] == "render":
                preset = str(parse_qs(parsed.query).get("preset", [data.get("export_preset", "source")])[0])
                job = self.render_manager.start(project_id, media_id, data, preset)
                self._json({"ok": True, "job_id": job.id}, HTTPStatus.ACCEPTED)
            elif len(parts) == 3:
                state = save_editor_state(self.editor_root, project_id, media_id, data)
                page_id = self.headers.get("X-Editor-Page-ID")
                operator = self.operator_bridge.publish_state(project_id, state, page_id)
                self._json({"ok": True, "state": state, "project_id": project_id, "revision": operator["revision"]})
            else:
                raise FileNotFoundError("Unknown API route")
        except FileNotFoundError as error:
            self._error(error, HTTPStatus.NOT_FOUND)
        except PermissionError as error:
            self._error(error, HTTPStatus.FORBIDDEN)
        except (json.JSONDecodeError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            self._error(error)

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
        path = (UI_DIR / relative).resolve()
        if UI_DIR.resolve() not in path.parents or not path.is_file():
            raise FileNotFoundError("UI asset not found")
        body = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _serve_media(self, path: Path) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                start = int(match.group(1) or 0)
                end = min(int(match.group(2) or size - 1), size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
        if start > end or start >= size:
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


def serve_editor(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    *,
    operator_full_access: bool = False,
) -> None:
    server = ThreadingHTTPServer((host, port), EditorRequestHandler)
    server.editor_root = root.resolve()  # type: ignore[attr-defined]
    url = f"http://{host}:{server.server_port}"
    server.session_token = secrets.token_urlsafe(32)  # type: ignore[attr-defined]
    server.allowed_authorities = {  # type: ignore[attr-defined]
        f"127.0.0.1:{server.server_port}",
        f"localhost:{server.server_port}",
        f"[::1]:{server.server_port}",
        f"{host.lower().rstrip('.')}:{server.server_port}",
    }
    server.terminal_manager = TerminalManager(  # type: ignore[attr-defined]
        root,
        url,
        server.session_token,  # type: ignore[attr-defined]
        operator_full_access,
    )
    server.render_manager = RenderManager(root)  # type: ignore[attr-defined]
    server.operator_bridge = OperatorBridge(root)  # type: ignore[attr-defined]
    print(f"LLM Video Editor UI: {url}")
    print("Press Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping editor.")
    finally:
        server.terminal_manager.close()  # type: ignore[attr-defined]
        server.render_manager.close()  # type: ignore[attr-defined]
        server.server_close()
