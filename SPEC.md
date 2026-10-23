# Product specification

LLM Video Editor is a local-first system for deterministic video editing with both direct manipulation and agent assistance. It supports authentic-footage finishing, reusable social-series formats, and loss-aware interchange with Premiere Pro and Diffusion Studio.

## Goals

1. Keep source media unchanged and make every edit reproducible from structured state.
2. Let a person and an LLM inspect and change the same timeline without hidden mutations.
3. Preserve the timing and identity mechanism of a successful series while making daily footage easy to replace.
4. Import existing Premiere work without discarding provenance or requiring media duplication.
5. Render locally with deterministic FFmpeg command construction and actionable preflight checks.
6. Keep executable third-party project code outside the trusted editor process.

## Canonical records

- The `.llmve` document is authoritative for timeline structure, project settings, template constraints, and Premiere provenance.
- `assets.json` is authoritative for file identity, link/copy mode, source location, online status, media probe data, and content-keyed cache location.
- FFmpeg is authoritative for final exports. AVFoundation is the low-latency editing preview.
- Project `AGENTS.md` and `FORMAT.md` files are generated explanations; they do not override `.llmve` values.
- Diffusion Studio sidecars are optional interchange projects. Only an inspected flattened render crosses back into the canonical asset library.

## Required behavior

- Imports and saves must preserve stable clip IDs and content hashes where media bytes are unchanged.
- Missing files, invalid source ranges, invalid speed, or a timeline without visuals must block rendering.
- Creative and format warnings must remain visible and intentionally overridable rather than silently rewriting the edit.
- Protected template tracks and required question/payoff beats must be enforceable by both UI and Operator actions.
- Local API/media requests must require the launch session; mutations must also validate local Host and Origin values.
- Operator must be approval-aware by default. Full filesystem/command access requires an explicit launch flag.
- All render, inspection, validation, transcript, and project-memory artifacts must remain local unless the user deliberately exports or pushes them.

## Non-goals

- Cloud media storage, collaborative multi-user timelines, and remote rendering are not core features.
- Diffusion Studio is not the canonical document model or renderer.
- Generated footage is not silently mixed into the authentic-footage pipeline.
- Git is not a media backup. Active projects require their own portable copy or external backup.

## Acceptance checks

- The Python test suite passes and source files compile.
- A representative project passes structural preflight and produces a canonical contact sheet.
- Deep checks report rendered black frames, silence, integrated loudness, and true peak.
- The native app builds after Swift changes.
- A generated Diffusion sidecar remains static, pinned, reviewable, and type-checkable against its declared API version.

See [README.md](README.md) for operation and [ARCHITECTURE.md](ARCHITECTURE.md) for the implementation boundaries.
