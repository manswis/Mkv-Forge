"""Pure global Playback Profile presets and anti-false-negative compatibility engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.config import AppConstants


@dataclass(frozen=True, slots=True)
class PlaybackProfileSpec:
    """Resolved household sound system playback capabilities."""

    preset: str
    label: str
    supported_codecs: frozenset[str]
    max_channels: int
    preferred_target_codec: str
    configured: bool


@dataclass(frozen=True, slots=True)
class PlaybackPresetDefinition:
    """Static metadata for one named hardware playback preset."""

    preset_id: str
    label: str
    short_label: str
    supported_codecs: tuple[str, ...]
    max_channels: int
    preferred_target_codec: str


# Every preset's preferred_target_codec is strictly bounded to ("eac3", "ac3", "aac").
PLAYBACK_PRESETS: Mapping[str, PlaybackPresetDefinition] = {
    AppConstants.PLAYBACK_PRESET_DOLBY_ARC: PlaybackPresetDefinition(
        preset_id=AppConstants.PLAYBACK_PRESET_DOLBY_ARC,
        label="Soundbar / Smart TV (HDMI ARC)",
        short_label="HDMI ARC (5.1)",
        supported_codecs=("eac3", "ac3", "aac_20"),
        max_channels=AppConstants.CHANNELS_51,
        preferred_target_codec=AppConstants.TARGET_CODEC_EAC3,
    ),
    AppConstants.PLAYBACK_PRESET_OPTICAL_LEGACY: PlaybackPresetDefinition(
        preset_id=AppConstants.PLAYBACK_PRESET_OPTICAL_LEGACY,
        label="Optical Soundbar / Legacy AVR (TOSLINK)",
        short_label="Optical (5.1)",
        supported_codecs=("ac3", "aac_20"),
        max_channels=AppConstants.CHANNELS_51,
        preferred_target_codec=AppConstants.TARGET_CODEC_AC3,
    ),
    AppConstants.PLAYBACK_PRESET_EARC_AVR: PlaybackPresetDefinition(
        preset_id=AppConstants.PLAYBACK_PRESET_EARC_AVR,
        label="Home Theater AVR (HDMI eARC · 7.1 / Atmos)",
        short_label="eARC AVR (7.1)",
        supported_codecs=(
            "truehd",
            "dts_hd_ma",
            "eac3",
            "ac3",
            "dts",
            "flac",
            "pcm",
            "aac_20",
        ),
        max_channels=AppConstants.CHANNELS_71,
        preferred_target_codec=AppConstants.TARGET_CODEC_EAC3,
    ),
    AppConstants.PLAYBACK_PRESET_STREAMING: PlaybackPresetDefinition(
        preset_id=AppConstants.PLAYBACK_PRESET_STREAMING,
        label="Plex / Jellyfin / Infuse (Apple TV, Shield, Web)",
        short_label="Streaming Clients (7.1)",
        supported_codecs=("eac3", "ac3", "aac_20", "aac_51", "opus", "flac"),
        max_channels=AppConstants.CHANNELS_71,
        preferred_target_codec=AppConstants.TARGET_CODEC_AAC,
    ),
    AppConstants.PLAYBACK_PRESET_STEREO_MOBILE: PlaybackPresetDefinition(
        preset_id=AppConstants.PLAYBACK_PRESET_STEREO_MOBILE,
        label="Stereo / Headphones / Mobile (2.0)",
        short_label="Stereo (2.0)",
        supported_codecs=("aac_20", "opus", "ac3", "eac3", "mp3"),
        max_channels=AppConstants.CHANNELS_STEREO,
        preferred_target_codec=AppConstants.TARGET_CODEC_AAC,
    ),
}


def sanitize_supported_codecs(raw_codecs: Sequence[str] | None) -> list[str]:
    """Normalize, deduplicate, and filter codec keys against AppConstants."""
    if not raw_codecs:
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for entry in raw_codecs:
        if not isinstance(entry, str):
            continue
        key = entry.strip().lower()
        if key in AppConstants.ALLOWED_PLAYBACK_CODEC_KEYS and key not in seen:
            seen.add(key)
            cleaned.append(key)
    return cleaned


def resolve_playback_profile(
    *,
    preset: str | None = None,
    supported_codecs: Sequence[str] | None = None,
    max_channels: int | None = None,
    preferred_target_codec: str | None = None,
) -> PlaybackProfileSpec:
    """Resolve persisted global settings fields into a deterministic PlaybackProfileSpec."""
    normalized_preset = (
        preset.strip().lower()
        if isinstance(preset, str) and preset.strip()
        else AppConstants.PLAYBACK_PRESET_NEUTRAL
    )
    if normalized_preset not in AppConstants.ALLOWED_PLAYBACK_PRESETS:
        normalized_preset = AppConstants.PLAYBACK_PRESET_NEUTRAL

    if normalized_preset == AppConstants.PLAYBACK_PRESET_NEUTRAL:
        return PlaybackProfileSpec(
            preset=AppConstants.PLAYBACK_PRESET_NEUTRAL,
            label="Not Configured",
            supported_codecs=frozenset(),
            max_channels=AppConstants.DEFAULT_PLAYBACK_MAX_CHANNELS,
            preferred_target_codec=AppConstants.DEFAULT_PLAYBACK_PREFERRED_CODEC,
            configured=False,
        )

    if normalized_preset in PLAYBACK_PRESETS:
        defn = PLAYBACK_PRESETS[normalized_preset]
        return PlaybackProfileSpec(
            preset=defn.preset_id,
            label=defn.short_label,
            supported_codecs=frozenset(defn.supported_codecs),
            max_channels=defn.max_channels,
            preferred_target_codec=defn.preferred_target_codec,
            configured=True,
        )

    # Custom profile
    cleaned_codecs = sanitize_supported_codecs(supported_codecs)
    resolved_max_ch = (
        int(max_channels)
        if max_channels in AppConstants.ALLOWED_PLAYBACK_MAX_CHANNELS
        else AppConstants.DEFAULT_PLAYBACK_MAX_CHANNELS
    )
    resolved_preferred = (
        preferred_target_codec.strip().lower()
        if isinstance(preferred_target_codec, str)
        and preferred_target_codec.strip().lower()
        in AppConstants.ALLOWED_PLAYBACK_PREFERRED_CODECS
        else AppConstants.DEFAULT_PLAYBACK_PREFERRED_CODEC
    )
    ch_label = (
        "7.1"
        if resolved_max_ch == AppConstants.CHANNELS_71
        else "2.0"
        if resolved_max_ch == AppConstants.CHANNELS_STEREO
        else "5.1"
    )
    return PlaybackProfileSpec(
        preset=AppConstants.PLAYBACK_PRESET_CUSTOM,
        label=f"Custom ({ch_label})",
        supported_codecs=frozenset(cleaned_codecs),
        max_channels=resolved_max_ch,
        preferred_target_codec=resolved_preferred,
        configured=len(cleaned_codecs) > 0,
    )


def classify_track_codec_key(
    raw_codec: str | None,
    channels: int | None = None,
) -> str | None:
    """Map a normalized or raw audio codec + channel count into a playback codec key.

    Explicitly separates `aac_51` (multichannel AAC > 2ch) from `aac_20` (stereo/mono AAC)
    and `dts_hd_ma` from lossy `dts`.
    """
    if not raw_codec or not isinstance(raw_codec, str) or not raw_codec.strip():
        return None
    lower = raw_codec.strip().lower()

    if "e-ac-3" in lower or "eac3" in lower or "eac-3" in lower:
        return "eac3"
    if "ac-3" in lower or "ac3" in lower:
        return "ac3"
    if "truehd" in lower:
        return "truehd"
    if "dts-hd" in lower or "dts_hd" in lower or "dtshd" in lower:
        if "high resolution" in lower or "hra" in lower:
            return "dts"
        return "dts_hd_ma"
    if "dts" in lower or "dca" in lower:
        return "dts"
    if "aac" in lower:
        if channels is not None and channels > AppConstants.CHANNELS_STEREO:
            return "aac_51"
        return "aac_20"
    if "opus" in lower:
        return "opus"
    if "flac" in lower:
        return "flac"
    if "alac" in lower:
        return "alac"
    if "pcm" in lower:
        return "pcm"
    if "mp3" in lower or "layer 3" in lower:
        return "mp3"
    return None


def _extract_track_codec_and_channels(track: Any) -> tuple[str | None, int | None]:
    """Safely read `(codec, channels)` from either a dict or AudioTrackSummary."""
    if isinstance(track, Mapping):
        raw_codec = track.get("codec")
        raw_ch = track.get("channels")
        if raw_ch is None:
            raw_ch = track.get("audio_channels")
    else:
        raw_codec = getattr(track, "codec", None)
        raw_ch = getattr(track, "channels", None)
        if raw_ch is None:
            raw_ch = getattr(track, "audio_channels", None)

    parsed_ch: int | None = None
    if raw_ch is not None:
        try:
            val = int(raw_ch)
            if val > 0:
                parsed_ch = val
        except (TypeError, ValueError):
            parsed_ch = None
    return (str(raw_codec) if raw_codec is not None else None), parsed_ch


def is_track_compatible(
    raw_codec: str | None,
    channels: int | None,
    profile: PlaybackProfileSpec,
) -> bool:
    """Return True if a single audio track is natively playable on `profile`."""
    if not profile.configured:
        return True
    codec_key = classify_track_codec_key(raw_codec, channels)
    if codec_key is None or codec_key not in profile.supported_codecs:
        return False
    if channels is not None and channels > profile.max_channels:
        return False
    return True


def evaluate_file_needs_conversion(
    audio_tracks: Sequence[Any] | None,
    profile: PlaybackProfileSpec,
    *,
    scan_status: str = AppConstants.FILE_SCAN_STATUS_READY,
) -> bool:
    """Return True if the file requires audio conversion for the active PlaybackProfile.

    Anti-stereo-commentary false-negative rule:
    - When `profile.max_channels > 2` (5.1 or 7.1 systems) and the file contains at
      least one multichannel (`> 2ch`) audio track, the file is only considered
      compatible if **at least one multichannel track** is compatible — preventing a
      2.0 stereo commentary track from masking an incompatible 5.1/7.1 main track.
    - When the file has only stereo/mono tracks, OR when the user's profile is Stereo
      (`max_channels <= 2`), at least one compatible audio track satisfies compatibility.
    """
    if not profile.configured:
        return False
    if scan_status != AppConstants.FILE_SCAN_STATUS_READY:
        return False
    if not audio_tracks:
        return False

    parsed_tracks: list[tuple[str | None, int | None]] = [
        _extract_track_codec_and_channels(t) for t in audio_tracks
    ]
    valid_tracks = [
        (codec, ch) for codec, ch in parsed_tracks if codec and codec.strip()
    ]
    if not valid_tracks:
        return False

    multichannel_tracks = [
        (codec, ch)
        for codec, ch in valid_tracks
        if ch is not None and ch > AppConstants.CHANNELS_STEREO
    ]

    if profile.max_channels > AppConstants.CHANNELS_STEREO and multichannel_tracks:
        has_compatible_surround = any(
            is_track_compatible(codec, ch, profile)
            for codec, ch in multichannel_tracks
        )
        return not has_compatible_surround

    has_any_compatible = any(
        is_track_compatible(codec, ch, profile) for codec, ch in valid_tracks
    )
    return not has_any_compatible
