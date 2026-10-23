# LLM Video Editor

A local-first video editor built for repeatable edits with an LLM in the loop. It combines a multitrack browser/native editor, Premiere Pro import, deterministic FFmpeg rendering, reusable episode templates, content-addressed media, and machine-readable inspection tools. Source footage is never modified.

There are two complementary workflows:

- **Manual/agent editor:** import or assemble an `.llmve` timeline, edit it in the local UI, inspect it with Codex, validate it, and render it deterministically.
- **Fast finishing pipeline:** give `cut`, `batch`, or `watch` a raw clip and produce matching horizontal and vertical exports from one Auto-Editor cut.

Everything runs locally. The core editor does not upload footage, and optional Diffusion Studio projects remain isolated sidecars.

## What is implemented

- Multitrack video/audio editing with trim, split, speed, transforms, color controls, fades, transitions, titles, captions, keyframes, track locks, and undo history.
- Native macOS playback through AVFoundation plus the same WebKit editor and embedded Codex Operator interface.
- Premiere `.prproj` import with linked or copied media and immutable source provenance.
- Content-addressed `assets.json` manifests, explicit relinking, and portable-project conversion.
- Reusable series contracts with ordered story slots, protected identity layers, and question-before-payoff validation.
- Agent-facing JSON commands for context, probing, frame grabs, filmstrips, waveforms, captures, checks, and renders.
- Structural and rendered checks for missing media, timing, visual gaps, black frames, silence, loudness, and peak level.
- A pinned, static Diffusion Studio sidecar for optional motion graphics; FFmpeg remains the canonical renderer.

## Documentation

- [Architecture](ARCHITECTURE.md) explains canonical records, data flow, rendering, and trust boundaries.
- [Project data and portability](docs/PROJECT_DATA.md) explains `.llmve`, `assets.json`, linked media, caches, backups, and recovery.
- [Development and RecCli](docs/DEVELOPMENT.md) covers the repository layout, checks, native build, project memory, and contribution rules.
- [Product specification](SPEC.md) records the supported workflows and non-negotiable invariants.

## Quick start (macOS / Apple Silicon)

```bash
gh repo clone willluecke/llm-video-editor
cd llm-video-editor
brew install ffmpeg whisper-cpp pipx wget python@3.11
pipx install auto-editor
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

`ffmpeg` and `ffprobe` are required for editing, inspection, and rendering. Auto-Editor is required only for `cut`, `batch`, and `watch`; `whisper-cli` and a model are required only for transcription. The embedded Operator additionally expects an authenticated Codex CLI (`codex login`).

The default config uses `models/ggml-small.en.bin`. Download the verified whisper.cpp model before the first run:

```bash
mkdir -p models
wget -c https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin -O models/ggml-small.en.bin
echo 'c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d  models/ggml-small.en.bin' | shasum -a 256 -c -
```

Homebrew's `whisper-cli` does not bundle model weights. If you instead install OpenAI Whisper (`pipx install openai-whisper`), set `transcribe.engine: whisper`; model downloads are then managed by Whisper.

Optional brand inputs belong in `brand/`:

- `watermark.png` for the top-right translucent watermark
- font files for future motion graphics

Until an image asset is supplied, the pipeline uses the deterministic `brand.watermark_text` fallback from `config.yml`. Set it to an empty string to disable the fallback; missing image and text branding then produces a warning without blocking export.

## Usage

```bash
lve ui
lve context my-editor-project
lve assets sync my-editor-project
lve media filmstrip clip.mov --project my-editor-project
lve check my-editor-project
lve capture my-editor-project --time 0 --time 5 --time 15
lve render my-editor-project --preset vertical
lve transcribe my-editor-project --asset interview.mov --force
lve cut ~/Downloads/example_raw.mp4
lve cut ~/Downloads/example_raw.mp4 --targets vertical --no-brand
lve batch
lve watch
lve --dry-run cut ~/Downloads/example.mp4
```

Installing the package exposes both `lve` and `video-pipeline`; `python run.py` is an equivalent checkout-local entry point. Global flags (`--config` and `--dry-run`) go before the command. Cut options go after it. A same-named YAML sidecar beside the source clip (for example `example_raw.yml`) is deep-merged over `config.yml`.

Horizontal exports receive an `.srt` sidecar; vertical captions are burned in. Exports and transcripts land under `out/<slug>/`. A reproducibility manifest stays under `work/<slug>/`; add `--keep-work` to retain the copied source and intermediate media too. Both directories are gitignored.

The `transcribe` command works directly on a manual-editor project and processes every asset in its media bin. It caches timestamped `.srt`, plain `.txt`, and normalized `.json` files under `work/<project>/transcripts/`; pass `--asset` to limit the batch or `--force` to refresh it. Whisper receives a short vocabulary prompt that can be overridden with `--prompt` for names and specialist terms.

### Agent inspection and validation

Installing the package exposes both `video-pipeline` and the shorter `lve` alias. The agent-facing commands emit JSON and keep derived artifacts under `work/<project>/cache/`:

- `context <project>` summarizes timeline, template, assets, and preflight state.
- `assets sync|list|relink|make-portable` maintains `assets.json`. Timeline clips retain readable filename IDs while each file receives a stable `sha256:` content ID, source/storage status, probe record, and content-keyed cache.
- `media probe|grab|filmstrip|waveform` inspects local footage without uploading it.
- `capture <project>` renders an aspect-preserving draft through the canonical FFmpeg graph and creates a timestamped contact sheet. Final export continues to render at the selected delivery resolution.
- `check <project>` validates media availability and bounds, visual schedule gaps, overlaps, protected layers, required story slots, question/payoff order, pacing, resolution, FPS, and runtime. Add `--deep` for rendered black-frame, silence, LUFS, and true-peak analysis.
- `render <project>` runs structural preflight before invoking the canonical FFmpeg renderer.

`check` reports all creative/format issues. Missing or out-of-range source media, invalid speed/ranges, and a timeline with no visuals block rendering; non-destructive format warnings can be reviewed and consciously overridden in the UI.

## Manual editor UI

Run `python run.py ui` after producing an export. The command opens a local multitrack editing workspace at `http://127.0.0.1:8765`. It also discovers older videos already stored under `out/`, even when they do not have a pipeline manifest.

