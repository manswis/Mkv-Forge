"""Unit tests for UserSettingsService and video dynamic range (HDR/DV/HLG/SDR) classification."""

from __future__ import annotations

import pytest

from app.config import AppConstants
from app.db.connection import DatabaseManager
from app.db.repositories import AppStateRepository
from app.models.schemas import UserSettingsUpdate
from app.services.probe_parser import parse_mkvmerge_json
from app.services.user_settings import UserSettingsService


class TestUserSettingsService:
    """Verify defaults, persistence, partial merges, and corrupt-data safety."""

    def test_defaults_and_round_trip_update(self, db_manager: DatabaseManager) -> None:
        state_repo = AppStateRepository(db_manager)
        service = UserSettingsService(state_repo)

        initial = service.get()
        assert initial.show_video_details is True

        updated = service.update(UserSettingsUpdate(show_video_details=False))
        assert updated.show_video_details is False

        # Re-read through a fresh service instance to verify SQLite persistence
        assert UserSettingsService(state_repo).get().show_video_details is False

        # Empty partial patch preserves current persisted values
        unchanged = service.update(UserSettingsUpdate())
        assert unchanged.show_video_details is False

    @pytest.mark.parametrize(
        "bad_payload",
        [
            "{not valid json",
            '["array", "not", "dict"]',
            '{"show_video_details": "not-a-bool-list", "show_video_details": []}',
            '{"show_video_details": {"nested": 1}}',
        ],
    )
    def test_corrupt_or_invalid_app_state_degrades_safely_to_defaults(
        self, db_manager: DatabaseManager, bad_payload: str
    ) -> None:
        state_repo = AppStateRepository(db_manager)
        state_repo.set(AppConstants.STATE_KEY_USER_SETTINGS, bad_payload)

        result = UserSettingsService(state_repo).get()
        assert result.show_video_details is True

    def test_unknown_legacy_keys_in_stored_json_are_ignored(
        self, db_manager: DatabaseManager
    ) -> None:
        state_repo = AppStateRepository(db_manager)
        state_repo.set(
            AppConstants.STATE_KEY_USER_SETTINGS,
            '{"show_video_details": false, "removed_future_flag": 42}',
        )

        result = UserSettingsService(state_repo).get()
        assert result.show_video_details is False


class TestVideoDynamicRangeClassification:
    """Verify HDR10, HLG, DV, DV HDR, HDR, SDR, and audio-only classification."""

    @staticmethod
    def _probe_video(
        codec: str = "HEVC/H.265/MPEG-H",
        codec_id: str = "V_MPEGH/ISO/HEVC",
        extra_props: dict | None = None,
    ) -> str | None:
        props = {
            "codec_id": codec_id,
            "pixel_dimensions": "3840x2160",
            **(extra_props or {}),
        }
        payload = {
            "container": {"properties": {"duration": 1_000_000_000}},
            "tracks": [
                {"id": 0, "type": "video", "codec": codec, "properties": props},
            ],
        }
        return parse_mkvmerge_json(payload).video_dynamic_range

    def test_hdr10_via_pq_transfer_characteristics(self) -> None:
        assert (
            self._probe_video(extra_props={"color_transfer_characteristics": 16})
            == "HDR10"
        )

    def test_hlg_via_transfer_characteristics(self) -> None:
        assert (
            self._probe_video(extra_props={"color_transfer_characteristics": 18})
            == "HLG"
        )

    def test_dolby_vision_only_and_dolby_vision_with_hdr10(self) -> None:
        dv_only = self._probe_video(
            extra_props={
                "block_addition_mappings": [{"type": 1685480259, "name": "dvcC"}]
            }
        )
        assert dv_only == "DV"

        dv_hdr = self._probe_video(
            extra_props={
                "color_transfer_characteristics": 16,
                "block_addition_mappings": [{"type": 1685485123, "name": "dvvC"}],
            }
        )
        assert dv_hdr == "DV HDR"

    def test_hdr_via_mastering_metadata_or_bt2020_primaries(self) -> None:
        assert self._probe_video(extra_props={"color_max_cll": 1000}) == "HDR"
        assert self._probe_video(extra_props={"color_primaries": 9}) == "HDR"

    def test_sdr_for_bt709_and_omitted_color_properties(self) -> None:
        assert (
            self._probe_video(
                codec="AVC/H.264/MPEG-4p10",
                codec_id="V_MPEG4/ISO/AVC",
                extra_props={"color_transfer_characteristics": 1},
            )
            == "SDR"
        )
        assert (
            self._probe_video(
                codec="AVC/H.264/MPEG-4p10",
                codec_id="V_MPEG4/ISO/AVC",
                extra_props={},
            )
            == "SDR"
        )

    def test_audio_only_file_has_no_dynamic_range(self) -> None:
        payload = {
            "container": {"properties": {"duration": 1_000_000_000}},
            "tracks": [
                {
                    "id": 0,
                    "type": "audio",
                    "codec": "E-AC-3",
                    "properties": {"audio_channels": 6},
                }
            ],
        }
        probe = parse_mkvmerge_json(payload)
        assert probe.video_codec is None
        assert probe.video_dynamic_range is None
