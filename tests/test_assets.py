from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from video_pipeline.assets import (
    attach_manifest_to_state,
    load_asset_manifest,
    make_assets_portable,
    relink_asset,
    sync_asset_manifest,
)


def _probe(path: Path) -> dict:
    return {
        "duration": float(path.stat().st_size),
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "has_audio": True,
    }


def test_asset_manifest_uses_content_ids_and_tracks_link_status(tmp_path: Path):
    project_id = "series"
    source = tmp_path / "camera.mov"
    source.write_bytes(b"same media")
    out = tmp_path / "out" / project_id
    media = tmp_path / "work" / project_id / "media"
    out.mkdir(parents=True)
    media.mkdir(parents=True)
    (out / "reference.mp4").write_bytes(b"same media")
    (media / "camera.mov").symlink_to(source)

    manifest = sync_asset_manifest(tmp_path, project_id, probe_media=_probe)
    records = {item["library_id"]: item for item in manifest["assets"]}

    assert records["camera.mov"]["content_id"] == records["reference.mp4"]["content_id"]
    assert records["camera.mov"]["storage"] == "linked"
    assert records["reference.mp4"]["storage"] == "rendered-output"
    assert records["camera.mov"]["status"] == "online"
    assert (tmp_path / records["camera.mov"]["cache_path"]).is_dir()

    state = {"project_id": project_id, "assets": [{"id": "camera.mov"}]}
    attach_manifest_to_state(state, manifest)
    assert state["assets"][0]["content_id"].startswith("sha256:")
    assert state["assets"][0]["source"] == str(source)


def test_asset_relink_and_make_portable_are_explicit(tmp_path: Path):
    project_id = "series"
    original = tmp_path / "original.mov"
    replacement = tmp_path / "replacement.mov"
    original.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    media = tmp_path / "work" / project_id / "media"
    media.mkdir(parents=True)
    linked = media / "shot.mov"
    linked.symlink_to(original)
    first = sync_asset_manifest(tmp_path, project_id, probe_media=_probe)
    first_id = first["assets"][0]["content_id"]

    relinked = relink_asset(tmp_path, project_id, "shot.mov", replacement, probe_media=_probe)
    record = relinked["assets"][0]
    assert linked.is_symlink()
    assert linked.resolve() == replacement
    assert record["content_id"] != first_id

    portable = make_assets_portable(tmp_path, project_id, probe_media=_probe)
    assert linked.is_file()
    assert not linked.is_symlink()
    assert linked.read_bytes() == b"replacement"
    assert portable["assets"][0]["storage"] == "project-copy"
    assert load_asset_manifest(tmp_path, project_id)["assets"][0]["status"] == "online"


def test_missing_referenced_asset_remains_in_manifest(tmp_path: Path):
    state = {
        "project_id": "series",
        "assets": [{"id": "gone.mov"}],
        "segments": [{"asset_id": "gone.mov"}],
    }
    manifest = sync_asset_manifest(tmp_path, "series", state=state, probe_media=_probe)
    assert manifest["assets"][0]["library_id"] == "gone.mov"
    assert manifest["assets"][0]["status"] == "missing"
    assert manifest["assets"][0]["content_id"] is None


def test_concurrent_manifest_refreshes_use_independent_atomic_files(tmp_path: Path):
    project_id = "series"
    media = tmp_path / "work" / project_id / "media"
    media.mkdir(parents=True)
    (media / "camera.mov").write_bytes(b"same media")

    with ThreadPoolExecutor(max_workers=8) as pool:
        manifests = list(pool.map(lambda _: sync_asset_manifest(tmp_path, project_id, probe_media=_probe), range(24)))

    assert all(item["assets"][0]["status"] == "online" for item in manifests)
    saved = load_asset_manifest(tmp_path, project_id)
    assert saved["assets"][0]["library_id"] == "camera.mov"
    assert not list((tmp_path / "work" / project_id).glob(".assets.json.*.tmp"))
