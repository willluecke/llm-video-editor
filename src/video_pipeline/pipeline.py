from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .config import load_config
from .process import CommandRunner

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}


def slugify(value: str) -> str:
    value = re.sub(r"(?i)(?:[_-]raw)$", "", value)
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "video"


def _ffmpeg_escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


@dataclass
class PipelineOptions:
    targets: tuple[str, ...] = ("horizontal", "vertical")
    captions: bool = True
    brand: bool = True
    model: str | None = None
    threshold: str | None = None
    preset: str | None = None
    keep_work: bool = False


class VideoPipeline:
    def __init__(self, root: Path, config_path: Path, runner: CommandRunner | None = None):
        self.root = root.resolve()
        self.config_path = config_path.resolve()
        self.runner = runner or CommandRunner()

    def cut(self, source: Path, options: PipelineOptions) -> dict[str, Path]:
        source = source.expanduser().resolve()
        if not source.is_file() and not self.runner.dry_run:
            raise FileNotFoundError(f"Input clip not found: {source}")
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError(f"Unsupported input format: {source.suffix}")

        slug = slugify(source.stem)
        work = self.root / "work" / slug
        work.mkdir(parents=True, exist_ok=True)
        sidecar_source = source.with_suffix(".yml")
        sidecar = work / "clip.yml"
        if sidecar_source.is_file():
            shutil.copy2(sidecar_source, sidecar)
        config = load_config(self.config_path, sidecar if sidecar.is_file() else None)
        src = work / "00_source.mp4"
        if not self.runner.dry_run:
            shutil.copy2(source, src)
        tight = self._tighten(src, work, config, options)
        srt, transcript = self._transcribe(tight, work, config, options)

        outputs: dict[str, Path] = {}
        for target in options.targets:
            if target not in config["targets"]:
                raise ValueError(f"Unknown target: {target}")
            outputs[target] = self._render(target, tight, srt, slug, work, config, options)
        out_dir = self.root / config["export"]["out_dir"] / slug
        exported_transcript = out_dir / "20_transcript.txt"
        if not self.runner.dry_run:
            shutil.copy2(transcript, exported_transcript)
        outputs["transcript"] = exported_transcript
        self._manifest(source, config, options, outputs, work)
        if not options.keep_work and not self.runner.dry_run:
            for artifact in (src, tight, work / "15_audio.wav", srt, transcript):
                artifact.unlink(missing_ok=True)
        return outputs

    def _tighten(self, source: Path, work: Path, config: dict[str, Any], options: PipelineOptions) -> Path:
        output = work / "10_tight.mp4"
        settings = config["tighten"]
        command = [self.runner.require("auto-editor"), str(source), "--edit", f"audio:threshold={options.threshold or settings['threshold']}", "--margin", str(settings["margin"]), "--no-open"]
        if settings.get("silent_speed") is not None:
            command += ["--silent-speed", str(settings["silent_speed"])]
        command += ["-o", str(output)]
        self.runner.run(command)
        return output

    def _transcribe(self, video: Path, work: Path, config: dict[str, Any], options: PipelineOptions) -> tuple[Path, Path]:
        settings = config["transcribe"]
        engine = settings.get("engine", "whisper-cli")
        model = options.model or settings["model"]
        srt, txt = work / "20_captions.srt", work / "20_transcript.txt"
        audio = work / "15_audio.wav"
        self.runner.run([
            self.runner.require("ffmpeg"), "-y", "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
        ])
        if engine == "whisper-cli":
            model_path = settings.get("model_path")
            if not model_path:
                raise ValueError("transcribe.model_path must point to a whisper.cpp GGML model when engine is whisper-cli")
            model_file = Path(model_path).expanduser()
            if not model_file.is_absolute():
                model_file = self.root / model_file
            if not model_file.is_file() and not self.runner.dry_run:
                raise FileNotFoundError(f"Whisper model not found: {model_file}")
            prefix = work / "20"
            self.runner.run([self.runner.require("whisper-cli"), "-m", str(model_file), "-f", str(audio), "-osrt", "-otxt", "-of", str(prefix)])
            if not self.runner.dry_run:
                prefix.with_suffix(".srt").replace(srt)
                prefix.with_suffix(".txt").replace(txt)
        elif engine == "whisper":
            self.runner.run([self.runner.require("whisper"), str(audio), "--model", model, "--output_dir", str(work), "--output_format", "all"])
            generated = work / f"{audio.stem}.srt"
            generated_txt = work / f"{audio.stem}.txt"
            if not self.runner.dry_run:
                generated.replace(srt)
                generated_txt.replace(txt)
        else:
            raise ValueError(f"Unsupported transcription engine: {engine}")
        return srt, txt

    def _render(self, target: str, video: Path, srt: Path, slug: str, work: Path, config: dict[str, Any], options: PipelineOptions) -> Path:
        ffmpeg = self.runner.require("ffmpeg")
        target_config = config["targets"][target]
        width, height = target_config["w"], target_config["h"]
        filters: list[str] = []
        if target == "vertical":
            filters.append("crop=ih*9/16:ih:(iw-ow)/2:0")
        filters += [f"scale={width}:{height}:force_original_aspect_ratio=decrease", f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black", "setsar=1"]
        burn = options.captions and config["transcribe"].get("burn_captions", True) and target_config.get("captions", "burn") == "burn"
        if burn:
            style = f"FontSize={target_config['caption_size']},PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=3,Alignment=2,MarginV=80"
            filters.append(f"subtitles='{_ffmpeg_escape(srt)}':force_style='{style}'")

        watermark = self.root / config["brand"].get("watermark", "")
        watermark_text = str(config["brand"].get("watermark_text", "")).strip()
        use_watermark = options.brand and watermark.is_file()
        use_text_watermark = options.brand and not use_watermark and bool(watermark_text)
        if use_text_watermark:
            safe_text = watermark_text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
            filters.append(
                f"drawtext=text='{safe_text}':fontcolor=white@0.65:fontsize=28:"
                "box=1:boxcolor=black@0.25:boxborderw=8:x=w-text_w-40:y=40"
            )
        inputs = ["-i", str(video)]
        if use_watermark:
            inputs += ["-i", str(watermark)]
            graph = f"[0:v]{','.join(filters)}[base];[1:v]format=rgba,colorchannelmixer=aa=0.72[wm];[base][wm]overlay=W-w-40:40[v]"
            filter_args = ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?"]
        else:
            if options.brand and not use_text_watermark:
                print(f"Warning: watermark missing and watermark_text is empty; skipping brand overlay: {watermark}")
            filter_args = ["-vf", ",".join(filters), "-map", "0:v", "-map", "0:a?"]

        export = config["export"]
        out_dir = self.root / export["out_dir"] / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        preset = options.preset or ("youtube_1080p" if target == "horizontal" else "shorts_1080x1920")
        output = out_dir / f"{date.today().isoformat()}_{slug}_{preset}.mp4"
        loudnorm = f"loudnorm=I={export['loudness_lufs']}:TP={export.get('true_peak', -1.5)}:LRA={export.get('loudness_range', 11)}"
        command = [ffmpeg, "-y", *inputs, *filter_args, "-af", loudnorm, "-c:v", "libx264", "-preset", "medium", "-b:v", str(export["video_bitrate"]), "-c:a", "aac", "-ar", "48000", "-b:a", str(export["audio_bitrate"]), "-movflags", "+faststart", str(output)]
        self.runner.run(command)
        if options.captions and target_config.get("captions") == "soft" and not self.runner.dry_run:
            shutil.copy2(srt, output.with_suffix(".srt"))
        if not self.runner.dry_run:
            self._validate(output, width, height, float(export["loudness_lufs"]))
        return output

    def _validate(self, output: Path, width: int, height: int, target_lufs: float) -> None:
        ffprobe = self.runner.require("ffprobe")
        raw = self.runner.capture([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,duration", "-of", "json", str(output)])
        data = json.loads(raw)["streams"][0]
        if (int(data["width"]), int(data["height"])) != (width, height):
            raise RuntimeError(f"Export has wrong resolution: {data['width']}x{data['height']}")
        if float(data.get("duration") or 0) <= 0:
            raise RuntimeError("Export has zero duration")
        ffmpeg = self.runner.require("ffmpeg")
        analysis = self.runner.capture_combined([
            ffmpeg, "-hide_banner", "-nostats", "-i", str(output),
            "-filter_complex", "ebur128=peak=true", "-f", "null", "-",
        ])
        matches = re.findall(r"I:\s*(-?\d+(?:\.\d+)?) LUFS", analysis)
        if matches:
            measured = float(matches[-1])
            if abs(measured - target_lufs) > 1.0:
                print(f"Warning: measured loudness is {measured:.1f} LUFS (target {target_lufs:.1f} LUFS)")

    def _manifest(self, source: Path, config: dict[str, Any], options: PipelineOptions, outputs: dict[str, Path], work: Path) -> None:
        manifest = {"source": str(source), "targets": list(options.targets), "captions": options.captions, "brand": options.brand, "outputs": {k: str(v) for k, v in outputs.items()}}
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def batch(self, options: PipelineOptions) -> list[dict[str, Path]]:
        config = load_config(self.config_path)
        input_dir = Path(config["input_dir"])
        seen = {p.parent.name for p in (self.root / "work").glob("*/manifest.json")}
        sources = sorted({p for pattern in config["match"] for p in input_dir.glob(pattern) if slugify(p.stem) not in seen})
        return [self.cut(source, options) for source in sources]

    def watch(self, options: PipelineOptions, interval: float = 2.0) -> None:
        print("Watching for new clips. Press Ctrl-C to stop.")
        while True:
            self.batch(options)
            time.sleep(interval)
