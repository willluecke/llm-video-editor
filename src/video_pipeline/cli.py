from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

# GUI launches (Spotlight/Finder) inherit a minimal PATH without Homebrew or
# pipx locations, which breaks shutil.which for ffmpeg, whisper-cli, and codex.
_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin"))


def _ensure_tool_path() -> None:
    current = os.environ.get("PATH", "").split(os.pathsep)
    missing = [d for d in _TOOL_DIRS if d not in current and Path(d).is_dir()]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing + current)


def _options(args: argparse.Namespace):
    from .pipeline import PipelineOptions

    targets = ("horizontal", "vertical") if args.targets == "both" else (args.targets,)
    return PipelineOptions(targets=targets, captions=not args.no_captions, brand=not args.no_brand, model=args.model, threshold=args.threshold, preset=args.preset, keep_work=args.keep_work)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic LLM Video Editor pipeline")
    parser.add_argument("--config", type=Path, default=Path(__file__).parents[2] / "config.yml")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing tools")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("cut", "batch", "watch"):
        command = sub.add_parser(name)
        if name == "cut":
            command.add_argument("clip", type=Path)
        command.add_argument("--targets", choices=("horizontal", "vertical", "both"), default="both")
        command.add_argument("--no-captions", action="store_true")
        command.add_argument("--no-brand", action="store_true")
        command.add_argument("--preset")
        command.add_argument("--model")
        command.add_argument("--threshold")
        command.add_argument("--keep-work", action="store_true")
    story = sub.add_parser("storyline", help="Reserved constructed-content branch")
    story.add_argument("--theme", required=True)
    story.add_argument("--media", type=Path, required=True)
    editor = sub.add_parser("ui", help="Open the local manual timeline editor")
    editor.add_argument("--host", default="127.0.0.1")
    editor.add_argument("--port", type=int, default=8765)
    editor.add_argument("--no-open", action="store_true", help="Do not open a browser automatically")
    editor.add_argument(
        "--operator-full-access",
        action="store_true",
        help="Run the embedded Codex terminal with --yolo (approval-aware mode is the default)",
    )
    transcribe = sub.add_parser("transcribe", help="Create timestamped transcripts for editor project media")
    transcribe.add_argument("project", help="Editor project id")
    transcribe.add_argument("--asset", action="append", help="Transcribe only this asset id (repeatable)")
    transcribe.add_argument("--model-path", type=Path, help="Override transcribe.model_path")
    transcribe.add_argument("--prompt", help="Vocabulary/context prompt for Whisper")
    transcribe.add_argument("--force", action="store_true", help="Replace cached transcripts")
    premiere = sub.add_parser("import-premiere", help="Import a Premiere Pro timeline as an editable project")
    premiere.add_argument("project_file", type=Path, help="Source .prproj file")
    premiere.add_argument("--project-id", help="Editor project id (defaults to a slug of --name)")
    premiere.add_argument("--name", help="Display name for the imported project")
    premiere.add_argument("--sequence", help="Premiere sequence name (defaults to Sequence 01 or the primary sequence)")
    premiere.add_argument("--preview", type=Path, help="Rendered MP4 used as the project preview")
    premiere.add_argument("--copy-media", action="store_true", help="Copy source media instead of linking it in place")
    context = sub.add_parser("context", help="Describe an editor project for a person or agent")
    context.add_argument("project", help="Editor project id")
    context.add_argument("--media", help="Select a project media version")
    assets = sub.add_parser("assets", help="Manage the content-addressed project asset library")
    asset_sub = assets.add_subparsers(dest="asset_command", required=True)
    for name in ("sync", "list"):
        asset_command = asset_sub.add_parser(name)
        asset_command.add_argument("project")
    relink = asset_sub.add_parser("relink")
    relink.add_argument("project")
    relink.add_argument("asset")
    relink.add_argument("source", type=Path)
    relink.add_argument("--copy", action="store_true", dest="copy_media")
    portable = asset_sub.add_parser("make-portable")
    portable.add_argument("project")
    portable.add_argument("--asset", action="append", help="Limit to this asset id (repeatable)")
    media = sub.add_parser("media", help="Inspect footage with deterministic local tools")
    media_sub = media.add_subparsers(dest="media_command", required=True)
    media_probe = media_sub.add_parser("probe")
    media_probe.add_argument("target")
    media_probe.add_argument("--project")
    grab = media_sub.add_parser("grab")
    grab.add_argument("target")
    grab.add_argument("--project")
    grab.add_argument("--time", type=float, action="append", required=True)
    grab.add_argument("--output-dir", type=Path)
    filmstrip = media_sub.add_parser("filmstrip")
    filmstrip.add_argument("target")
    filmstrip.add_argument("--project")
    filmstrip.add_argument("--frames", type=int, default=12)
    filmstrip.add_argument("--output", type=Path)
    waveform = media_sub.add_parser("waveform")
    waveform.add_argument("target")
    waveform.add_argument("--project")
    waveform.add_argument("--output", type=Path)
    capture = sub.add_parser("capture", help="Render canonical project frames to a contact sheet")
    capture.add_argument("project")
    capture.add_argument("--media")
    capture.add_argument("--time", type=float, action="append")
    capture.add_argument("--output", type=Path)
    capture.add_argument("--refresh", action="store_true")
    check = sub.add_parser("check", help="Validate project structure and optionally rendered output")
    check.add_argument("project")
    check.add_argument("--media")
    check.add_argument("--deep", action="store_true")
    check.add_argument("--refresh-capture", action="store_true")
    render = sub.add_parser("render", help="Preflight and render the saved editor timeline")
    render.add_argument("project")
    render.add_argument("--media")
    render.add_argument("--preset", choices=("source", "youtube", "vertical", "draft"), default="source")
    render.add_argument("--deep-check", action="store_true")
    diffusion = sub.add_parser("diffusion", help="Use Diffusion Studio as an isolated motion-graphics sidecar")
    diffusion_sub = diffusion.add_subparsers(dest="diffusion_command", required=True)
    sidecar = diffusion_sub.add_parser("sidecar")
    sidecar.add_argument("project")
    sidecar.add_argument("--output", type=Path)
    sidecar.add_argument("--force", action="store_true")
    diffusion_import = diffusion_sub.add_parser("import-render")
    diffusion_import.add_argument("project")
    diffusion_import.add_argument("render", type=Path)
    diffusion_import.add_argument("--name")
    return parser


