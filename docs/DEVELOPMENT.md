# Development and RecCli

## Repository map

- `src/video_pipeline/editor.py` — project model, local server, structured actions, and FFmpeg timeline construction.
- `src/video_pipeline/ui/` — browser/WebKit editor and embedded terminal interface.
- `src/video_pipeline/pipeline.py` — one-clip Auto-Editor finishing pipeline.
- `assets.py`, `inspection.py`, and `checks.py` — media identity, agent-readable inspection, and validation.
- `premiere.py` and `diffusion_bridge.py` — external interchange boundaries.
- `macos/NativeApp/` — AVFoundation/WebKit macOS client.
- `tests/` — unit and local integration coverage that does not require media tools.

See [../ARCHITECTURE.md](../ARCHITECTURE.md) before changing a canonical boundary.

## Local environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

FFmpeg is required for real media operations. Auto-Editor and Whisper are workflow-specific rather than test dependencies. Use `lve --dry-run cut <clip>` when reviewing generated finishing commands without executing them.

## Verification ladder

1. Run the narrow test module for the code being changed.
2. Run `pytest -q` and `python -m compileall -q src run.py`.
3. For timeline or renderer work, run structural `lve check`, canonical `lve capture`, and one representative render.
4. For deep-check changes, inspect the cached JSON and contact sheet—not only the process exit status.
5. For Swift changes, run `swift build` in `macos/NativeApp/`; use `./macos/build_native_app.sh` only when an app bundle is needed.

Generated bundles and Swift package caches are ignored. Do not commit `.build/`, `build/`, footage, renders, model weights, transcripts, or personal agent history.

## RecCli memory

For RecCli users, register the checkout as `llm-video-editor`. Suggested feature areas for its local `.devproject` are:

1. deterministic export pipeline
2. manual editor and timeline state
3. transcription workflow
4. asset registry and media inspection
5. project validation and format checks
6. Premiere Pro import
7. Diffusion Studio motion sidecar
8. native macOS editor
9. testing

At session start, load project context using this repository as the working directory. Use history search when a decision or file has prior context. At the end of meaningful work, save a session note containing the outcome, decisions, problems solved, open issues, next steps, and exact files changed.

`llm-video-editor.devproject` and `devsession/` remain local and ignored. They can contain absolute paths and private editing history. After moving a checkout or intentionally rewriting memory, run RecCli doctor; rebuild the index if it still contains stale search previews.

## Change discipline

- Preserve unrelated user work in ignored project directories.
- Keep project mutations revisioned and idempotent where they cross the Operator API.
- Maintain path-containment checks for all project, media, and upload endpoints.
- Do not weaken token, cookie, Host/Origin, or approval protections for local convenience.
- Add a test with each bug fix when the behavior can be reproduced without external media.
- Update README or the relevant document when a command, canonical record, trust boundary, or portability rule changes.
