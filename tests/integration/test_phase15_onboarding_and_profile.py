"""Integration tests for Phase 15 Global Playback Profile, Needs Conversion API, and Onboarding."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.config import AppConstants, Settings
from app.db.repositories import MediaFileUpsert, utc_now_iso


def _seed_file(
    api_app,
    test_settings: Settings,
    filename: str,
    audio_summary: list[dict],
) -> int:
    rel = f"movies/{filename}"
    target = test_settings.storage_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"synthetic-mkv")
    has_aac_51 = any(
        a["codec"] == "AAC" and (a.get("channels") or 0) >= 6 for a in audio_summary
    )
    has_ac3_eac3 = any(a["codec"] in ("AC3", "EAC3") for a in audio_summary)
    return api_app.state.media_repo.upsert(
        MediaFileUpsert(
            filepath=rel,
            folder="/movies",
            filename=filename,
            size_bytes=target.stat().st_size,
            mtime=1700000000.0,
            duration_us=1_000_000,
            resolution="1080p",
            video_codec="HEVC",
            audio_summary_json=json.dumps(audio_summary),
            subtitle_count=0,
            container_title="",
            has_global_tags=False,
            has_aac_51=has_aac_51,
            has_ac3_eac3=has_ac3_eac3,
            raw_json="{}",
            scanned_at=utc_now_iso(),
        )
    )


class TestPhase15OnboardingAndGlobalPlaybackProfile:
    """Verify 3-step onboarding APIs, global household profile scoping, and zero-rescan compatibility."""

    async def test_auth_status_reports_storage_mount_and_respects_existing_installs(
        self, api_client: httpx.AsyncClient, test_settings: Settings
    ) -> None:
        # Fresh install -> needs_setup=True + storage path/writable metadata for Step 3
        st_fresh = (await api_client.get("/api/auth/status")).json()
        assert st_fresh["auth_mode"] == AppConstants.AUTH_MODE_UNCONFIGURED
        assert st_fresh["needs_setup"] is True
        assert st_fresh["storage_path"] == str(test_settings.storage_dir)
        assert st_fresh["storage_writable"] is True

        # Completing setup transitions cleanly out of unconfigured -> existing installs never see wizard
        setup_resp = await api_client.post(
            "/api/auth/setup", json={"mode": "anonymous"}
        )
        assert setup_resp.status_code == AppConstants.HTTP_OK
        st_after = (await api_client.get("/api/auth/status")).json()
        assert st_after["auth_mode"] == AppConstants.AUTH_MODE_ANONYMOUS
        assert st_after["needs_setup"] is False

    async def test_playback_profile_is_global_across_users_while_ui_prefs_stay_per_user(
        self, api_client: httpx.AsyncClient
    ) -> None:
        # User 1 (alice) sets up local auth
        r1 = await api_client.post(
            "/api/auth/setup",
            json={
                "mode": "local",
                "username": "alice",
                "display_name": "Alice",
                "password": "password123",
            },
        )
        assert r1.status_code == AppConstants.HTTP_OK

        # Alice creates peer user bob
        r_bob = await api_client.post(
            "/api/auth/users",
            json={
                "username": "bob",
                "display_name": "Bob",
                "password": "password456",
            },
        )
        assert r_bob.status_code == AppConstants.HTTP_CREATED

        # Alice configures global Optical Playback Profile + her own light theme
        patch_alice = await api_client.patch(
            "/api/settings",
            json={
                "playback_profile_preset": "optical_legacy",
                "theme_mode": "light",
            },
        )
        assert patch_alice.status_code == AppConstants.HTTP_OK
        alice_cfg = patch_alice.json()
        assert alice_cfg["playback_profile_preset"] == "optical_legacy"
        assert alice_cfg["playback_preferred_codec"] == "ac3"
        assert alice_cfg["theme_mode"] == "light"

        # Switch session to Bob -> sees Alice's GLOBAL playback profile, but keeps Bob's own dark theme!
        await api_client.post("/api/auth/logout")
        login_bob = await api_client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "password456"},
        )
        assert login_bob.status_code == AppConstants.HTTP_OK
        bob_cfg = (await api_client.get("/api/settings")).json()
        assert bob_cfg["playback_profile_preset"] == "optical_legacy"
        assert bob_cfg["playback_supported_codecs"] == ["ac3", "aac_20"]
        assert bob_cfg["playback_max_channels"] == 6
        assert bob_cfg["theme_mode"] == "dark"  # Per-user UI pref isolated!

    async def test_dashboard_and_library_recalculate_needs_conversion_without_rescan(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        await api_client.post("/api/auth/setup", json={"mode": "anonymous"})

        # Seed 5 representative library files including the stereo-commentary false-negative trap!
        _seed_file(
            api_app,
            test_settings,
            "A_Aac51WithCommentary.mkv",
            [
                {"codec": "AAC", "channels": 6, "channel_layout": "5.1", "language": "eng"},
                {"codec": "AAC", "channels": 2, "channel_layout": "2.0", "language": "eng"},
            ],
        )
        _seed_file(
            api_app,
            test_settings,
            "B_Eac3Only.mkv",
            [{"codec": "EAC3", "channels": 6, "channel_layout": "5.1", "language": "eng"}],
        )
        _seed_file(
            api_app,
            test_settings,
            "C_Ac3Only.mkv",
            [{"codec": "AC3", "channels": 6, "channel_layout": "5.1", "language": "eng"}],
        )
        _seed_file(
            api_app,
            test_settings,
            "D_TrueHd71.mkv",
            [{"codec": "TrueHD", "channels": 8, "channel_layout": "7.1", "language": "eng"}],
        )
        _seed_file(
            api_app,
            test_settings,
            "E_StereoOnly.mkv",
            [{"codec": "AAC", "channels": 2, "channel_layout": "2.0", "language": "eng"}],
        )

        # 1. Neutral (unconfigured) -> 0 flagged
        d_neutral = (await api_client.get("/api/dashboard")).json()["stats"]
        assert d_neutral["playback_profile_configured"] is False
        assert d_neutral["needs_conversion"] == 0

        # 2. Switch to HDMI ARC (5.1) -> A (commentary trap caught!) + D (TrueHD 7.1) = 2
        await api_client.patch(
            "/api/settings", json={"playback_profile_preset": "dolby_arc"}
        )
        d_arc = (await api_client.get("/api/dashboard")).json()["stats"]
        assert d_arc["playback_profile_configured"] is True
        assert d_arc["playback_profile_label"] == "HDMI ARC (5.1)"
        assert d_arc["needs_conversion"] == 2

        f_arc = (await api_client.get("/api/files?needs_conversion=true")).json()
        assert f_arc["total"] == 2
        assert sorted(item["filename"] for item in f_arc["items"]) == [
            "A_Aac51WithCommentary.mkv",
            "D_TrueHd71.mkv",
        ]
        assert all(item["needs_conversion"] is True for item in f_arc["items"])
        assert f_arc["facets"]["needs_conversion"] == 2

        # 3. Switch to Optical (5.1) -> A + B (E-AC-3!) + D = 3 (ZERO re-scan!)
        await api_client.patch(
            "/api/settings", json={"playback_profile_preset": "optical_legacy"}
        )
        d_opt = (await api_client.get("/api/dashboard")).json()["stats"]
        assert d_opt["needs_conversion"] == 3
        f_opt = (await api_client.get("/api/files?needs_conversion=true")).json()
        assert sorted(item["filename"] for item in f_opt["items"]) == [
            "A_Aac51WithCommentary.mkv",
            "B_Eac3Only.mkv",
            "D_TrueHd71.mkv",
        ]

        # 4. Switch to eARC AVR (7.1) -> only A (TrueHD 7.1 is native!) = 1
        await api_client.patch(
            "/api/settings", json={"playback_profile_preset": "earc_avr"}
        )
        d_earc = (await api_client.get("/api/dashboard")).json()["stats"]
        assert d_earc["needs_conversion"] == 1

        # 5. Switch to Stereo (2.0) -> B, C, D (A has AAC 2.0, E has AAC 2.0) = 3
        await api_client.patch(
            "/api/settings", json={"playback_profile_preset": "stereo_mobile"}
        )
        d_stereo = (await api_client.get("/api/dashboard")).json()["stats"]
        assert d_stereo["needs_conversion"] == 3


class TestPhase15FactoryReset:
    """Exhaustive tests for Factory Reset physical DB rebuild, auth/admin guards, SSE/upload reset, and media safety."""

    async def test_anonymous_factory_reset_guards_rebuilds_db_preserves_media_and_keeps_uploads_dir(
        self, api_client: httpx.AsyncClient, api_app, test_settings: Settings
    ) -> None:
        await api_client.post("/api/auth/setup", json={"mode": "anonymous"})
        await api_client.patch(
            "/api/settings", json={"playback_profile_preset": "optical_legacy"}
        )

        # Seed a real media file + staged upload + in-memory SSE log line
        media_file = test_settings.storage_dir / "movies" / "Precious.mkv"
        media_file.parent.mkdir(parents=True, exist_ok=True)
        media_file.write_bytes(b"precious-movie-bytes-untouched")
        _seed_file(
            api_app,
            test_settings,
            "Precious.mkv",
            [{"codec": "AAC", "channels": 6, "channel_layout": "5.1", "language": "eng"}],
        )
        # Re-write Precious.mkv with exact bytes we want to assert survive untouched
        media_file.write_bytes(b"precious-movie-bytes-untouched")

        test_settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        staged = test_settings.uploads_dir / "staged-sub.srt"
        staged.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

        api_app.state.job_broker.start_job(
            job_id="old-job", filepath="movies/Precious.mkv", stage="remux"
        )
        api_app.state.job_broker.publish_log(
            job_id="old-job", filepath="movies/Precious.mkv", log_line="stale log line"
        )
        assert len(api_app.state.job_broker.recent_logs) == 1

        # Start worker & scheduler to verify Guardrail #1 pauses & resumes them cleanly
        api_app.state.job_worker.start()
        api_app.state.maintenance_service.start_scheduler()

        # 1. Wrong/missing confirm_text rejected with 400
        bad1 = await api_client.post("/api/maintenance/factory-reset", json={})
        assert bad1.status_code == AppConstants.HTTP_BAD_REQUEST
        bad2 = await api_client.post(
            "/api/maintenance/factory-reset", json={"confirm_text": "reset"}
        )
        assert bad2.status_code == AppConstants.HTTP_BAD_REQUEST

        # 2. Busy guard: active job -> 409 Conflict
        api_app.state.job_worker._active_job_id = "busy-job-1"
        try:
            busy_resp = await api_client.post(
                "/api/maintenance/factory-reset", json={"confirm_text": "RESET"}
            )
            assert busy_resp.status_code == AppConstants.HTTP_CONFLICT
        finally:
            api_app.state.job_worker._active_job_id = None

        # 3. Valid RESET succeeds and physically rebuilds DB
        ok = await api_client.post(
            "/api/maintenance/factory-reset", json={"confirm_text": "RESET"}
        )
        assert ok.status_code == AppConstants.HTTP_OK, ok.text
        body = ok.json()
        assert body["status"] == "reset"
        assert body["removed_db_files"] >= 1
        assert body["removed_uploads"] == 1

        # Media file is 100% untouched
        assert media_file.exists()
        assert media_file.read_bytes() == b"precious-movie-bytes-untouched"

        # Guardrail #4: uploads directory stays intact, only its contents are cleared
        assert test_settings.uploads_dir.is_dir()
        assert list(test_settings.uploads_dir.iterdir()) == []

        # Guardrail #3: SSE broker ring buffer & status reset cleanly; library_broker stays open
        snap = api_app.state.job_broker.get_snapshot()
        assert snap.status == AppConstants.JOB_STATUS_IDLE
        assert snap.recent_logs == []
        assert api_app.state.library_broker._closed is False

        # Guardrail #1: background worker & scheduler resumed cleanly
        assert api_app.state.job_worker.is_running is True
        assert api_app.state.maintenance_service._scheduler_task is not None
        assert not api_app.state.maintenance_service._scheduler_task.done()

        # Fresh DB state: back to unconfigured onboarding Step 1 + neutral profile + 0 files
        st = (await api_client.get("/api/auth/status")).json()
        assert st["auth_mode"] == AppConstants.AUTH_MODE_UNCONFIGURED
        assert st["needs_setup"] is True
        dash = (await api_client.get("/api/dashboard")).json()
        assert dash["stats"]["total_files"] == 0
        assert dash["stats"]["playback_profile_preset"] == "neutral"

        await api_app.state.maintenance_service.shutdown_scheduler()

    async def test_local_auth_factory_reset_requires_admin_and_valid_password(
        self, api_client: httpx.AsyncClient, api_app
    ) -> None:
        setup = await api_client.post(
            "/api/auth/setup",
            json={
                "mode": "local",
                "username": "admin1",
                "display_name": "Admin One",
                "password": "StrongPassword1!",
            },
        )
        assert setup.status_code == AppConstants.HTTP_OK

        # 1. Wrong password -> 400 Bad Request (does NOT wipe DB or expire session!)
        wrong_pw = await api_client.post(
            "/api/maintenance/factory-reset",
            json={"password": "WrongPassword!"},
        )
        assert wrong_pw.status_code == AppConstants.HTTP_BAD_REQUEST
        assert (await api_client.get("/api/auth/status")).json()["auth_mode"] == "local"

        # 2. Non-admin role -> 403 Forbidden
        with api_app.state.db.connect() as conn:
            conn.execute("UPDATE users SET role = 'viewer' WHERE username = 'admin1'")
            conn.commit()
        non_admin = await api_client.post(
            "/api/maintenance/factory-reset",
            json={"password": "StrongPassword1!"},
        )
        assert non_admin.status_code == AppConstants.HTTP_FORBIDDEN

        # Restore admin role
        with api_app.state.db.connect() as conn:
            conn.execute("UPDATE users SET role = 'admin' WHERE username = 'admin1'")
            conn.commit()

        # 3. Correct admin password -> 200 OK, clears cookie & returns to unconfigured Step 1
        ok = await api_client.post(
            "/api/maintenance/factory-reset",
            json={"password": "StrongPassword1!"},
        )
        assert ok.status_code == AppConstants.HTTP_OK
        st_after = (await api_client.get("/api/auth/status")).json()
        assert st_after["auth_mode"] == AppConstants.AUTH_MODE_UNCONFIGURED
        assert st_after["needs_setup"] is True

        # 4. Unauthenticated request in local mode -> 401
        await api_client.post(
            "/api/auth/setup",
            json={
                "mode": "local",
                "username": "admin2",
                "display_name": "Admin Two",
                "password": "AnotherPassword2!",
            },
        )
        await api_client.post("/api/auth/logout")
        unauth = await api_client.post(
            "/api/maintenance/factory-reset",
            json={"password": "AnotherPassword2!"},
        )
        assert unauth.status_code == AppConstants.HTTP_UNAUTHORIZED
