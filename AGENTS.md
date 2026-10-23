# LLM Video Editor agent guide

This repository is a local-first video editor. Preserve user media, structured project state, and deterministic output above convenience.

## Session bootstrap

1. Load RecCli project context for this repository before substantive work.
2. Treat stale paths or summaries from another machine as historical data, not current state.
3. Inspect `git status` before editing; `work/`, `out/`, models, build products, and RecCli memory are intentionally ignored.
4. Save a RecCli session note after meaningful work so the next agent receives current decisions rather than stale history.

## Canonical boundaries

- `.llmve` owns timeline and episode-template structure.
- `assets.json` owns content identity and link/copy status.
- FFmpeg owns delivery renders; AVFoundation owns interactive preview.
- Premiere import provenance is immutable unless the user explicitly reimports.
- Diffusion project JavaScript never executes inside LLM Video Editor.

Do not hand-edit a real project file merely to make validation pass. Use editor actions or the CLI, preserve filename-based timeline IDs, and never modify source footage.

## Verification

Run the smallest relevant tests while iterating, then before handoff run:

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python -m compileall -q src run.py
```

For timeline/render changes, also run `lve check <project>`, `lve capture <project>`, and a representative render. For Swift changes, build `macos/NativeApp` or run `./macos/build_native_app.sh`.

## Security

Keep the local session-token, Host/Origin, path-containment, and approval-aware Operator protections intact. Do not enable `--operator-full-access`, execute a Diffusion project, delete generated media, make linked assets portable, or rewrite Premiere provenance without an explicit user request.
