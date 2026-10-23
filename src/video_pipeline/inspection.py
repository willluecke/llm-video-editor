from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .assets import derived_cache_root, load_asset_manifest


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f"{name} is required for media inspection")
    return executable


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        detail = "\n".join((result.stderr or result.stdout).strip().splitlines()[-30:])
        raise RuntimeError(f"Media inspection failed with status {result.returncode}: {detail or command[0]}")
    return result


def resolve_media_target(root: Path, target: str, project_id: str | None = None) -> Path:
    direct = Path(target).expanduser()
    if direct.is_file():
        return direct.resolve()
    if not project_id:
        raise FileNotFoundError(f"Media does not exist: {target}")
    manifest = load_asset_manifest(root, project_id)
    record = next((item for item in manifest["assets"] if item["library_id"] == target), None)
    if record and record.get("project_path"):
        candidate = root / str(record["project_path"])
        if candidate.is_file():
            return candidate
    from .editor import _resolve_asset

    return _resolve_asset(root, project_id, target)


def probe_media(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    result = _run([
        _tool("ffprobe"),
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(path),
    ])
    data = json.loads(result.stdout)
    streams = []
    for stream in data.get("streams", []):
        streams.append({
            key: stream.get(key)
            for key in (
                "index", "codec_name", "codec_long_name", "codec_type", "profile",
                "width", "height", "pix_fmt", "r_frame_rate", "avg_frame_rate",
                "sample_rate", "channels", "channel_layout", "duration", "bit_rate",
                "color_range", "color_space", "color_transfer", "color_primaries",
            )
            if stream.get(key) is not None
        })
    format_data = data.get("format", {})
    return {
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
        "format": format_data.get("format_name"),
        "duration": round(float(format_data.get("duration") or 0), 3),
        "bit_rate": int(format_data.get("bit_rate") or 0),
        "tags": format_data.get("tags", {}),
        "streams": streams,
    }


def _time_name(value: float) -> str:
    return f"{max(0.0, value):010.3f}".replace(".", "-")


def grab_frames(
    path: Path,
    times: Iterable[float],
    output_dir: Path,
    *,
    max_width: int = 1280,
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    for value in times:
        at = max(0.0, float(value))
        output = output_dir / f"{path.stem}-{_time_name(at)}s.png"
        _run([
            _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{at:.6f}", "-i", str(path), "-frames:v", "1",
            "-vf", f"scale={max_width}:-2:force_original_aspect_ratio=decrease",
            str(output),
        ])
        artifacts.append({"time": round(at, 3), "path": str(output)})
    return artifacts


def filmstrip_media(path: Path, output: Path, *, frames: int = 12, cell_width: int = 320) -> dict[str, Any]:
    metadata = probe_media(path)
    duration = max(0.001, float(metadata["duration"]))
    count = max(1, min(int(frames), 64))
    columns = min(4, count)
    rows = math.ceil(count / columns)
    rate = count / duration
    output.parent.mkdir(parents=True, exist_ok=True)
    _run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(path),
        "-vf",
        (
            f"fps={rate:.9f},scale={cell_width}:-2:force_original_aspect_ratio=decrease,"
            f"tile={columns}x{rows}:padding=8:margin=8:color=0x101216"
        ),
        "-frames:v", "1", str(output),
    ])
    times = [round((index + 0.5) * duration / count, 3) for index in range(count)]
    return {"path": str(output), "frames": count, "times": times, "duration": metadata["duration"]}


_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


def waveform_media(
    path: Path,
    output: Path,
    *,
    width: int = 1600,
    height: int = 320,
    silence_db: float = -40.0,
    silence_duration: float = 0.4,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    _run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-filter_complex",
        f"[0:a]aformat=channel_layouts=mono,showwavespic=s={width}x{height}:colors=0x79d6bd[v]",
        "-map", "[v]", "-frames:v", "1", str(output),
    ])
    silence = subprocess.run(
        [
            _tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
            "-af", f"silencedetect=n={silence_db}dB:d={silence_duration}", "-f", "null", "-",
        ],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    ranges: list[dict[str, float]] = []
    pending: float | None = None
    for line in silence.stderr.splitlines():
        start = _SILENCE_START.search(line)
        end = _SILENCE_END.search(line)
        if start:
            pending = float(start.group(1))
        if end and pending is not None:
            ranges.append({"start": round(pending, 3), "end": round(float(end.group(1)), 3)})
            pending = None
    return {"path": str(output), "silence_ranges": ranges, "silence_db": silence_db}


def _state_digest(state: dict[str, Any]) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:20]


