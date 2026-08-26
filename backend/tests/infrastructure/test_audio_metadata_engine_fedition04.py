"""F-EDITION-04: legacy projection codec-aware bit depth."""
from types import SimpleNamespace

from infrastructure.audio.metadata_engine import legacy_audio_projection
from models.audio_metadata import (
    AudioMetadataDocument,
    AudioTechnicalInfo,
    FileAttributeSnapshot,
    NativeMetadataSnapshot,
    ReadAudioDocument,
)


def _document(detected_format, codec, bit_depth) -> ReadAudioDocument:
    technical = AudioTechnicalInfo(
        duration_seconds=200.0,
        bitrate_bps=900_000,
        sample_rate_hz=44_100,
        channels=2,
        bit_depth=bit_depth,
        codec=codec,
        file_size_bytes=1000,
    )
    probe = SimpleNamespace(detected_format=detected_format)
    return ReadAudioDocument(
        probe=probe,  # type: ignore[arg-type]
        metadata=AudioMetadataDocument(fields=()),
        artwork=(),
        technical=technical,
        raw_tags=(),
        native_tags=NativeMetadataSnapshot(storage_kind="none"),
        file_attributes=FileAttributeSnapshot(
            atime_ns=1, mtime_ns=1, permission_bits=0o644
        ),
        warnings=(),
    )


def test_alac_m4a_retains_depth():
    _, info = legacy_audio_projection(_document("m4a", "alac", 16))
    assert info.bit_depth == 16
    assert info.file_format == "m4a"


def test_aac_m4a_suppresses_synthetic_depth():
    _, info = legacy_audio_projection(_document("m4a", "mp4a", 16))
    assert info.bit_depth is None


def test_mp4_mov_with_alac_keep_depth():
    for fmt in ("mp4", "mov"):
        _, info = legacy_audio_projection(_document(fmt, "alac", 24))
        assert info.bit_depth == 24


def test_flac_wav_unchanged():
    for fmt in ("flac", "wav"):
        _, info = legacy_audio_projection(_document(fmt, "", 16))
        assert info.bit_depth == 16


def test_unknown_codec_no_depth():
    _, info = legacy_audio_projection(_document("m4a", None, None))
    assert info.bit_depth is None