def _json(data: object) -> None:
    print(json.dumps(data, indent=2))


def _project_context(root: Path, project_id: str, media_id: str | None = None) -> dict[str, object]:
    from .assets import load_asset_manifest
    from .checks import check_state
    from .editor import load_editor_state

    state = load_editor_state(root, project_id, media_id)
    manifest = load_asset_manifest(root, project_id)
    template = state.get("episode_template")
    preflight = check_state(root, project_id, state, manifest=manifest)
    return {
        "root": str(root),
        "project": state.get("project"),
        "project_file": state.get("project_file"),
        "media": state.get("media"),
        "resolution": state.get("resolution"),
        "fps": state.get("fps"),
        "duration": preflight["stats"]["duration"],
        "tracks": state.get("tracks"),
        "template": template,
        "template_status": state.get("template_status"),
        "asset_summary": {
            "total": len(manifest["assets"]),
            "online": sum(item.get("status") == "online" for item in manifest["assets"]),
            "missing": sum(item.get("status") == "missing" for item in manifest["assets"]),
            "linked": sum(item.get("storage") == "linked" for item in manifest["assets"]),
        },
        "preflight": preflight,
    }


def main(argv: list[str] | None = None) -> int:
    _ensure_tool_path()
    args = build_parser().parse_args(argv)
    root = args.config.expanduser().resolve().parent
    if args.command == "ui":
        from .editor import serve_editor

        serve_editor(
            root,
            args.host,
            args.port,
            not args.no_open,
            operator_full_access=args.operator_full_access,
        )
        return 0
    if args.command == "context":
        try:
            _json(_project_context(root, args.project, args.media))
        except (FileNotFoundError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"Error: {error}")
            return 1
        return 0
    if args.command == "assets":
        from .assets import load_asset_manifest, make_assets_portable, relink_asset, sync_asset_manifest
        from .editor import probe

        try:
            if args.asset_command == "sync":
                result = sync_asset_manifest(root, args.project, probe_media=probe)
            elif args.asset_command == "list":
                result = load_asset_manifest(root, args.project)
            elif args.asset_command == "relink":
                result = relink_asset(root, args.project, args.asset, args.source, copy_media=args.copy_media, probe_media=probe)
            else:
                result = make_assets_portable(root, args.project, args.asset, probe_media=probe)
            _json(result)
        except (FileNotFoundError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f"Error: {error}")
            return 1
        return 0
    if args.command == "media":
        from .inspection import filmstrip_media, grab_frames, probe_media, resolve_media_target, waveform_media

        try:
            target = resolve_media_target(root, args.target, args.project)
            cache = root / "work" / (args.project or "inspection") / "cache" / "media"
            if args.media_command == "probe":
                result = probe_media(target)
            elif args.media_command == "grab":
                output_dir = args.output_dir or cache / target.stem / "frames"
                result = {"target": str(target), "frames": grab_frames(target, args.time, output_dir)}
            elif args.media_command == "filmstrip":
                output = args.output or cache / target.stem / "filmstrip.png"
                result = filmstrip_media(target, output, frames=args.frames)
            else:
                output = args.output or cache / target.stem / "waveform.png"
                result = waveform_media(target, output)
            _json(result)
        except (FileNotFoundError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f"Error: {error}")
            return 1
        return 0
    if args.command == "capture":
        from .inspection import capture_project

        try:
            _json(capture_project(root, args.project, media_id=args.media, times=args.time, output=args.output, refresh=args.refresh))
        except (FileNotFoundError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f"Error: {error}")
            return 1
        return 0
    if args.command == "check":
        from .checks import check_project

        try:
            report = check_project(root, args.project, media_id=args.media, deep=args.deep, refresh_capture=args.refresh_capture)
            _json(report)
            return 0 if report["valid"] else 1
        except (FileNotFoundError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f"Error: {error}")
            return 1
    if args.command == "render":
        from .checks import check_project
        from .editor import load_editor_state, render_editor_state

        try:
            if args.deep_check:
                report = check_project(root, args.project, media_id=args.media, deep=True)
                if not report["valid"]:
                    _json(report)
                    return 1
            state = load_editor_state(root, args.project, args.media)
            output = render_editor_state(root, args.project, str(state["media"]), state, args.preset)
            _json({"ok": True, "output": str(output), "preset": args.preset})
        except (FileNotFoundError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f"Error: {error}")
            return 1
        return 0
    if args.command == "diffusion":
        from .diffusion_bridge import generate_diffusion_sidecar, import_diffusion_render

        try:
            if args.diffusion_command == "sidecar":
                result = generate_diffusion_sidecar(root, args.project, output=args.output, force=args.force)
            else:
                result = import_diffusion_render(root, args.project, args.render, name=args.name)
            _json(result)
        except (FileExistsError, FileNotFoundError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f"Error: {error}")
            return 1
        return 0
    if args.command == "storyline":
        print("The FireRed-OpenStoryline constructed-content branch is intentionally opt-in and is not installed by the core pipeline.")
        return 2
    if args.command == "import-premiere":
        from .premiere import import_premiere_project

        try:
            result = import_premiere_project(
                root,
                args.project_file,
                project_id=args.project_id,
                name=args.name,
                sequence_name=args.sequence,
                preview=args.preview,
                copy_media=args.copy_media,
            )
        except (FileExistsError, FileNotFoundError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"Error: {error}")
            return 1
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "transcribe":
        from .config import load_config
        from .process import CommandRunner
        from .transcript import DEFAULT_PROMPT, ProjectTranscriber

        settings = load_config(args.config)["transcribe"]
        model = args.model_path or Path(settings.get("model_path", ""))
        if not model.is_absolute():
            model = root / model
        try:
            results, index = ProjectTranscriber(root, model, CommandRunner(dry_run=args.dry_run)).transcribe(
                args.project,
                args.asset,
                force=args.force,
                prompt=args.prompt or DEFAULT_PROMPT,
            )
            for result in results:
                status = "cached" if result.cached else "created"
                print(f"{status}: {result.asset_id} ({result.segments} timed segments)")
            print(f"index: {index}")
        except (FileNotFoundError, ValueError, RuntimeError) as error:
            print(f"Error: {error}")
            return 1
        return 0
    from .pipeline import VideoPipeline
    from .process import CommandRunner

    pipeline = VideoPipeline(root, args.config, CommandRunner(dry_run=args.dry_run))
    options = _options(args)
    try:
        if args.command == "cut":
            outputs = pipeline.cut(args.clip, options)
            for name, path in outputs.items():
                print(f"{name}: {path}")
        elif args.command == "batch":
            print(f"Processed {len(pipeline.batch(options))} clip(s)")
        else:
            pipeline.watch(options)
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"Error: {error}")
        return 1
    return 0