- Click the ruler to move the playhead; press Space to play or pause.
- Press `S` or use the scissors button to split a clip at the playhead.
- Drag either edge of a selected clip to trim it, or use the compact manual controls in **Operator**.
- Add MP4, MOV, M4V, WebM, M4A, MP3, WAV, AAC, or FLAC files to the media bin, then click `+` or drag them onto a track. From an empty library, **Add media** creates and names the first project. Drag timeline clips between tracks or to a new time to rearrange the edit.
- Add and delete video/audio tracks, mute or lock tracks, and unlink a video's audio into an independently editable audio clip.
- Add titles and captions at the playhead. Selected clips expose transitions, speed, timeline position, crop, scale, X/Y position, brightness, contrast, saturation, temperature, volume, audio fades, and keyframes.
- Press Delete to remove the selected clip and use Operator to change its properties.
- Hover a project-media row and use its trash button to move that project's generated exports to the macOS Trash; source footage and `work/` state are preserved.
- **Operator** combines manual clip controls with a real Codex CLI terminal beside the edit. It uses the existing ChatGPT/Codex subscription login, so Terminal.app does not need to remain open. Timeline requests are sent back through a structured action bridge and appear in the UI immediately with undo history.
- Finder files or folders dropped on **Media bin** are recursively imported into the project. A file or folder dropped on Operator resolves to its original path from Desktop, Downloads, Documents, Movies, or the project root and inserts a shell-escaped path exactly like Terminal.app; unresolved standalone files are copied into `operator-uploads/` first.
- Use **Review edit** to send the visible timeline, playhead, format, and clip timestamps to Codex for feedback.
- Press Command-S/Control-S to persist the edit under `work/<slug>/edits/`.
- Choose Source, YouTube 1080p, Vertical 1080×1920, or Draft 720p, then use **Render edit**. FFmpeg renders in the background with visible progress and creates a new timestamped MP4 without replacing earlier exports.

Saving creates one reopenable `work/<slug>/<slug>.llmve` project file containing the active media version and every saved timeline for that project. The editor automatically reopens the last active media and timeline from this file; the older per-media JSON files remain as compatibility copies. Manual changes are non-destructive and always begin from the generated edit state. Use `python run.py ui --no-open` to serve without opening a browser, or change the local port with `--port`.

`work/`, `out/`, `models/`, build products, and RecCli session memory are intentionally gitignored. A normal Git clone contains code and documentation, not footage or private editing history. See [Project data and portability](docs/PROJECT_DATA.md) before moving an active edit to another machine.

### Import a Premiere Pro project

Modern `.prproj` files can be imported directly into the manual editor. The importer reads Premiere's sequence, video, audio, caption, clip timing, speed, title, and transition graph. Nested sequences are retained in the import report and flattened into source clips on the editable timeline. Source media is linked in place by default so the import does not duplicate large camera files; pass `--copy-media` for a self-contained project.

```bash
python run.py import-premiere "/path/to/edit.prproj" \
  --preview "/path/to/rendered-edit.mp4" \
  --project-id my-series-template \
  --name "My Series Template"
```

The resulting `.llmve` project opens normally in the app. Its exact source-project hash and full original sequence tree are stored under `premiere_import` and in `work/<project>/premiere-import-report.json`, so later manual saves preserve the provenance record.

