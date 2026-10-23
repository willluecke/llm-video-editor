# Combined editor architecture

LLM Video Editor keeps a deliberately split architecture: deterministic media operations form the trusted core, while agents and optional motion tools communicate through narrow interchange surfaces.

```text
Premiere .prproj ──> .llmve timeline + provenance
                         │
            ┌────────────┼─────────────┐
            │            │             │
      AVFoundation   FFmpeg export   lve inspection
        preview       (canonical)    / check / capture
            │            │             │
            └────────────┼─────────────┘
                         │
                  assets.json
            filename ID + SHA-256 ID
                         │
          optional static Diffusion sidecar
                         │
                  flattened render
                         │
                  ordinary LLMVE asset
```

## Canonical records

- `.llmve` owns timeline structure, Premiere provenance, episode-template constraints, protected/reserved tracks, and Operator revision state.
- `assets.json` owns content identity, source/project paths, storage mode, stat/probe metadata, online state, and derived-cache location.
- `AGENTS.md` and `FORMAT.md` inside a project are generated human/agent guides. The `.llmve` contract remains authoritative.
- `cache/` contains disposable content-keyed frames, filmstrips, waveforms, captures, and validation reports.

## Trust boundaries

- The browser/native client first establishes a random local server session. API and media requests require that session; mutations also enforce local Host/Origin checks.
- Operator changes the timeline through typed, revisioned, idempotent actions. Its terminal is approval-aware unless the user explicitly launches `--operator-full-access`.
- Media inspection, transcription, validation, and rendering are local processes. None of the combined core uploads footage.
- Diffusion Studio is a sidecar only. Generated TSX is reviewable static interchange and never evaluates inside LLM Editor. Its rendered output crosses back as media bytes, not executable project code.

## Render invariants

FFmpeg is the canonical renderer. Capture uses the same graph at an aspect-preserving draft resolution for fast visual verification. Deep checks analyze that rendered artifact for actual black frames, silence, loudness, and true peak. Delivery exports retain their requested resolution and encoder settings.

The structural checker runs before both UI and CLI renders. Hard source failures block the job; creative/format findings remain visible so the user can intentionally override them.
