from __future__ import annotations

import base64
import gzip
from pathlib import Path

import pytest

from video_pipeline.premiere import (
    PREMIERE_TICKS_PER_SECOND,
    PremiereItem,
    PremiereProject,
    PremiereSequence,
    PremiereTrack,
    _decode_formatted_text,
)


def test_decodes_utf8_caption_envelope_with_line_break_and_curly_apostrophe():
    text = "Maybe it fits\rIt’s a 2 + 2."
    font = "ArialMT"
    payload = (
        b"binary-prefix"
        + len(font).to_bytes(4, "little")
        + font.encode()
        + b"\0style-data"
        + len(text).to_bytes(4, "little")
        + text.encode()
        + b"\0\0"
    )

    decoded = _decode_formatted_text(base64.b64encode(payload).decode())

    assert decoded == "Maybe it fits\nIt’s a 2 + 2."


def test_reads_gzip_premiere_sequence_and_clip(tmp_path: Path):
    ticks = PREMIERE_TICKS_PER_SECOND
    xml = f'''<?xml version="1.0" encoding="UTF-8" ?>
<PremiereData Version="3">
  <Sequence ObjectUID="sequence" Version="11">
    <Node><Properties><MZ.Sequence.PreviewFrameSizeWidth>1080</MZ.Sequence.PreviewFrameSizeWidth><MZ.Sequence.PreviewFrameSizeHeight>1920</MZ.Sequence.PreviewFrameSizeHeight><MZ.WorkOutPoint>{3 * ticks}</MZ.WorkOutPoint></Properties></Node>
    <TrackGroups><TrackGroup><Second ObjectRef="10" /></TrackGroup></TrackGroups><Name>Sequence 01</Name>
  </Sequence>
  <VideoTrackGroup ObjectID="10"><TrackGroup><Tracks><Track ObjectURef="track" /></Tracks></TrackGroup></VideoTrackGroup>
  <VideoClipTrack ObjectUID="track"><ClipTrack><Track><Index>0</Index><IsMuted>false</IsMuted></Track><ClipItems><TrackItems><TrackItem ObjectRef="20" /></TrackItems></ClipItems></ClipTrack></VideoClipTrack>
  <VideoClipTrackItem ObjectID="20"><ClipTrackItem><TrackItem><Start>0</Start><End>{3 * ticks}</End></TrackItem><SubClip ObjectRef="21" /></ClipTrackItem></VideoClipTrackItem>
  <SubClip ObjectID="21"><Clip ObjectRef="22" /><Name>camera.mov</Name></SubClip>
  <VideoClip ObjectID="22"><Clip><InPoint>{4 * ticks}</InPoint><OutPoint>{10 * ticks}</OutPoint><PlaybackSpeed>2</PlaybackSpeed></Clip></VideoClip>
  <Media ObjectUID="media"><Title>camera.mov</Title><ActualMediaFilePath>/tmp/camera.mov</ActualMediaFilePath></Media>
</PremiereData>'''
    project_file = tmp_path / "fixture.prproj"
    with gzip.open(project_file, "wb") as destination:
        destination.write(xml.encode())

    project = PremiereProject(project_file)
    sequence = project.primary_sequence()
    item = sequence.tracks[0].items[0]

    assert sequence.name == "Sequence 01"
    assert (sequence.width, sequence.height) == (1080, 1920)
    assert sequence.duration == pytest.approx(3.0)
    assert item.label == "camera.mov"
    assert item.source_start == pytest.approx(4.0)
    assert item.source_end == pytest.approx(10.0)
    assert item.speed == pytest.approx(2.0)


def test_flattens_selected_range_from_nested_sequence():
    child = PremiereItem(
        object_id="child",
        kind="video",
        track_index=0,
        timeline_start=1.0,
        timeline_end=4.0,
        label="camera.mov",
        source_start=10.0,
        source_end=16.0,
        speed=2.0,
        media_path="/tmp/camera.mov",
    )
    nested = PremiereSequence(
        name="Nested Sequence 01",
        width=1080,
        height=1920,
        work_out=4.0,
        tracks=(PremiereTrack(kind="video", index=0, muted=False, items=(child,)),),
    )
    parent = PremiereItem(
        object_id="parent",
        kind="video",
        track_index=2,
        timeline_start=5.0,
        timeline_end=6.5,
        label=nested.name,
        source_start=2.0,
        source_end=3.5,
        speed=1.0,
        nested_sequence=nested.name,
    )
    project = object.__new__(PremiereProject)
    project.sequences = {nested.name: nested}

    flattened = project.flatten_item(parent)

    assert len(flattened) == 1
    assert flattened[0].track_index == 2
    assert flattened[0].timeline_start == pytest.approx(5.0)
    assert flattened[0].timeline_end == pytest.approx(6.5)
    assert flattened[0].source_start == pytest.approx(12.0)
    assert flattened[0].source_end == pytest.approx(15.0)
    assert flattened[0].speed == pytest.approx(2.0)
