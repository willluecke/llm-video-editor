from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any


PREMIERE_TICKS_PER_SECOND = 254_016_000_000


def _seconds(value: str | None) -> float:
    if not value:
        return 0.0
    return int(value) / PREMIERE_TICKS_PER_SECOND


def _decode_formatted_text(value: str | None) -> str:
    """Recover Premiere caption text from its small binary text envelope."""
    if not value:
        return ""
    raw = base64.b64decode("".join(value.split()))
    candidates: list[tuple[int, int, str]] = []
    for offset in range(max(0, len(raw) - 4)):
        declared = int.from_bytes(raw[offset : offset + 4], "little")
        if not 1 <= declared <= 2000:
            continue
        payload = raw[offset + 4 :].split(b"\0", 1)[0]
        if not payload:
            continue
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not any(character.isalpha() for character in decoded):
            continue
        if any(ord(character) < 32 and character not in "\r\n\t" for character in decoded):
            continue
        length_delta = min(abs(len(decoded) - declared), abs(len(payload) - declared))
        if length_delta <= 8:
            candidates.append((len(decoded), offset, decoded))
    if not candidates:
        return ""
    # Premiere stores font/style strings before the actual caption in the same
    # envelope. The authored copy is the final valid length-prefixed string.
    return max(candidates, key=lambda item: (item[1], item[0]))[2].replace("\r", "\n").strip()


@dataclass(frozen=True)
class PremiereTransition:
    name: str
    duration: float


@dataclass(frozen=True)
class PremiereItem:
    object_id: str
    kind: str
    track_index: int
    timeline_start: float
    timeline_end: float
    label: str
    source_start: float = 0.0
    source_end: float = 0.0
    speed: float = 1.0
    media_path: str | None = None
    nested_sequence: str | None = None
    text: str | None = None
    audio_category: str | None = None
    effects: tuple[str, ...] = ()
    transition_in: PremiereTransition | None = None
    transition_out: PremiereTransition | None = None
    scale: float | None = None
    position_x: float | None = None
    position_y: float | None = None
    rotation: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.timeline_end - self.timeline_start)


@dataclass(frozen=True)
class PremiereTrack:
    kind: str
    index: int
    muted: bool
    items: tuple[PremiereItem, ...]


@dataclass(frozen=True)
class PremiereSequence:
    name: str
    width: int
    height: int
    work_out: float
    tracks: tuple[PremiereTrack, ...]

    @property
    def duration(self) -> float:
        item_end = max((item.timeline_end for track in self.tracks for item in track.items), default=0.0)
        return item_end or self.work_out


