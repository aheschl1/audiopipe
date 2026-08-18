"""Canonical 44-byte WAV headers for server-synthesized segments.

The layout matches what streaming clients upload and what
:mod:`services.stitch` assumes: PCM, 16-bit samples, one ``data`` chunk.
"""

import struct

WAV_HEADER_BYTES = 44

_BYTES_PER_SAMPLE = 2


def wav_header(data_bytes: int, sample_rate_hz: int, channels: int) -> bytes:
    """The 44-byte header for ``data_bytes`` of 16-bit PCM."""
    byte_rate = sample_rate_hz * channels * _BYTES_PER_SAMPLE
    block_align = channels * _BYTES_PER_SAMPLE
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate_hz, byte_rate, block_align, 16)
        + b"data"
        + struct.pack("<I", data_bytes)
    )


def wrap_pcm(pcm: bytes, sample_rate_hz: int, channels: int) -> bytes:
    """A complete WAV file around raw 16-bit PCM."""
    return wav_header(len(pcm), sample_rate_hz, channels) + pcm


def pcm_duration_ms(data_bytes: int, sample_rate_hz: int, channels: int) -> int:
    """How many milliseconds of audio ``data_bytes`` of 16-bit PCM spans."""
    return round(data_bytes * 1000 / (sample_rate_hz * channels * _BYTES_PER_SAMPLE))
