"""The PyAV transforms behind the thumbnail and video-proxy pipelines."""

import io

import av

from processing import media
from tests.visual_media import jpeg_bytes, mp4_bytes


def test_image_thumbnail_fits_and_keeps_aspect() -> None:
    still = media.image_thumbnail(jpeg_bytes(640, 480))
    assert (still.width, still.height) == (320, 240)
    assert still.data[:2] == b"\xff\xd8"


def test_video_frames_are_evenly_spaced_stills() -> None:
    stills = media.video_frames(mp4_bytes(seconds=3))
    assert len(stills) == 3
    offsets = [s.offset_ms for s in stills]
    assert offsets == sorted(offsets)
    assert 300 <= offsets[0] <= 700 and 2300 <= offsets[-1] <= 2700
    assert all(s.data[:2] == b"\xff\xd8" for s in stills)


def test_proxy_is_seekable_h264_with_audio() -> None:
    clip = media.proxy(mp4_bytes(seconds=2), max_side=160)
    assert (clip.width, clip.height) == (160, 120)
    assert 1700 <= clip.duration_ms <= 2000
    # moov ahead of mdat: browsers can start and seek before the whole file.
    assert clip.data.index(b"moov") < clip.data.index(b"mdat")
    with av.open(io.BytesIO(clip.data)) as container:
        assert container.streams.video[0].codec_context.name == "h264"
        assert container.streams.audio[0].codec_context.name == "aac"


def test_frame_count_is_one_per_second_capped() -> None:
    assert media.frame_count(300) == 1
    assert media.frame_count(11_553) == 12
    assert media.frame_count(600_000) == media.MAX_FRAMES


def test_candidate_frames_sample_at_the_requested_rate() -> None:
    candidates = media.candidate_frames(mp4_bytes(seconds=3), fps=2.0)
    assert [c.offset_ms for c in candidates] == [0, 500, 1000, 1500, 2000, 2500]
    assert all(c.jpeg[:2] == b"\xff\xd8" and c.sharpness >= 0 for c in candidates)
    # The fixture's green channel ramps up over the clip.
    assert candidates[-1].brightness > candidates[0].brightness


def test_frames_at_returns_the_requested_offsets() -> None:
    stills = media.frames_at(mp4_bytes(seconds=3, width=320, height=240), [1500, 0], max_side=200)
    assert sorted(stills) == [0, 1500]
    assert (stills[1500].width, stills[1500].height) == (200, 150)
    assert stills[1500].offset_ms == 1500


def test_image_still_is_both_a_keyframe_and_a_candidate() -> None:
    still, candidate = media.image_still(jpeg_bytes(2000, 1000))
    assert (still.width, still.height) == (1024, 512)
    assert candidate.offset_ms == 0 and candidate.jpeg[:2] == b"\xff\xd8"


def test_sharpness_separates_edges_from_flat() -> None:
    import numpy as np

    flat = np.full((64, 64), 128.0)
    checker = np.indices((64, 64)).sum(axis=0) % 2 * 255.0
    assert media.sharpness(flat) == 0
    assert media.sharpness(checker) > 1000