class PremiereProject:
    """Read the gzip-compressed Premiere project graph used by modern .prproj files."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"Premiere project not found: {self.path}")
        with self.path.open("rb") as source:
            signature = source.read(2)
        opener = gzip.open if signature == b"\x1f\x8b" else open
        with opener(self.path, "rb") as source:
            self.root = ET.parse(source).getroot()
        self.by_id = {element.attrib["ObjectID"]: element for element in self.root if "ObjectID" in element.attrib}
        self.by_uid = {element.attrib["ObjectUID"]: element for element in self.root if "ObjectUID" in element.attrib}
        self._sequence_elements = {
            element.findtext("Name") or f"Sequence {index + 1}": element
            for index, element in enumerate(self.root.findall("Sequence"))
        }
        self._media_paths = self._read_media_paths()
        self.sequences = {
            name: self._read_sequence(name, element)
            for name, element in self._sequence_elements.items()
        }

    def _read_media_paths(self) -> dict[str, str]:
        paths: dict[str, str] = {}
        for media in self.root.findall("Media"):
            title = media.findtext("Title")
            raw_path = media.findtext("ActualMediaFilePath") or media.findtext("FilePath")
            if not title or not raw_path or not raw_path.startswith("/"):
                continue
            candidate = Path(raw_path)
            if not candidate.exists() and raw_path.startswith("/Volumes/Macintosh HD/"):
                candidate = Path(raw_path.removeprefix("/Volumes/Macintosh HD"))
            paths.setdefault(title, str(candidate))
        return paths

    def _read_sequence(self, name: str, element: ET.Element) -> PremiereSequence:
        properties = element.find("./Node/Properties")
        width = int(properties.findtext("MZ.Sequence.PreviewFrameSizeWidth") or 0) if properties is not None else 0
        height = int(properties.findtext("MZ.Sequence.PreviewFrameSizeHeight") or 0) if properties is not None else 0
        work_out = _seconds(properties.findtext("MZ.WorkOutPoint")) if properties is not None else 0.0
        tracks: list[PremiereTrack] = []
        for group_pointer in element.findall("./TrackGroups/TrackGroup/Second"):
            group = self.by_id.get(group_pointer.attrib.get("ObjectRef", ""))
            if group is None:
                continue
            if group.tag.startswith("Video"):
                kind = "video"
            elif group.tag.startswith("Audio"):
                kind = "audio"
            else:
                kind = "caption"
            for fallback_index, pointer in enumerate(group.findall("./TrackGroup/Tracks/Track")):
                track = self.by_uid.get(pointer.attrib.get("ObjectURef", ""))
                if track is None:
                    continue
                index = int(track.findtext(".//Track/Index") or fallback_index)
                muted = (track.findtext(".//Track/IsMuted") or "false").lower() == "true"
                items = tuple(
                    self._read_item(self.by_id[item_pointer.attrib["ObjectRef"]], kind, index)
                    for item_pointer in track.findall(".//ClipItems/TrackItems/TrackItem")
                    if item_pointer.attrib.get("ObjectRef") in self.by_id
                )
                tracks.append(PremiereTrack(kind=kind, index=index, muted=muted, items=items))
        return PremiereSequence(name=name, width=width, height=height, work_out=work_out, tracks=tuple(tracks))

    def _read_item(self, element: ET.Element, track_kind: str, track_index: int) -> PremiereItem:
        timeline = element.find(".//TrackItem")
        start = _seconds(timeline.findtext("Start")) if timeline is not None else 0.0
        end = _seconds(timeline.findtext("End")) if timeline is not None else start
        label = element.tag
        source_start = 0.0
        source_end = max(0.0, end - start)
        speed = 1.0
        nested_sequence = None
        text = None
        media_path = None

        subclip_pointer = element.find(".//SubClip")
        if subclip_pointer is not None:
            subclip = self.by_id.get(subclip_pointer.attrib.get("ObjectRef", ""))
            if subclip is not None:
                label = subclip.findtext("Name") or label
                nested_sequence = label if label in self._sequence_elements else None
                media_path = self._media_paths.get(label)
                clip_pointer = subclip.find("Clip")
                clip = self.by_id.get(clip_pointer.attrib.get("ObjectRef", "")) if clip_pointer is not None else None
                if clip is not None:
                    source_start = _seconds(clip.findtext(".//InPoint"))
                    source_end = _seconds(clip.findtext(".//OutPoint"))
                    speed = max(0.001, float(clip.findtext(".//PlaybackSpeed") or 1.0))

        effects, effect_text, motion = self._read_effects(element)
        if effect_text:
            text = effect_text
        kind = track_kind
        if label == "Graphic" and text:
            kind = "title"
        if track_kind == "caption":
            kind = "caption"
            block_pointer = element.find("./BlockVector/BlockVectorItem")
            block = self.by_id.get(block_pointer.attrib.get("ObjectRef", "")) if block_pointer is not None else None
            text = _decode_formatted_text(block.findtext("FormattedTextData")) if block is not None else ""
            label = text or "Caption"

        properties = timeline.find("./Node/Properties") if timeline is not None else None
        audio_category = properties.findtext("ESP.Tag") if properties is not None else None
        transition_in = self._read_transition(element.find(".//HeadTransition"))
        transition_out = self._read_transition(element.find(".//TailTransition"))
        return PremiereItem(
            object_id=element.attrib.get("ObjectID", ""),
            kind=kind,
            track_index=track_index,
            timeline_start=start,
            timeline_end=end,
            label=label,
            source_start=source_start,
            source_end=source_end,
            speed=speed,
            media_path=media_path,
            nested_sequence=nested_sequence,
            text=text,
            audio_category=audio_category,
            effects=tuple(effects),
            transition_in=transition_in,
            transition_out=transition_out,
            scale=motion.get("scale"),
            position_x=motion.get("position_x"),
            position_y=motion.get("position_y"),
            rotation=motion.get("rotation"),
        )

    def _read_effects(self, item: ET.Element) -> tuple[list[str], str | None, dict[str, float]]:
        chain_pointer = item.find(".//ComponentOwner/Components")
        chain = self.by_id.get(chain_pointer.attrib.get("ObjectRef", "")) if chain_pointer is not None else None
        effects: list[str] = []
        text = None
        motion: dict[str, float] = {}
        if chain is None:
            return effects, text, motion
        for component_pointer in chain.findall(".//Components/Component"):
            component = self.by_id.get(component_pointer.attrib.get("ObjectRef", ""))
            if component is None:
                continue
            display_name = component.findtext(".//DisplayName") or component.findtext("FilterMatchName")
            if display_name:
                effects.append(display_name)
            if display_name == "Text":
                text = (component.findtext(".//InstanceName") or "").replace("\r", "\n").strip()
            if display_name != "Motion":
                continue
            for parameter_pointer in component.findall(".//Params/Param"):
                parameter = self.by_id.get(parameter_pointer.attrib.get("ObjectRef", ""))
                if parameter is None:
                    continue
                parameter_name = (parameter.findtext("Name") or "").strip()
                current = parameter.findtext("CurrentValue")
                if current is None:
                    continue
                try:
                    if parameter_name == "Scale":
                        motion["scale"] = float(current) / 100.0
                    elif parameter_name == "Rotation":
                        motion["rotation"] = float(current)
                    elif parameter_name == "Position" and ":" in current:
                        x, y = current.split(":", 1)
                        motion["position_x"] = float(x)
                        motion["position_y"] = float(y)
                except ValueError:
                    continue
        return effects, text, motion

    def _read_transition(self, pointer: ET.Element | None) -> PremiereTransition | None:
        if pointer is None:
            return None
        transition = self.by_id.get(pointer.attrib.get("ObjectRef", ""))
        if transition is None:
            return None
        start = _seconds(transition.findtext(".//Start"))
        end = _seconds(transition.findtext(".//End"))
        return PremiereTransition(
            name=transition.findtext(".//DisplayName") or "Transition",
            duration=max(0.0, end - start),
        )

    def primary_sequence(self, requested: str | None = None) -> PremiereSequence:
        if requested:
            try:
                return self.sequences[requested]
            except KeyError as error:
                raise ValueError(f"Unknown Premiere sequence: {requested}") from error
        if "Sequence 01" in self.sequences:
            return self.sequences["Sequence 01"]
        return max(
            self.sequences.values(),
            key=lambda sequence: (
                sum(item.kind == "video" for track in sequence.tracks for item in track.items),
                sequence.duration,
            ),
        )

    def flatten_item(self, item: PremiereItem, kind: str | None = None) -> list[PremiereItem]:
        if not item.nested_sequence:
            return [item]
        nested = self.sequences[item.nested_sequence]
        selected_kind = kind or item.kind
        flattened: list[PremiereItem] = []
        for track in nested.tracks:
            if track.kind != selected_kind or track.muted:
                continue
            for child in track.items:
                overlap_start = max(item.source_start, child.timeline_start)
                overlap_end = min(item.source_end, child.timeline_end)
                if overlap_end - overlap_start <= 0.001:
                    continue
                child_source_start = child.source_start + (overlap_start - child.timeline_start) * child.speed
                child_source_end = child.source_start + (overlap_end - child.timeline_start) * child.speed
                sliced = replace(
                    child,
                    track_index=item.track_index,
                    timeline_start=item.timeline_start + (overlap_start - item.source_start) / item.speed,
                    timeline_end=item.timeline_start + (overlap_end - item.source_start) / item.speed,
                    source_start=child_source_start,
                    source_end=child_source_end,
                    speed=child.speed * item.speed,
                )
                flattened.extend(self.flatten_item(sliced, selected_kind))
        return flattened

    def tree(self) -> dict[str, Any]:
        return {
            "source": str(self.path),
            "sequences": [
                {
                    "name": sequence.name,
                    "resolution": [sequence.width, sequence.height],
                    "work_out": round(sequence.work_out, 6),
                    "duration": round(sequence.duration, 6),
                    "tracks": [
                        {
                            "kind": track.kind,
                            "index": track.index + 1,
                            "muted": track.muted,
                            "items": [asdict(item) for item in track.items],
                        }
                        for track in sequence.tracks
                    ],
                }
                for sequence in self.sequences.values()
            ],
        }


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "premiere-project"


def _transition(value: PremiereTransition | None) -> dict[str, Any]:
    if value is None:
        return {"type": "none", "duration": 0.0}
    transition_type = "dissolve" if "dissolve" in value.name.lower() else "fade"
    return {"type": transition_type, "duration": round(value.duration, 3)}


def _link_or_copy(source: Path, destination: Path, copy_media: bool) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Import destination already exists: {destination}")
    if copy_media:
        shutil.copy2(source, destination)
    else:
        destination.symlink_to(source)


def _still_proxy(source: Path, destination: Path, duration: float, width: int, height: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to import Premiere still images")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-i",
            str(source),
            "-t",
            f"{max(0.1, duration):.3f}",
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
            "-r",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(destination),
        ],
        check=True,
    )


def _preview_slice(source: Path, destination: Path, start: float, duration: float) -> None:
    """Create a visual-only fallback from the authoritative rendered preview."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to create a Premiere preview fallback")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-i",
            str(source),
            "-t",
            f"{max(0.1, duration):.3f}",
            "-an",
            "-vf",
            "tpad=stop_mode=clone:stop_duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(destination),
        ],
        check=True,
    )