def _capture_sheet(video: Path, times: list[float], output: Path, width: int, height: int) -> None:
    count = len(times)
    columns = min(4, count)
    rows = math.ceil(count / columns)
    aspect = width / max(1, height)
    cell_width = 360
    cell_height = max(202, min(640, round(cell_width / max(0.2, aspect))))
    command = [_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
    for value in times:
        command += ["-ss", f"{value:.6f}", "-i", str(video)]
    chains: list[str] = []
    for index, value in enumerate(times):
        chains.append(
            f"[{index}:v]scale={cell_width}:{cell_height}:force_original_aspect_ratio=decrease,"
            f"pad={cell_width}:{cell_height}:(ow-iw)/2:(oh-ih)/2:0x101216,"
            f"drawtext=text='{value:.3f}s':x=12:y=12:fontsize=22:fontcolor=white:"
            f"box=1:boxcolor=black@0.65:boxborderw=7[v{index}]"
        )
    layout = "|".join(f"{(index % columns) * cell_width}_{(index // columns) * cell_height}" for index in range(count))
    chains.append("".join(f"[v{index}]" for index in range(count)) + f"xstack=inputs={count}:layout={layout}:fill=0x101216[out]")
    output.parent.mkdir(parents=True, exist_ok=True)
    command += ["-filter_complex", ";".join(chains), "-map", "[out]", "-frames:v", "1", str(output)]
    _run(command)


def capture_project(
    root: Path,
    project_id: str,
    *,
    media_id: str | None = None,
    times: Iterable[float] | None = None,
    output: Path | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    from .editor import _render_asset_paths, _resolve_media, build_render_command, load_editor_state

    state = load_editor_state(root, project_id, media_id)
    selected_media = str(state["media"])
    _, source = _resolve_media(root, project_id, selected_media)
    digest = _state_digest(state)
    cache = derived_cache_root(root, project_id) / "captures" / digest
    canonical = cache / "canonical.mp4"
    cache.mkdir(parents=True, exist_ok=True)
    if refresh or not canonical.is_file():
        temporary = canonical.with_suffix(".rendering.mp4")
        temporary.unlink(missing_ok=True)
        command = build_render_command(
            _tool("ffmpeg"),
            source,
            temporary,
            state,
            asset_paths=_render_asset_paths(root, project_id, state),
            # The same filter graph and timing as final export, rendered at an
            # aspect-preserving draft resolution so contact-sheet validation
            # does not require a full-resolution mezzanine on every edit.
            preset="draft",
        )
        _run(command)
        temporary.replace(canonical)
    duration = max(
        (
            float(item.get("timeline_start", 0))
            + (
                float(item.get("duration", 0))
                if item.get("kind") in {"title", "caption"}
                else (float(item.get("source_end", 0)) - float(item.get("source_start", 0)))
                / max(0.1, float(item.get("speed", 1)))
            )
        )
        for item in state.get("segments", [])
        if item.get("enabled", True)
    )
    shots = [max(0.0, min(float(value), max(0.0, duration - 0.001))) for value in (times or [])]
    if not shots:
        shots = [duration * ratio for ratio in (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 0.999)]
    sheet = output or cache / "contact-sheet.png"
    _capture_sheet(canonical, shots, sheet, int(state["resolution"][0]), int(state["resolution"][1]))
    report = {
        "project_id": project_id,
        "media": selected_media,
        "state_digest": digest,
        "render": str(canonical),
        "contact_sheet": str(sheet),
        "times": [round(value, 3) for value in shots],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (cache / "capture.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
