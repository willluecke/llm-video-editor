from pathlib import Path

from video_pipeline.checks import blocking_preflight_issues, check_state


def _manifest(*assets: tuple[str, float]) -> dict:
    return {
        "assets": [
            {
                "library_id": name,
                "status": "online",
                "probe": {"duration": duration},
            }
            for name, duration in assets
        ]
    }


def _clip(clip_id: str, asset_id: str, start: float, source_start: float, source_end: float, slot_id: str | None = None) -> dict:
    return {
        "id": clip_id,
        "kind": "video",
        "asset_id": asset_id,
        "track_id": "v1",
        "timeline_start": start,
        "source_start": source_start,
        "source_end": source_end,
        "speed": 1,
        "slot_id": slot_id,
        "enabled": True,
    }


def test_check_reports_missing_media_gaps_and_out_of_bounds(tmp_path: Path):
    state = {
        "resolution": [1080, 1920],
        "fps": 30,
        "tracks": [{"id": "v1", "kind": "video", "muted": False, "locked": False}],
        "segments": [
            _clip("first", "one.mov", 0, 0, 1),
            _clip("second", "missing.mov", 2, 0, 3),
        ],
    }
    report = check_state(tmp_path, "series", state, manifest=_manifest(("one.mov", 0.5)))
    codes = {item["code"] for item in report["issues"]}
    assert {"source-out-of-bounds", "source-missing", "black-frame-schedule"} <= codes
    assert blocking_preflight_issues(report)
    gap = next(item for item in report["issues"] if item["code"] == "black-frame-schedule")
    assert gap["ranges"] == [{"start": 1.0, "end": 2.0}]


def test_check_enforces_explicit_series_constraints(tmp_path: Path):
    state = {
        "resolution": [1920, 1080],
        "fps": 24,
        "tracks": [
            {"id": "v1", "kind": "video", "muted": False, "locked": False},
            {"id": "v2", "kind": "video", "muted": False, "locked": False},
        ],
        "segments": [
            _clip("question", "one.mov", 0, 0, 1, "question"),
            _clip("payoff", "one.mov", 1, 1, 2, "payoff"),
        ],
        "episode_template": {
            "slots": [
                {"id": "question", "label": "Question", "start": 0, "end": 1, "required": True},
                {"id": "payoff", "label": "Payoff", "start": 1, "end": 2, "required": True},
            ],
            "question_slot_id": "question",
            "payoff_slot_id": "payoff",
            "locked_track_ids": ["v2"],
            "reserved_track_ids": [],
            "constraints": {
                "resolution": [1080, 1920],
                "fps": 30,
                "runtime": {"min": 3, "max": 5},
                "required_locked_track_ids": ["v2"],
                "slot_timing_tolerance": 0.05,
            },
        },
    }
    report = check_state(tmp_path, "series", state, manifest=_manifest(("one.mov", 2)))
    codes = {item["code"] for item in report["issues"]}
    assert {"format-resolution", "format-fps", "runtime-short", "identity-track-unlocked"} <= codes


def test_check_accepts_complete_contiguous_template(tmp_path: Path):
    state = {
        "resolution": [1080, 1920],
        "fps": 30,
        "tracks": [
            {"id": "v1", "kind": "video", "muted": False, "locked": False},
            {"id": "v2", "kind": "video", "muted": False, "locked": True},
        ],
        "segments": [
            _clip("question", "one.mov", 0, 0, 1, "question"),
            _clip("payoff", "one.mov", 1, 1, 2, "payoff"),
        ],
        "episode_template": {
            "slots": [
                {"id": "question", "label": "Question", "start": 0, "end": 1, "required": True},
                {"id": "payoff", "label": "Payoff", "start": 1, "end": 2, "required": True},
            ],
            "question_slot_id": "question",
            "payoff_slot_id": "payoff",
            "locked_track_ids": ["v2"],
            "reserved_track_ids": [],
            "constraints": {
                "resolution": [1080, 1920], "fps": 30,
                "runtime": {"min": 1, "max": 3},
                "required_locked_track_ids": ["v2"],
                "slot_timing_tolerance": 0.05,
            },
        },
    }
    report = check_state(tmp_path, "series", state, manifest=_manifest(("one.mov", 2)))
    assert report["valid"] is True
    assert report["issues"] == []
