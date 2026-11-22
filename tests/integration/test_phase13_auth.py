"""Exhaustive unit and integration tests for Phase 13 (Local Accounts, Onboarding, Hybrid Settings & Job Attribution)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.config import AppConstants
from app.services.auth_service import (
    AuthValidationError,
    hash_password,
    hash_session_token,
    verify_password,
)


class TestPasswordHashingAndSessionSecurity:
    """Verify stdlib scrypt hashing, constant-time verification, and edge cases."""

    def test_scrypt_hash_and_verify_round_trip(self) -> None:
        hashed = hash_password("super-secret-pass")
        assert hashed.startswith("scrypt$")
        assert "super-secret-pass" not in hashed
        assert verify_password("super-secret-pass", hashed) is True
        assert verify_password("wrong-password", hashed) is False
        assert verify_password("", hashed) is False
        assert verify_password("super-secret-pass", "") is False
        assert verify_password("super-secret-pass", "malformed$hash") is False
        assert verify_password("super-secret-pass", "pbkdf2$1$1$1$aa$bb") is False

        # Two hashes of the same password must use distinct random salts
        hashed_2 = hash_password("super-secret-pass")
        assert hashed != hashed_2

        with pytest.raises(AuthValidationError):
            hash_password("")


class TestPhase13AuthOnboardingAndRouteProtection:
    """Verify onboarding flows, HTTP vs HTTPS cookies, route guards, user CRUD, hybrid settings, and job attribution."""

    async def test_first_run_anonymous_setup_and_upgrade_to_local(
        self, api_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        # Fresh instance starts unconfigured -> needs_setup = True, resolved user = Anonymous
        status_0 = await api_client.get("/api/auth/status")
        assert status_0.status_code == AppConstants.HTTP_OK
        body_0 = status_0.json()
        assert body_0["auth_mode"] == "unconfigured"
        assert body_0["authenticated"] is True
        assert body_0["needs_setup"] is True
        assert body_0["user"]["display_name"] == "Anonymous"
        assert body_0["user"]["is_anonymous"] is True

        # Protected APIs still accessible in unconfigured/anonymous mode
        assert (await api_client.get("/api/dashboard")).status_code == AppConstants.HTTP_OK

        # Choose "Continue Without Sign-In"
        anon_resp = await api_client.post("/api/auth/setup", json={"mode": "anonymous"})
        assert anon_resp.status_code == AppConstants.HTTP_OK
        anon_body = anon_resp.json()
        assert anon_body["auth_mode"] == "anonymous"
        assert anon_body["needs_setup"] is False
        assert anon_body["user"]["is_anonymous"] is True

        # Upgrade from anonymous -> local account
        local_resp = await api_client.post(
            "/api/auth/setup",
            json={
                "mode": "local",
                "username": "Manoj",
                "display_name": "Manoj Biswas",
                "password": "my-strong-password",
            },
        )
        assert local_resp.status_code == AppConstants.HTTP_OK
        local_body = local_resp.json()
        assert local_body["auth_mode"] == "local"
        assert local_body["authenticated"] is True
        assert local_body["needs_setup"] is False
        assert local_body["user"]["username"] == "manoj"
        assert local_body["user"]["display_name"] == "Manoj Biswas"

        # Cookie MUST NOT have Secure flag over plain HTTP (internal NPM reverse proxy guardrail)
        set_cookie = local_resp.headers.get("set-cookie", "")
        assert AppConstants.AUTH_SESSION_COOKIE_NAME in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" not in set_cookie

        # Re-running setup when already in local mode -> 409 Conflict
        dup_setup = await api_client.post(
            "/api/auth/setup",
            json={
                "mode": "local",
                "username": "other",
                "password": "another-password",
            },
        )
        assert dup_setup.status_code == AppConstants.HTTP_CONFLICT

        # Zero PII in logs (username, display_name, password must never appear)
        log_blob = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "my-strong-password" not in log_blob
        assert "Manoj Biswas" not in log_blob

    async def test_route_protection_login_logout_https_cookie_and_expired_session(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        setup_resp = await api_client.post(
            "/api/auth/setup",
            json={
                "mode": "local",
                "username": "alice",
                "display_name": "Alice",
                "password": "alice-password-1",
            },
        )
        assert setup_resp.status_code == AppConstants.HTTP_OK

        # Logout revokes session and clears cookie
        logout_resp = await api_client.post("/api/auth/logout")
        assert logout_resp.status_code == AppConstants.HTTP_OK
        assert logout_resp.json()["authenticated"] is False

        # Unauthenticated requests to protected APIs return 401
        assert (await api_client.get("/api/dashboard")).status_code == AppConstants.HTTP_UNAUTHORIZED
        assert (await api_client.get("/api/files")).status_code == AppConstants.HTTP_UNAUTHORIZED
        assert (await api_client.get("/api/jobs")).status_code == AppConstants.HTTP_UNAUTHORIZED
        assert (await api_client.get("/api/events/stream")).status_code == AppConstants.HTTP_UNAUTHORIZED

        # Public allowlist is never blocked by 401 (health returns 200 or 503 if tools absent)
        health_status = (await api_client.get("/api/health")).status_code
        assert health_status in (AppConstants.HTTP_OK, AppConstants.HTTP_SERVICE_UNAVAILABLE)
        assert (await api_client.get("/api/auth/status")).status_code == AppConstants.HTTP_OK
        assert (await api_client.get("/")).status_code == AppConstants.HTTP_OK

        # Tampered cookie is rejected with 401
        api_client.cookies.set(AppConstants.AUTH_SESSION_COOKIE_NAME, "forged-token-123")
        assert (await api_client.get("/api/dashboard")).status_code == AppConstants.HTTP_UNAUTHORIZED
        api_client.cookies.clear()

        # Wrong password rejected with 401
        bad_login = await api_client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "wrong-password"},
        )
        assert bad_login.status_code == AppConstants.HTTP_UNAUTHORIZED

        # Valid login over HTTPS (via X-Forwarded-Proto: https) sets Secure cookie
        https_login = await api_client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "alice-password-1"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert https_login.status_code == AppConstants.HTTP_OK
        assert "Secure" in https_login.headers.get("set-cookie", "")

        # Valid login over internal HTTP (no Secure flag) lets HTTP client send cookie back
        api_client.cookies.clear()
        good_login = await api_client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "alice-password-1"},
        )
        assert good_login.status_code == AppConstants.HTTP_OK
        assert "Secure" not in good_login.headers.get("set-cookie", "")
        assert (await api_client.get("/api/dashboard")).status_code == AppConstants.HTTP_OK

        # Expired session is rejected and purged
        raw_cookie = api_client.cookies.get(AppConstants.AUTH_SESSION_COOKIE_NAME)
        assert raw_cookie is not None
        past_iso = (
            (datetime.now(timezone.utc) - timedelta(hours=2))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        api_app.state.auth_repo.touch_session(hash_session_token(raw_cookie), past_iso)
        assert (await api_client.get("/api/dashboard")).status_code == AppConstants.HTTP_UNAUTHORIZED

    async def test_peer_users_last_user_guard_hybrid_settings_and_job_attribution(
        self, api_app
    ) -> None:
        transport = httpx.ASGITransport(app=api_app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_a,
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_b,
        ):
            # User A initializes local auth
            setup = await client_a.post(
                "/api/auth/setup",
                json={
                    "mode": "local",
                    "username": "manoj",
                    "display_name": "Manoj",
                    "password": "password-one",
                },
            )
            assert setup.status_code == AppConstants.HTTP_OK
            user_a_id = setup.json()["user"]["id"]

            # Last remaining local user cannot be deleted -> 409 Conflict
            del_last = await client_a.delete(f"/api/auth/users/{user_a_id}")
            assert del_last.status_code == AppConstants.HTTP_CONFLICT

            # Add peer user B
            add_b = await client_a.post(
                "/api/auth/users",
                json={
                    "username": "alex",
                    "display_name": "Alex",
                    "password": "password-two",
                },
            )
            assert add_b.status_code == AppConstants.HTTP_CREATED
            user_b_id = add_b.json()["id"]

            # User B signs in
            login_b = await client_b.post(
                "/api/auth/login",
                json={"username": "alex", "password": "password-two"},
            )
            assert login_b.status_code == AppConstants.HTTP_OK

            # Hybrid settings isolation:
            # User A changes per-user settings (theme_mode, font_scale, confirm_cancel_job)
            # AND system setting (job_history_retention_days)
            patch_a = await client_a.patch(
                "/api/settings",
                json={
                    "theme_mode": "light",
                    "font_scale": 115,
                    "confirm_cancel_job": False,
                    "job_history_retention_days": 90,
                },
            )
            assert patch_a.status_code == AppConstants.HTTP_OK
            assert patch_a.json()["theme_mode"] == "light"
            assert patch_a.json()["font_scale"] == 115
            assert patch_a.json()["confirm_cancel_job"] is False
            assert patch_a.json()["job_history_retention_days"] == 90

            # User B MUST keep their own per-user defaults, while seeing the system-wide retention change!
            settings_b = (await client_b.get("/api/settings")).json()
            assert settings_b["theme_mode"] == "dark"
            assert settings_b["font_scale"] == 100
            assert settings_b["confirm_cancel_job"] is True
            assert settings_b["job_history_retention_days"] == 90

            # Job attribution: seed a file and enqueue a job as User B
            storage_dir: Path = api_app.state.settings.storage_dir
            (storage_dir / "movies").mkdir(parents=True, exist_ok=True)
            (storage_dir / "movies" / "Test.mkv").write_bytes(b"fake-mkv")
            enq = await client_b.post(
                "/api/jobs",
                json={
                    "filepath": "movies/Test.mkv",
                    "config": {
                        "output_mode": "keep_both",
                        "tracks": [
                            {"id": 0, "type": "video", "enabled": True}
                        ],
                    },
                },
            )
            assert enq.status_code == AppConstants.HTTP_ACCEPTED
            assert enq.json()["created_by_name"] == "Alex"

            # Delete User B -> job attribution survives user deletion
            del_b = await client_a.delete(f"/api/auth/users/{user_b_id}")
            assert del_b.status_code == AppConstants.HTTP_OK

            dash_a = (await client_a.get("/api/dashboard")).json()
            assert any(
                item["created_by_name"] == "Alex"
                for item in dash_a["recent_activity"]
            )

            # Change password & switch back to anonymous mode (requires valid password)
            pw_resp = await client_a.patch(
                "/api/auth/password",
                json={
                    "current_password": "password-one",
                    "new_password": "password-new-1",
                },
            )
            assert pw_resp.status_code == AppConstants.HTTP_OK

            bad_disable = await client_a.post(
                "/api/auth/disable", json={"password": "password-one"}
            )
            assert bad_disable.status_code == AppConstants.HTTP_UNAUTHORIZED

            good_disable = await client_a.post(
                "/api/auth/disable", json={"password": "password-new-1"}
            )
            assert good_disable.status_code == AppConstants.HTTP_OK
            assert good_disable.json()["auth_mode"] == "anonymous"
            assert good_disable.json()["user"]["is_anonymous"] is True

            # Unauthenticated client can now access dashboard again
            client_b.cookies.clear()
            assert (await client_b.get("/api/dashboard")).status_code == AppConstants.HTTP_OK

    async def test_api_key_lifecycle_and_middleware_authentication(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Verify API key generation, header authentication (X-API-Key and Bearer), and revocation."""
        # 1. Onboard local admin
        setup_res = await api_client.post(
            "/api/auth/setup",
            json={
                "mode": "local",
                "username": "keyuser",
                "display_name": "Key User",
                "password": "password-admin-1",
            },
        )
        assert setup_res.status_code == AppConstants.HTTP_OK

        # 2. Raw unauthenticated request without key or cookie -> 401
        async with httpx.AsyncClient(
            transport=api_client._transport, base_url="http://testserver"
        ) as anon_client:
            unauth_res = await anon_client.get("/api/dashboard")
            assert unauth_res.status_code == AppConstants.HTTP_UNAUTHORIZED
            assert unauth_res.json()["detail"] == "Authentication required"

            # 3. Create API key using authenticated session
            create_res = await api_client.post(
                "/api/auth/api-keys",
                json={"name": "Homepage Widget"},
            )
            assert create_res.status_code == AppConstants.HTTP_OK
            key_data = create_res.json()
            raw_key = key_data["key"]
            key_id = key_data["id"]
            assert raw_key.startswith(AppConstants.AUTH_API_KEY_PREFIX)
            assert "••••" in key_data["key_prefix"]

            # 4. List keys
            list_res = await api_client.get("/api/auth/api-keys")
            assert list_res.status_code == AppConstants.HTTP_OK
            keys = list_res.json()["api_keys"]
            assert len(keys) == 1
            assert keys[0]["id"] == key_id
            assert keys[0]["name"] == "Homepage Widget"

            # 5. Access with X-API-Key header
            x_res = await anon_client.get(
                "/api/dashboard",
                headers={AppConstants.AUTH_API_KEY_HEADER: raw_key},
            )
            assert x_res.status_code == AppConstants.HTTP_OK
            dashboard_data = x_res.json()
            assert "stats" in dashboard_data
            assert "analytics" in dashboard_data
            assert "top_codec_label" in dashboard_data["analytics"]

            # 6. Access with Authorization: Bearer header
            bearer_res = await anon_client.get(
                "/api/dashboard",
                headers={"authorization": f"Bearer {raw_key}"},
            )
            assert bearer_res.status_code == AppConstants.HTTP_OK

            # 7. Access with invalid API key
            invalid_res = await anon_client.get(
                "/api/dashboard",
                headers={AppConstants.AUTH_API_KEY_HEADER: "mkv_invalid_token_99999"},
            )
            assert invalid_res.status_code == AppConstants.HTTP_UNAUTHORIZED
            assert invalid_res.json()["detail"] == "Invalid or revoked API key"

            # 8. Delete API key
            del_res = await api_client.delete(f"/api/auth/api-keys/{key_id}")
            assert del_res.status_code == AppConstants.HTTP_OK
            assert del_res.json()["ok"] is True

            # 9. Access with revoked key -> 401
            revoked_res = await anon_client.get(
                "/api/dashboard",
                headers={AppConstants.AUTH_API_KEY_HEADER: raw_key},
            )
            assert revoked_res.status_code == AppConstants.HTTP_UNAUTHORIZED
            assert revoked_res.json()["detail"] == "Invalid or revoked API key"

    async def test_anonymous_mode_allows_api_without_key_and_gates_key_management(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """When authentication is disabled, APIs work without a key and key CRUD requires local auth."""
        setup = await api_client.post("/api/auth/setup", json={"mode": "anonymous"})
        assert setup.status_code == AppConstants.HTTP_OK
        assert setup.json()["auth_mode"] == AppConstants.AUTH_MODE_ANONYMOUS

        # Dashboard API works without any API key in Anonymous mode
        dash_res = await api_client.get("/api/dashboard")
        assert dash_res.status_code == AppConstants.HTTP_OK

        # Key management endpoints require local authentication
        list_res = await api_client.get("/api/auth/api-keys")
        assert list_res.status_code == AppConstants.HTTP_UNAUTHORIZED
        create_res = await api_client.post(
            "/api/auth/api-keys", json={"name": "Homepage"}
        )
        assert create_res.status_code == AppConstants.HTTP_UNAUTHORIZED

    async def test_phase13_ui_landmarks_present(
        self, api_client: httpx.AsyncClient
    ) -> None:
        html = (await api_client.get("/")).text
        assert 'aria-label="Welcome to MKV Forge"' in html
        assert "onboarding-wizard-shell" in html
        assert "onboardingNextFromAccess()" in html
        assert "onboardingContinueAnonymous()" in html
        assert "Continue Without Sign-In" in html
        assert 'id="onboard-username"' in html
        assert 'id="onboard-password"' in html
        assert 'id="onboard-confirm-password"' in html
        assert "togglePasswordVisible('onboard_pw')" in html
        assert "togglePasswordVisible('onboard_confirm')" in html
        assert "togglePasswordVisible('login_pw')" in html
        assert 'aria-label="Sign in to MKV Forge"' in html
        assert 'autocomplete="current-password"' in html
        assert "toggleMobileSettingsSection(C.SETTINGS_SECTIONS.ACCOUNT)" in html
        assert "Running without authentication (Anonymous)" in html
        assert "Switch to No-Auth Mode" in html
        assert "API Keys &amp; Integrations" in html
        assert "createdApiKeyModal !== null" in html
        assert "logoutConfirmOpen" in html
        assert "currentUserDisplayName" in html
        assert "job.created_by_name || 'Anonymous'" in html
        assert "`by ${job.created_by_name" not in html
        assert "Your session expired" in html

    def test_frontend_global_401_recovery_opens_login_and_suppresses_raw_error(self) -> None:
        """Verify 401 on any protected API call transitions cleanly to Sign-In or recovers silently."""
        import json
        import subprocess

        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        let authStatusPayload = {{ auth_mode: "local", authenticated: true, needs_setup: false, user: {{ id: 1, username: "manoj", display_name: "Manoj", is_anonymous: false }} }};
        let failDashboard401 = false;
        let sseOpen = 0;
        const ctx = {{
          console, setTimeout: (fn) => {{ fn(); return 1; }}, clearTimeout: () => {{}},
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{
            sseOpen++;
            this.addEventListener = () => {{}};
            this.close = () => {{ sseOpen--; }};
          }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/dashboard", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "complete", addEventListener: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 390, innerHeight: 844,
          fetch: async (url) => {{
            const s = String(url);
            if (s.includes("/api/auth/status")) {{
              return {{ ok: true, status: 200, json: async () => authStatusPayload }};
            }}
            if (s.includes("/api/dashboard") && failDashboard401) {{
              return {{ ok: false, status: 401, json: async () => ({{ detail: "Authentication required" }}) }};
            }}
            return {{ ok: true, status: 200, json: async () => ({{ stats: {{ total_files: 0 }}, recent_activity: [], active: null, queued: [], history: [] }}) }};
          }},
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "pull_refresh.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        (async () => {{
          const app = ctx.mkvForgeApp();
          app.init();
          await new Promise((r) => setImmediate(r));

          // Simulate overnight session expiry -> next API call returns 401
          failDashboard401 = true;
          authStatusPayload = {{ auth_mode: "local", authenticated: false, needs_setup: false, user: null }};
          await app.loadDashboard();
          await new Promise((r) => setImmediate(r));

          console.log(JSON.stringify({{
            isLoginRequired: app.isLoginRequired,
            authExpiredNotice: app.authExpiredNotice,
            dashboardError: app.dashboardError,
            sseOpenAfter401: sseOpen,
          }}));
          process.exit(0);
        }})();
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True
        )
        out = json.loads(proc.stdout)
        assert out == {
            "isLoginRequired": True,
            "authExpiredNotice": True,
            "dashboardError": None,
            "sseOpenAfter401": 0,
        }

    def test_frontend_anonymous_mode_never_prompts_login_on_resume_or_idle(self) -> None:
        """Verify when auth is disabled (anonymous mode), init & overnight resume stay frictionless."""
        import json
        import subprocess

        js_dir = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
        script = f"""
        const fs = require("fs");
        const vm = require("vm");
        let authStatusCalls = 0;
        let sseOpen = 0;
        const ctx = {{
          console, setTimeout: (fn) => {{ fn(); return 1; }}, clearTimeout: () => {{}},
          setInterval: () => 1, clearInterval: () => {{}},
          URL, URLSearchParams, AbortController,
          EventSource: function() {{
            sseOpen++;
            this.addEventListener = () => {{}};
            this.close = () => {{ sseOpen--; }};
          }},
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          location: {{ hash: "#/dashboard", origin: "http://localhost" }},
          history: {{ replaceState: () => {{}} }},
          document: {{ visibilityState: "visible", readyState: "complete", addEventListener: () => {{}} }},
          addEventListener: () => {{}},
          innerWidth: 390, innerHeight: 844,
          fetch: async (url) => {{
            const s = String(url);
            if (s.includes("/api/auth/status")) {{
              authStatusCalls++;
              return {{
                ok: true, status: 200,
                json: async () => ({{
                  auth_mode: "anonymous",
                  authenticated: true,
                  needs_setup: false,
                  user: {{ id: 1, username: "anonymous", display_name: "Anonymous", is_anonymous: true }},
                }}),
              }};
            }}
            return {{ ok: true, status: 200, json: async () => ({{ stats: {{ total_files: 2 }}, recent_activity: [], active: null, queued: [], history: [] }}) }};
          }},
        }};
        ctx.window = ctx;
        vm.createContext(ctx);
        for (const f of ["constants.js", "formatters.js", "api.js", "folder_tree.js", "editor.js", "pull_refresh.js", "toast_stack.js", "app.js"]) {{
          vm.runInContext(fs.readFileSync({json.dumps(str(js_dir))} + "/" + f, "utf8"), ctx, {{ filename: f }});
        }}
        (async () => {{
          const app = ctx.mkvForgeApp();
          app.init();
          await new Promise((r) => setImmediate(r));

          const callsAfterInit = authStatusCalls;
          // Simulate mobile tab hidden overnight -> resumed
          ctx.document.visibilityState = "hidden";
          app.handleAppHidden();
          ctx.document.visibilityState = "visible";
          app.handleAppResume({{ forceSse: true }});
          await new Promise((r) => setImmediate(r));

          console.log(JSON.stringify({{
            authMode: app.authMode,
            isLoginRequired: app.isLoginRequired,
            isOnboardingRequired: app.isOnboardingRequired,
            authExpiredNotice: app.authExpiredNotice,
            currentUserDisplayName: app.currentUserDisplayName,
            callsAfterInit,
            callsAfterResume: authStatusCalls,
            sseOpenAfterResume: sseOpen,
          }}));
          process.exit(0);
        }})();
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True
        )
        out = json.loads(proc.stdout)
        assert out == {
            "authMode": "anonymous",
            "isLoginRequired": False,
            "isOnboardingRequired": False,
            "authExpiredNotice": False,
            "currentUserDisplayName": "Anonymous",
            "callsAfterInit": 1,
            "callsAfterResume": 1,  # No redundant /api/auth/status call on resume in anonymous mode
            "sseOpenAfterResume": 1,
        }