### Encoded episode templates

An `.llmve` timeline can optionally contain an `episode_template` contract with ordered story slots, required beats, protected identity tracks, a question slot, a payoff slot, and original-reference markers. Encoded projects display their mechanism in the media panel. Each beat is marked as reference, replaced, or missing.

Select a replaceable clip and choose **Replace · keep timing** to assign another asset while retaining the clip's track, timeline duration, transforms, effects, and story-slot identity. Matching production audio is replaced with the same source when available. Protected title and music tracks cannot be moved, trimmed, deleted, or changed by Operator actions.

Use **Duplicate episode** to create a lightweight linked copy, advance its episode number, and preserve the template contract. Rendering warns when a required beat—especially the encoded question or payoff—is missing.

The embedded Codex session is approval-aware by default. The server uses a random per-launch token for API/media requests, validates local Host and Origin values, and passes the token to Operator for structured timeline actions. The badge in the UI shows the active trust level. `python run.py ui --operator-full-access` explicitly enables `codex --yolo`; in that mode Codex can edit files and run commands as the current macOS user without approval prompts, so use it only with projects and prompts you trust. If needed, authenticate once from a shell with `codex login`; ChatGPT subscription authentication is reused automatically.

### Optional Diffusion Studio motion sidecar

Diffusion Studio is available as an isolated motion-graphics tool, not as LLM Editor's renderer or document model:

```bash
lve diffusion sidecar my-editor-project
# Review work/my-editor-project/diffusion-sidecar/index.tsx, then open it with dapi.
lve diffusion import-render my-editor-project /path/to/rendered-motion.mp4
```

The generated sidecar is static, records the reviewed Diffusion Studio commit, and uses absolute local media references. Diffusion project JavaScript executes unsandboxed in the Diffusion desktop process, so review the folder before opening it. Only the flattened render is imported back into LLM Editor, where it becomes an ordinary content-addressed asset. `.llmve`, Premiere provenance, AVFoundation preview, and FFmpeg export remain authoritative.

On macOS, the optional AppleScript launcher source lives in `macos/LLM Video Editor.applescript`. The Desktop app starts the local server in the background, reuses it when it is already running, and writes startup diagnostics to `~/Library/Logs/LLM Video Editor/editor.log`.

### Native macOS editor

The definitive macOS client lives under `macos/NativeApp/`. It keeps the established editor and Operator interface in WebKit but replaces browser video elements with one native AVFoundation composition and `AVPlayerLayer`. Cuts, timing, speed, track order, transforms, rotation, crop, opacity transitions, linked or independent audio, volume fades, titles, and captions update from the `.llmve` timeline without restarting the Python server or Operator session.

Build the signed local app with:

```bash
./macos/build_native_app.sh
open "build/macos/LLM Video Editor.app"
```

The app reuses port 8765 when the editor is already running and starts `run.py ui --no-open` only when necessary. Native playback diagnostics are written to `~/Library/Logs/LLM Video Editor/native.log`.

## Configuration

`config.yml` is the source of truth for input matching, silence thresholds, transcription, output sizes, caption style, watermark, bitrates, and loudness. `--model`, `--threshold`, `--preset`, and target/caption/brand flags override a run.

The `storyline` command is a clearly marked boundary for the optional FireRed-OpenStoryline integration. It is not installed or silently invoked by the authentic-footage pipeline.

## RecCli project memory

If you use RecCli, register your checkout as `llm-video-editor`. RecCli can store resumable session memory in `llm-video-editor.devproject` and `devsession/`. Those files are deliberately ignored because they may contain machine paths, project history, and private editing context.

At the beginning of an agent session, load RecCli context for this project. At the end of meaningful work, save a concise session note with decisions, solved problems, changed files, and remaining work. Run RecCli's project doctor after moving or rewriting memory, and rebuild its index if search results are stale. Full conventions are in [Development and RecCli](docs/DEVELOPMENT.md).

## Tests

```bash
pytest -q
python -m compileall -q src run.py
```

The Python suite does not require FFmpeg, Auto-Editor, or Whisper. Before releasing a renderer or native-playback change, also run a real project `check`, `capture`, and representative render; build the Swift app when its sources change.

## License and importer provenance

The project was developed locally before its first private GitHub upload on August 29, 2026.
This publication snapshot has personal metadata and project-specific examples removed;
the GitHub upload date is not the start of development.

Original project code is licensed under [MIT](LICENSE). Vendored xterm.js and addon-fit retain
their own MIT copyright notices in `src/video_pipeline/ui/vendor/`.

The [Premiere parser](src/video_pipeline/premiere.py) was written from the author's own `.prproj`
files using Python's standard library and observations of user-owned XML. No Adobe application
code, proprietary documentation or assets were consulted, and no other `.prproj` parser was used
as a reference. Real project files and media are not included.
