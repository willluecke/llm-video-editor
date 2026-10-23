from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


ASSET_MANIFEST_VERSION = 1
MEDIA_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".m4a", ".mp3", ".wav", ".aac", ".flac"}
Probe = Callable[[Path], dict[str, Any]]


def asset_manifest_path(root: Path, project_id: str) -> Path:
    return root / "work" / project_id / "assets.json"


def derived_cache_root(root: Path, project_id: str, content_id: str | None = None) -> Path:
    base = root / "work" / project_id / "cache"
    if not content_id:
        return base
    digest = content_id.removeprefix("sha256:")
    return base / digest


def file_sha256(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load_asset_manifest(root: Path, project_id: str) -> dict[str, Any]:
    path = asset_manifest_path(root, project_id)
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = {}
    assets = raw.get("assets") if isinstance(raw.get("assets"), list) else []
    return {
        "format": "llm-video-editor-assets",
        "version": ASSET_MANIFEST_VERSION,
        "project_id": project_id,
        "updated_at": raw.get("updated_at"),
        "assets": [item for item in assets if isinstance(item, dict) and item.get("library_id")],
    }


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _project_candidates(root: Path, project_id: str) -> dict[str, Path]:
    candidates: dict[str, Path] = {}
    for directory in (root / "out" / project_id, root / "work" / project_id / "media"):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.suffix.lower() in MEDIA_SUFFIXES and (path.is_file() or path.is_symlink()):
                candidates.setdefault(path.name, path)
    return candidates


def _referenced_asset_ids(state: dict[str, Any] | None) -> set[str]:
    if not state:
        return set()
    result = {
        Path(str(item.get("id", ""))).name
        for item in state.get("assets", [])
        if isinstance(item, dict) and item.get("id")
    }
    result.update(
        Path(str(item.get("asset_id", ""))).name
        for item in state.get("segments", [])
        if isinstance(item, dict) and item.get("asset_id")
    )
    return {item for item in result if item}


def _storage_mode(root: Path, project_id: str, path: Path) -> str:
    if path.is_symlink():
        return "linked"
    try:
        path.relative_to(root / "work" / project_id / "media")
    except ValueError:
        return "rendered-output"
    return "project-copy"


def _record_for(
    root: Path,
    project_id: str,
    library_id: str,
    path: Path | None,
    previous: dict[str, Any] | None,
    probe_media: Probe | None,
) -> dict[str, Any]:
    online = bool(path and path.is_file())
    source = str(path.resolve(strict=False)) if path else str(previous.get("source", "") if previous else "")
    project_path = ""
    if path:
        try:
            project_path = str(path.relative_to(root))
        except ValueError:
            project_path = str(path)
    elif previous:
        project_path = str(previous.get("project_path", ""))

    stat: dict[str, int] | None = None
    content_id = previous.get("content_id") if previous else None
    metadata = previous.get("probe") if previous and isinstance(previous.get("probe"), dict) else None
    if online and path:
        info = path.stat()
        stat = {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
        unchanged = previous and previous.get("stat") == stat and previous.get("content_id")
        if not unchanged:
            content_id = f"sha256:{file_sha256(path)}"
            metadata = None
        if metadata is None and probe_media:
            try:
                metadata = probe_media(path)
            except Exception as error:
                metadata = {"error": str(error)}

    return {
        "library_id": library_id,
        "content_id": content_id,
        "name": Path(library_id).stem.replace("_", " "),
        "project_path": project_path,
        "source": source,
        "storage": _storage_mode(root, project_id, path) if path else str(previous.get("storage", "missing") if previous else "missing"),
        "mime_type": mimetypes.guess_type(library_id)[0] or "application/octet-stream",
        "status": "online" if online else "missing",
        "stat": stat or (previous.get("stat") if previous else None),
        "probe": metadata,
        "cache_path": (
            str(derived_cache_root(root, project_id, str(content_id)).relative_to(root))
            if content_id
            else None
        ),
    }


def sync_asset_manifest(
    root: Path,
    project_id: str,
    *,
    state: dict[str, Any] | None = None,
    probe_media: Probe | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    previous = load_asset_manifest(root, project_id)
    old = {str(item["library_id"]): item for item in previous["assets"]}
    paths = _project_candidates(root, project_id)
    ids = set(paths) | set(old) | _referenced_asset_ids(state)
    assets = [
        _record_for(root, project_id, library_id, paths.get(library_id), old.get(library_id), probe_media)
        for library_id in sorted(ids, key=str.casefold)
    ]
    manifest = {
        "format": "llm-video-editor-assets",
        "version": ASSET_MANIFEST_VERSION,
        "project_id": project_id,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "assets": assets,
    }
    _atomic_json(asset_manifest_path(root, project_id), manifest)
    for item in assets:
        if item.get("content_id"):
            derived_cache_root(root, project_id, str(item["content_id"])).mkdir(parents=True, exist_ok=True)
    return manifest


def attach_manifest_to_state(state: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    records = {str(item["library_id"]): item for item in manifest.get("assets", [])}
    for asset in state.get("assets", []):
        record = records.get(str(asset.get("id", "")))
        if not record:
            continue
        asset["content_id"] = record.get("content_id")
        asset["content_status"] = record.get("status")
        asset["storage"] = record.get("storage")
        asset["source"] = record.get("source")
    state["asset_manifest"] = str(Path("work") / str(state.get("project_id", "")) / "assets.json")
    return state


def _safe_library_id(asset_id: str) -> str:
    safe = Path(asset_id).name
    if safe != asset_id or not safe or Path(safe).suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("Invalid project asset id")
    return safe


def relink_asset(
    root: Path,
    project_id: str,
    asset_id: str,
    source: Path,
    *,
    copy_media: bool = False,
    probe_media: Probe | None = None,
) -> dict[str, Any]:
    library_id = _safe_library_id(asset_id)
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Relink source does not exist: {source}")
    if source.suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("Relink source is not supported media")
    manifest = load_asset_manifest(root, project_id)
    record = next((item for item in manifest["assets"] if item["library_id"] == library_id), None)
    destination = root / str(record.get("project_path")) if record and record.get("project_path") else root / "work" / project_id / "media" / library_id
    try:
        destination.resolve(strict=False).relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("Asset destination escapes the editor root") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.relink-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        if copy_media:
            shutil.copy2(source, temporary)
        else:
            temporary.symlink_to(source)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return sync_asset_manifest(root, project_id, probe_media=probe_media)


def make_assets_portable(
    root: Path,
    project_id: str,
    asset_ids: Iterable[str] | None = None,
    *,
    probe_media: Probe | None = None,
) -> dict[str, Any]:
    selected = {_safe_library_id(item) for item in asset_ids} if asset_ids else None
    manifest = sync_asset_manifest(root, project_id, probe_media=probe_media)
    for record in manifest["assets"]:
        if record.get("storage") != "linked" or record.get("status") != "online":
            continue
        if selected is not None and record["library_id"] not in selected:
            continue
        destination = root / str(record["project_path"])
        source = destination.resolve()
        temporary = destination.with_name(f".{destination.name}.portable-{os.getpid()}")
        temporary.unlink(missing_ok=True)
        try:
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return sync_asset_manifest(root, project_id, probe_media=probe_media)


def import_asset_path(
    root: Path,
    project_id: str,
    source: Path,
    *,
    name: str | None = None,
    probe_media: Probe | None = None,
) -> tuple[Path, dict[str, Any]]:
    source = source.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("Choose a supported media file")
    destination_dir = root / "work" / project_id / "media"
    destination_dir.mkdir(parents=True, exist_ok=True)
    requested = _safe_library_id(name or source.name)
    destination = destination_dir / requested
    index = 2
    while destination.exists() or destination.is_symlink() or (root / "out" / project_id / destination.name).exists():
        destination = destination_dir / f"{Path(requested).stem}-{index}{Path(requested).suffix}"
        index += 1
    shutil.copy2(source, destination)
    manifest = sync_asset_manifest(root, project_id, probe_media=probe_media)
    return destination, manifest