def _auto_preview(project_path: Path) -> Path | None:
    candidates = [
        project_path.with_name(project_path.stem.replace(" ", "-") + ".mp4"),
        project_path.with_suffix(".mp4"),
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def import_premiere_project(
    root: Path,
    project_path: Path,
    *,
    project_id: str | None = None,
    name: str | None = None,
    sequence_name: str | None = None,
    preview: Path | None = None,
    copy_media: bool = False,
) -> dict[str, Any]:
    """Create an editable .llmve project from a Premiere timeline.

    Premiere nests are retained in the import report and flattened only for the
    editor timeline, whose current render model is intentionally clip based.
    """
    from .editor import AUDIO_SUFFIXES, VIDEO_SUFFIXES, _asset_record, probe, save_editor_state
    from .project_docs import ensure_project_docs

    root = root.expanduser().resolve()
    premiere = PremiereProject(project_path)
    sequence = premiere.primary_sequence(sequence_name)
    display_name = name or f"{premiere.path.stem} Premiere Import"
    resolved_project_id = project_id or _slug(display_name)
    out_dir = root / "out" / resolved_project_id
    work_dir = root / "work" / resolved_project_id
    if out_dir.exists() or work_dir.exists():
        raise FileExistsError(f"Editor project already exists: {resolved_project_id}")

    resolved_preview = (preview.expanduser().resolve() if preview else _auto_preview(premiere.path))
    if resolved_preview is None or not resolved_preview.is_file():
        raise FileNotFoundError("A rendered Premiere preview MP4 is required for the editor project")

    media_dir = work_dir / "media"
    source_dir = work_dir / "premiere-source"
    warnings: list[str] = []
    try:
        out_dir.mkdir(parents=True)
        media_dir.mkdir(parents=True)
        source_dir.mkdir(parents=True)
        active_media = resolved_preview.name
        _link_or_copy(resolved_preview, out_dir / active_media, copy_media)
        _link_or_copy(premiere.path, source_dir / premiere.path.name, copy_media)

        video_track_count = max((track.index for track in sequence.tracks if track.kind == "video"), default=0) + 1
        audio_track_count = max((track.index for track in sequence.tracks if track.kind == "audio"), default=0) + 1
        tracks = [
            {"id": f"v{index}", "kind": "video", "name": f"V{index} · Premiere", "muted": False, "locked": False}
            for index in range(1, video_track_count + 1)
        ]
        caption_track_id = f"v{video_track_count + 1}"
        if any(track.kind == "caption" for track in sequence.tracks):
            tracks.append({"id": caption_track_id, "kind": "video", "name": "Captions · Premiere", "muted": False, "locked": False})
        tracks.extend(
            {"id": f"a{index}", "kind": "audio", "name": f"A{index} · Premiere", "muted": False, "locked": False}
            for index in range(1, audio_track_count + 1)
        )

        imported: list[tuple[PremiereItem, str]] = []
        for track in sequence.tracks:
            if track.muted:
                continue
            track_id = caption_track_id if track.kind == "caption" else f"{'a' if track.kind == 'audio' else 'v'}{track.index + 1}"
            for item in track.items:
                if item.nested_sequence:
                    for flattened in premiere.flatten_item(item, track.kind):
                        imported.append((flattened, track_id))
                else:
                    imported.append((item, track_id))

        linked_assets: dict[str, Path] = {}
        assets: dict[str, dict[str, Any]] = {}
        preview_proxies: dict[tuple[int, int], str] = {}
        segments: list[dict[str, Any]] = []
        for index, (item, track_id) in enumerate(imported, start=1):
            if item.kind in {"title", "caption"}:
                duration = max(0.1, item.duration)
                segments.append(
                    {
                        "id": f"premiere-{item.kind}-{index}",
                        "label": item.label,
                        "kind": item.kind,
                        "track_id": track_id,
                        "timeline_start": round(item.timeline_start, 3),
                        "duration": round(duration, 3),
                        "text": item.text or item.label,
                        "font_size": 54 if item.kind == "caption" else 64,
                        "text_color": "#ffffff",
                        "transition_in": _transition(item.transition_in),
                        "transition_out": _transition(item.transition_out),
                        "enabled": True,
                    }
                )
                continue

            source = Path(item.media_path) if item.media_path else None
            if source is None or not source.is_file():
                warnings.append(f"Skipped missing media for {item.label}: {item.media_path or 'no path in Premiere project'}")
                continue
            duration = item.duration
            asset_id = source.name
            source_start = item.source_start
            source_end = item.source_end
            suffix = source.suffix.lower()
            visual_fallback = False
            if suffix in VIDEO_SUFFIXES | AUDIO_SUFFIXES:
                try:
                    source_metadata = probe(source)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    if item.kind == "audio":
                        warning = f"Skipped inaccessible audio {item.label}: {source}"
                        if warning not in warnings:
                            warnings.append(warning)
                        continue
                    visual_fallback = True
                else:
                    destination = media_dir / asset_id
                    if asset_id not in linked_assets:
                        _link_or_copy(source, destination, copy_media)
                        linked_assets[asset_id] = destination
                    assets.setdefault(asset_id, _asset_record(resolved_project_id, linked_assets[asset_id], source_metadata))
            else:
                asset_id = f"{source.stem}-still.mp4"
                destination = media_dir / asset_id
                if asset_id not in linked_assets:
                    try:
                        _still_proxy(source, destination, duration, sequence.width or 1080, sequence.height or 1920)
                    except (OSError, RuntimeError, subprocess.SubprocessError):
                        destination.unlink(missing_ok=True)
                        visual_fallback = True
                    else:
                        linked_assets[asset_id] = destination
                if not visual_fallback:
                    source_start = 0.0
                    source_end = max(0.1, duration * item.speed)

            if visual_fallback:
                proxy_key = (round(item.timeline_start * 1000), round(item.timeline_end * 1000))
                asset_id = preview_proxies.get(proxy_key, "")
                if not asset_id:
                    asset_id = f"premiere-preview-{proxy_key[0]:06d}-{proxy_key[1]:06d}.mp4"
                    destination = media_dir / asset_id
                    _preview_slice(resolved_preview, destination, item.timeline_start, duration)
                    linked_assets[asset_id] = destination
                    preview_proxies[proxy_key] = asset_id
                    assets[asset_id] = _asset_record(resolved_project_id, destination, probe(destination))
                source_start = 0.0
                source_end = max(0.1, duration)
                warning = f"Used rendered-preview fallback for inaccessible visual {item.label}: {source}"
                if warning not in warnings:
                    warnings.append(warning)
            elif asset_id not in assets:
                assets[asset_id] = _asset_record(resolved_project_id, linked_assets[asset_id], probe(linked_assets[asset_id]))

            transform_scale = 1.0 if visual_fallback else (item.scale if item.scale is not None else 1.0)
            x = 0.0 if visual_fallback else ((item.position_x or 0.5) - 0.5) * (sequence.width or 1080)
            y = 0.0 if visual_fallback else ((item.position_y or 0.5) - 0.5) * (sequence.height or 1920)
            segment = {
                "id": f"premiere-{item.kind}-{index}",
                "label": item.label,
                "kind": item.kind,
                "asset_id": asset_id,
                "track_id": track_id,
                "audio_track_id": None,
                "audio_linked": False,
                "timeline_start": round(item.timeline_start, 3),
                "source_start": round(source_start, 3),
                "source_end": round(source_end, 3),
                "speed": 1.0 if visual_fallback else round(item.speed, 3),
                "volume": 1.0,
                "audio_fade_in": round(item.transition_in.duration, 3) if item.kind == "audio" and item.transition_in else 0.0,
                "audio_fade_out": round(item.transition_out.duration, 3) if item.kind == "audio" and item.transition_out else 0.0,
                "transition_in": _transition(item.transition_in) if item.kind == "video" else {"type": "none", "duration": 0.0},
                "transition_out": _transition(item.transition_out) if item.kind == "video" else {"type": "none", "duration": 0.0},
                "transform": {
                    "scale": round(transform_scale, 3),
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "rotation": int(round(item.rotation or 0.0) / 90.0) * 90 % 360,
                    "crop_top": 0.0,
                    "crop_right": 0.0,
                    "crop_bottom": 0.0,
                    "crop_left": 0.0,
                },
                "color": {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "temperature": 0.0},
                "keyframes": [],
                "enabled": True,
            }
            segments.append(segment)

        preview_metadata = probe(out_dir / active_media)
        preview_asset = _asset_record(resolved_project_id, out_dir / active_media, preview_metadata)
        state = {
            "version": 2,
            "project_id": resolved_project_id,
            "media": active_media,
            "duration": preview_metadata["duration"],
            "resolution": [sequence.width or preview_metadata["width"], sequence.height or preview_metadata["height"]],
            "fps": preview_metadata["fps"],
            "has_audio": preview_metadata["has_audio"],
            "assets": [preview_asset, *assets.values()],
            "tracks": tracks,
            "segments": segments,
            "export_preset": "vertical" if sequence.height > sequence.width else "source",
        }
        saved = save_editor_state(root, resolved_project_id, active_media, state)
        project_file = work_dir / f"{resolved_project_id}.llmve"
        project_data = json.loads(project_file.read_text())
        project_data["name"] = display_name
        project_data["premiere_import"] = {
            "source_project": str(premiere.path),
            "source_project_sha256": hashlib.sha256(premiere.path.read_bytes()).hexdigest(),
            "source_copy": str((source_dir / premiere.path.name).relative_to(root)),
            "preview": str(resolved_preview),
            "primary_sequence": sequence.name,
            "imported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "media_mode": "copy" if copy_media else "link",
            "warnings": warnings,
            "sequence_tree": premiere.tree(),
        }
        project_file.write_text(json.dumps(project_data, indent=2) + "\n")
        report_path = work_dir / "premiere-import-report.json"
        report_path.write_text(json.dumps(project_data["premiere_import"], indent=2) + "\n")
        ensure_project_docs(root, resolved_project_id, saved)
        return {
            "project_id": resolved_project_id,
            "project_file": str(project_file),
            "report": str(report_path),
            "source_project": str(premiere.path),
            "active_media": active_media,
            "sequence": sequence.name,
            "tracks": len(tracks),
            "segments": len(saved["segments"]),
            "assets": len(assets) + 1,
            "warnings": warnings,
        }
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
