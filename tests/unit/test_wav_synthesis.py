"""Server-synthesized WAV headers match the canonical client layout."""

import io
import wave

from services.wav import WAV_HEADER_BYTES, pcm_duration_ms, wav_header, wrap_pcm
from tests.wav import wav_bytes


def test_header_matches_client_layout() -> None:
    reference = wav_bytes(seconds=0.1)
    pcm = reference[WAV_HEADER_BYTES:]
    assert wrap_pcm(pcm, 16000, 1) == reference


def test_header_parses_with_stdlib() -> None:
    pcm = b"\x00\x01" * 480
    with wave.open(io.BytesIO(wrap_pcm(pcm, 48000, 2)), "rb") as handle:
        assert handle.getframerate() == 48000
        assert handle.getnchannels() == 2
        assert handle.getsampwidth() == 2
        assert handle.readframes(handle.getnframes()) == pcm


def test_header_length() -> None:
    assert len(wav_header(0, 16000, 1)) == WAV_HEADER_BYTES


def test_pcm_duration() -> None:
    assert pcm_duration_ms(32000, 16000, 1) == 1000
    assert pcm_duration_ms(1600, 16000, 1) == 50
    assert pcm_duration_ms(64000, 16000, 2) == 1000
