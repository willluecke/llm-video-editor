from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .assets import import_asset_path
from .checks import segment_duration


DIFFUSION_EDITOR_PIN = "aeb873bfc6c239a05821c9af307fca34a06e8928"


def _safe_id(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return normalized[:80] or fallback


def _jsx_text(value: str) -> str:
    return "{" + json.dumps(value) + "}"


def _static_node(segment: dict[str, Any], path: Path | None, width: int, height: int) -> str | None:
    kind = str(segment.get("kind", ""))
    node_id = _safe_id(str(segment.get("id", "")), "clip")
    start = float(segment.get("timeline_start", 0))
    end = start + segment_duration(segment)
    if end <= start:
        return None
    if kind in {"video", "audio"}:
        if not path:
            return None
        source_in = float(segment.get("source_start", 0))
        source_out = float(segment.get("source_end", source_in))
        rate = max(0.1, float(segment.get("speed", 1)))
        src = json.dumps(str(path.resolve()))
        if kind == "audio":
            linear_volume = max(0.000001, float(segment.get("volume", 1)))
            volume_db = max(-96.0, 20 * math.log10(linear_volume))
            return (
                f"        <audio id={json.dumps(node_id)} src={{{src}}} start={{{start:.6f}}} end={{{end:.6f}}} "
                f"sourceIn={{{source_in:.6f}}} sourceOut={{{source_out:.6f}}} playbackRate={{{rate:.6f}}} "
                f"volume={{{volume_db:.6f}}} />"
            )
        transform = segment.get("transform", {})
        scale = max(0.1, float(transform.get("scale", 1)))
        x = float(transform.get("x", 0))
        y = float(transform.get("y", 0))
        return (
            f"        <video id={json.dumps(node_id)} src={{{src}}} start={{{start:.6f}}} end={{{end:.6f}}} "
            f"sourceIn={{{source_in:.6f}}} sourceOut={{{source_out:.6f}}} playbackRate={{{rate:.6f}}} "
            f"width={{{width}}} height={{{height}}} objectFit=\"cover\" x={{{x:.3f}}} y={{{y:.3f}}} "
            f"scale={{{scale:.6f}}} muted={{{str(not bool(segment.get('audio_linked', False))).lower()}}} />"
        )
    if kind in {"title", "caption"}:
        font_size = int(segment.get("font_size", 64))
        y = round(height * (0.79 if kind == "caption" else 0.10))
        text = str(segment.get("text") or segment.get("label") or "")
        return (
            f"        <text id={json.dumps(node_id)} start={{{start:.6f}}} end={{{end:.6f}}} "
            f"x={{0}} y={{{y}}} width={{{width}}} height={{{max(100, round(height * 0.16))}}} "
            f"fontSize={{{font_size}}} color={json.dumps(str(segment.get('text_color', '#ffffff')))} "
            f"textAlign=\"center\">{_jsx_text(text)}</text>"
        )
    return None


def generate_diffusion_sidecar(
    root: Path,
    project_id: str,
    *,
    output: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    safe_project = Path(project_id).name
    if safe_project != project_id:
        raise ValueError("Invalid project id")
    from .editor import _resolve_asset, load_editor_state

    state = load_editor_state(root, project_id)
    destination = (output or root / "work" / project_id / "diffusion-sidecar").expanduser().resolve()
    generated = ["index.tsx", "package.json", "tsconfig.json", "llmve-map.json", ".diffusion-pin.json", "README.md"]
    if destination.exists() and not force and any((destination / name).exists() for name in generated):
        raise FileExistsError(f"Diffusion sidecar already exists: {destination}; pass --force to refresh generated files")
    destination.mkdir(parents=True, exist_ok=True)
    width, height = [int(value) for value in state.get("resolution", [1920, 1080])]
    track_order = {str(track.get("id")): index for index, track in enumerate(state.get("tracks", []))}
    segments = sorted(
        (item for item in state.get("segments", []) if item.get("enabled", True)),
        key=lambda item: (track_order.get(str(item.get("track_id")), 999), float(item.get("timeline_start", 0))),
    )
    nodes: list[str] = []
    mapping: list[dict[str, Any]] = []
    for segment in segments:
        path: Path | None = None
        if segment.get("asset_id"):
            try:
                path = _resolve_asset(root, project_id, str(segment["asset_id"]))
            except FileNotFoundError:
                path = None
        node = _static_node(segment, path, width, height)
        if not node:
            continue
        nodes.append(node)
        mapping.append({
            "llmve_id": segment.get("id"),
            "diffusion_id": _safe_id(str(segment.get("id", "")), "clip"),
            "kind": segment.get("kind"),
            "asset_id": segment.get("asset_id"),
            "slot_id": segment.get("slot_id"),
        })
    source = "\n".join([
        "// Generated static interchange. Review before opening in Diffusion Studio.",
        "export default function LLMVideoEditorSidecar() {",
        "  return (",
        "    <stage background=\"#000000\">",
        f"      <scene id=\"llmve_scene\" name={json.dumps(str(state.get('project', {}).get('name') or project_id))} width={{{width}}} height={{{height}}} fill=\"black\" active>",
        *nodes,
        "      </scene>",
        "    </stage>",
        "  );",
        "}",
        "",
    ])
    package = {
        "name": f"llmve-{_safe_id(project_id, 'project').lower()}",
        "private": True,
        "version": "0.0.0",
        "main": "index.tsx",
        "projectId": f"llmve-{_safe_id(project_id, 'project').lower()}",
        "displayName": f"{state.get('project', {}).get('name') or project_id} · motion sidecar",
        "devDependencies": {"@diffusionstudio/jsx": "0.200.0", "solid-js": "^1.9.9"},
    }
    tsconfig = {
        "compilerOptions": {
            "target": "es2024",
            "module": "ESNext",
            "moduleResolution": "bundler",
            "moduleDetection": "force",
            "jsx": "preserve",
            "jsxImportSource": "@diffusionstudio/jsx",
            "strict": True,
            "skipLibCheck": True,
            "noEmit": True,
        },
        "include": ["index.tsx"],
    }
    pin = {
        "format": "llm-video-editor-diffusion-sidecar",
        "version": 1,
        "diffusion_editor_commit": DIFFUSION_EDITOR_PIN,
        "source_project": project_id,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trust": "Project JavaScript executes unsandboxed in Diffusion Studio. Review this folder before opening it.",
    }
    readme = f"""# Diffusion Studio motion sidecar

This is a static interchange generated from `{project_id}`. LLM Video Editor remains the canonical timeline and FFmpeg remains the canonical final renderer.

The bridge is pinned for review against Diffusion Studio commit `{DIFFUSION_EDITOR_PIN}`. Diffusion project modules execute unsandboxed in its desktop process. Review `index.tsx` and any future dependencies before opening this folder; do not copy downloaded project code into it blindly.

Open this folder with `dapi open {json.dumps(str(destination))}`, add motion graphics in Diffusion Studio, export a flattened video, inspect it, then run:

```bash
lve diffusion import-render {project_id} /path/to/rendered-motion.mp4
```

The imported render becomes an ordinary content-addressed LLM Editor asset. It is never executed as code inside LLM Editor.
"""
    files = {
        "index.tsx": source,
        "package.json": json.dumps(package, indent=2) + "\n",
        "tsconfig.json": json.dumps(tsconfig, indent=2) + "\n",
        "llmve-map.json": json.dumps({"project_id": project_id, "nodes": mapping}, indent=2) + "\n",
        ".diffusion-pin.json": json.dumps(pin, indent=2) + "\n",
        "README.md": readme,
    }
    for name, content in files.items():
        (destination / name).write_text(content)
    return {"project_id": project_id, "sidecar": str(destination), "nodes": len(nodes), "pin": pin}


def import_diffusion_render(root: Path, project_id: str, render: Path, *, name: str | None = None) -> dict[str, Any]:
    from .editor import load_editor_state, probe, save_editor_state

    destination, manifest = import_asset_path(root, project_id, render, name=name, probe_media=probe)
    state = load_editor_state(root, project_id)
    saved = save_editor_state(root, project_id, str(state["media"]), state)
    asset = next(item for item in saved["assets"] if item["id"] == destination.name)
    return {
        "project_id": project_id,
        "asset": asset,
        "manifest": str(root / "work" / project_id / "assets.json"),
        "content_id": next(item["content_id"] for item in manifest["assets"] if item["library_id"] == destination.name),
    }
