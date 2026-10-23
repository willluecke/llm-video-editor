# Project data and portability

LLM Video Editor separates code, editable project state, source media, derived caches, and delivery renders. Understanding that split is the difference between a portable project and a checkout that only works on one Mac.

## Project layout

```text
work/<project>/
  <project>.llmve       canonical timeline and project settings
  assets.json           content identity and media-location manifest
  media/                copied media or symlinks to linked originals
  edits/                compatibility timeline snapshots
  transcripts/          local Whisper outputs
  cache/                disposable inspection, capture, and check artifacts
  premiere-source/      preserved import material when present
  diffusion-sidecar/    optional executable-project interchange boundary

out/<project>/          previews and delivery renders
```

The Git repository intentionally ignores `work/`, `out/`, and model weights. Git protects source code; it is not an edit or footage backup.

## Canonical files

`<project>.llmve` contains timeline tracks, segments, transforms, effects, titles, captions, active media, episode-template constraints, revision state, and Premiere provenance. Save it through the editor so compatibility snapshots and generated project guides remain synchronized.

`assets.json` keeps the human-readable filename ID used by timeline clips and a stable SHA-256 content ID for the actual bytes. It also records whether an asset is linked, copied into the project, or a rendered output; its last known source path; online/missing state; probe metadata; and content-keyed cache path.

## Linked versus portable media

Premiere imports link source footage by default. A link avoids duplicating large files, but the project depends on that original path. Inspect the current state with:

```bash
lve assets list <project>
lve context <project>
```

After moving footage, repair one asset explicitly:

```bash
lve assets relink <project> IMG_0001.MOV /new/location/IMG_0001.MOV
```

To copy linked media into `work/<project>/media/`:

```bash
lve assets make-portable <project>
# Or limit the operation:
lve assets make-portable <project> --asset IMG_0001.MOV
```

This can consume substantial disk space. Check available space and the manifest before converting a large project.

## Backup and transfer

For a durable transfer to another Mac:

1. Run `lve check <project>` and resolve missing media.
2. Run `lve assets make-portable <project>` if the recipient will not have the same source paths.
3. Copy the entire `work/<project>/` directory.
4. Copy any delivery renders from `out/<project>/` that must be preserved.
5. On the destination, place those directories under the checkout and run `lve assets sync <project>` followed by `lve check <project>`.

Caches under `work/<project>/cache/` can be discarded and regenerated. Keep the `.llmve`, `assets.json`, media, Premiere provenance, and any irreplaceable renders.

## Recovery and validation

Use `lve context` for a compact state summary, `lve check` for structural issues, and `lve check --deep` to analyze a canonical rendered draft. `lve capture` creates a contact sheet through the same timing/filter graph used for export, which is useful after relinking or moving projects.

Never “fix” a missing asset by changing a clip ID to an unrelated filename. Relink it explicitly so its content identity and provenance remain auditable.
