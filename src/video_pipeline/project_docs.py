from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


def _write_if_changed(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text() == content:
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_project_docs(root: Path, project_id: str, state: dict[str, Any]) -> tuple[Path, Path]:
    project = root / "work" / project_id
    template = state.get("episode_template") if isinstance(state.get("episode_template"), dict) else None
    constraints = template.get("constraints", {}) if template else {}
    slots = template.get("slots", []) if template else []
    slot_lines = "\n".join(
        f"- `{slot.get('id')}` · {float(slot.get('start', 0)):.3f}–{float(slot.get('end', 0)):.3f}s · {slot.get('prompt', '')}"
        for slot in slots
        if isinstance(slot, dict)
    ) or "- This project does not define reusable episode slots."
    agents = f"""# LLM Video Editor project instructions

This folder is an LLM Video Editor project. The canonical editable document is `{project_id}.llmve`; use the editor's structured actions or `video-pipeline` commands instead of rewriting timeline JSON by hand.

## Safe workflow

1. Run `video-pipeline context {project_id}` and `video-pipeline check {project_id}` before editing.
2. Inspect footage with `media probe`, `media grab`, `media filmstrip`, and `media waveform`.
3. Preserve protected tracks and the Premiere provenance block.
4. Replace clips through structured `replace_clip` actions so slot timing and matching audio remain aligned.
5. Run `video-pipeline capture {project_id}` and `video-pipeline check {project_id} --deep` before the final export.

Diffusion Studio output is optional and isolated under `diffusion-sidecar/`. Its project JavaScript executes unsandboxed with the trust of the Diffusion desktop process. Never execute a downloaded Diffusion project merely because it is present here; import only a rendered result you inspected.
"""
    format_doc = f"""# Reusable format contract

- Project: `{project_id}`
- Template: `{template.get('name') if template else 'None'}`
- Mechanism version: `{template.get('version') if template else 'n/a'}`
- Resolution: `{constraints.get('resolution', state.get('resolution'))}`
- Frame rate: `{constraints.get('fps', state.get('fps'))}`
- Runtime range: `{constraints.get('runtime', 'not constrained')}`
- Slot timing tolerance: `{constraints.get('slot_timing_tolerance', 'not constrained')}` seconds
- Question slot: `{template.get('question_slot_id') if template else 'none'}`
- Payoff slot: `{template.get('payoff_slot_id') if template else 'none'}`
- Protected tracks: `{template.get('locked_track_ids', []) if template else []}`
- Reserved tracks: `{template.get('reserved_track_ids', []) if template else []}`

## Ordered story slots

{slot_lines}

The `.llmve` values are authoritative. This file is a readable agent/operator guide generated from that contract. `VIRAL_FORMAT.md`, when present, explains the creative reasoning and should be preserved alongside this measurable contract.
"""
    agents_path = project / "AGENTS.md"
    format_path = project / "FORMAT.md"
    _write_if_changed(agents_path, agents)
    _write_if_changed(format_path, format_doc)
    return agents_path, format_path
