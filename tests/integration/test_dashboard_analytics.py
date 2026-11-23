"""Exhaustive integration tests for Dashboard Analytics, Storage Breakdown, Audio Codec Distribution & Filtering."""

from __future__ import annotations

import json
import shutil
from collections import namedtuple

import httpx
import pytest

from app.config import AppConstants
from app.db.repositories import MediaFileUpsert, utc_now_iso

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def _make_record(
    *,
    filepath: str,
    size_bytes: int,
    audio_tracks: list[dict[str, object]],
    resolution: str | None = "2160p",
    video_codec: str | None = "HEVC",
    video_dynamic_range: str | None = "HDR10",
    subtitle_count: int = 1,
    scan_status: str = AppConstants.FILE_SCAN_STATUS_READY,
    has_aac_51: bool = False,
    has_ac3_eac3: bool = True,
    container_title: str = "",
) -> MediaFileUpsert:
    parts = filepath.rsplit("/", 1)
    folder = f"/{parts[0]}" if len(parts) == 2 else "/"
    filename = parts[-1]
    return MediaFileUpsert(
        filepath=filepath,
        folder=folder,
        filename=filename,
        size_bytes=size_bytes,
        mtime=1700000000.0,
        duration_us=7_200_000_000,
        resolution=resolution,
        video_codec=video_codec,
        video_dynamic_range=video_dynamic_range,
        audio_summary_json=json.dumps(audio_tracks),
        subtitle_count=subtitle_count,
        container_title=container_title,
        has_global_tags=False,
        has_aac_51=has_aac_51,
        has_ac3_eac3=has_ac3_eac3,
        raw_json="{}",
        scanned_at=utc_now_iso(),
        scan_status=scan_status,
    )


class TestDashboardAnalyticsAPI:
    """Verify storage breakdown, audio codec track math, resolution/HDR pills, and scanning exclusions."""

    async def test_dashboard_analytics_aggregates_ready_files_and_excludes_scanning(
        self, api_app, api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = api_app.state.media_repo

        # Ready MKV with EAC3 + AAC tracks (2160p DV HDR)
        repo.upsert(
            _make_record(
                filepath="movies/Dune.mkv",
                size_bytes=20 * 1024**3,
                audio_tracks=[
                    {"codec": "EAC3", "channels": 6, "lang": "eng"},
                    {"codec": "AAC", "channels": 2, "lang": "eng"},
                ],
                resolution="2160p",
                video_dynamic_range="DV HDR",
            )
        )
        # Ready MKV with DTS-HD + TrueHD tracks (1080p SDR)
        repo.upsert(
            _make_record(
                filepath="movies/Matrix.mkv",
                size_bytes=10 * 1024**3,
                audio_tracks=[
                    {"codec": "DTS-HD", "channels": 8, "lang": "eng"},
                    {"codec": "TrueHD", "channels": 8, "lang": "eng"},
                ],
                resolution="1080p",
                video_dynamic_range="SDR",
            )
        )
        # Ready MP4 with AC3 + FLAC ("Other") tracks (1080p HDR10)
        repo.upsert(
            _make_record(
                filepath="shows/Pilot.mp4",
                size_bytes=5 * 1024**3,
                audio_tracks=[
                    {"codec": "AC3", "channels": 6, "lang": "eng"},
                    {"codec": "FLAC", "channels": 2, "lang": "jpn"},
                ],
                resolution="1080p",
                video_dynamic_range="HDR10",
            )
        )
        # In-flight 'scanning' row MUST be excluded from codec, resolution, and dynamic range analytics
        repo.upsert(
            _make_record(
                filepath="movies/Uploading.mkv",
                size_bytes=2 * 1024**3,
                audio_tracks=[
                    {"codec": "EAC3", "channels": 6, "lang": "eng"},
                    {"codec": "OPUS", "channels": 2, "lang": "eng"},
                ],
                resolution="720p",
                video_dynamic_range="HLG",
                scan_status=AppConstants.FILE_SCAN_STATUS_SCANNING,
            )
        )
        # Failed 'error' row MUST also be excluded from ready analytics
        repo.upsert(
            _make_record(
                filepath="movies/Corrupt.mkv",
                size_bytes=1 * 1024**3,
                audio_tracks=[{"codec": "AAC", "channels": 6, "lang": "eng"}],
                resolution="480p",
                video_dynamic_range="HDR",
                scan_status=AppConstants.FILE_SCAN_STATUS_ERROR,
            )
        )

        # Deterministic disk usage: 100 GiB total, 50 GiB used, 50 GiB free
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _path: _DiskUsage(100 * 1024**3, 50 * 1024**3, 50 * 1024**3),
        )

        resp = await api_client.get("/api/dashboard")
        assert resp.status_code == AppConstants.HTTP_OK
        payload = resp.json()

        # Stats total_files includes all indexed rows (3 ready + 1 scanning + 1 error = 5)
        assert payload["stats"]["total_files"] == 5

        analytics = payload["analytics"]
        storage = analytics["storage"]
        assert storage["mkv_count"] == 4
        assert storage["mkv_bytes"] == 33 * 1024**3
        assert storage["mp4_count"] == 1
        assert storage["mp4_bytes"] == 5 * 1024**3
        assert storage["media_bytes"] == 38 * 1024**3
        assert storage["disk_total_bytes"] == 100 * 1024**3
        assert storage["disk_used_bytes"] == 50 * 1024**3
        assert storage["disk_free_bytes"] == 50 * 1024**3
        assert storage["other_used_bytes"] == 12 * 1024**3

        # Audio codecs: 6 ready tracks total (1 each of EAC3, AC3, AAC, DTS, TrueHD, Other)
        assert analytics["total_audio_tracks"] == 6
        codec_map = {item["key"]: item for item in analytics["audio_codecs"]}
        assert set(codec_map.keys()) == {"EAC3", "AC3", "AAC", "DTS", "TrueHD", "Other"}
        assert sum(item["count"] for item in analytics["audio_codecs"]) == 6
        total_pct = sum(item["percent"] for item in analytics["audio_codecs"])
        assert 99.0 <= total_pct <= 101.0

        # Resolutions & Dynamic Ranges exclude 720p/480p/HLG/HDR from scanning/error rows
        res_map = {item["label"]: item["count"] for item in analytics["resolutions"]}
        assert res_map == {"2160p": 1, "1080p": 2}
        dr_map = {item["label"]: item["count"] for item in analytics["dynamic_ranges"]}
        assert dr_map == {"DV HDR": 1, "HDR10": 1, "SDR": 1}

    async def test_storage_other_used_bytes_clamped_when_cache_exceeds_disk_used(
        self, api_app, api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If SQLite media_bytes is temporarily larger than disk_used, other_used_bytes must clamp to 0."""
        api_app.state.media_repo.upsert(
            _make_record(
                filepath="movies/Huge.mkv",
                size_bytes=80 * 1024**3,
                audio_tracks=[{"codec": "EAC3", "channels": 6, "lang": "eng"}],
            )
        )
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _path: _DiskUsage(100 * 1024**3, 40 * 1024**3, 60 * 1024**3),
        )

        resp = await api_client.get("/api/dashboard")
        assert resp.status_code == AppConstants.HTTP_OK
        storage = resp.json()["analytics"]["storage"]
        assert storage["media_bytes"] == 80 * 1024**3
        assert storage["other_used_bytes"] == 0


class TestAudioCodecLibraryFilter:
    """Verify GET /api/files?audio_codec=... filtering across codec families and edge cases."""

    async def test_audio_codec_filter_matches_families_and_excludes_scanning(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        repo = api_app.state.media_repo
        repo.upsert(
            _make_record(
                filepath="movies/Eac3Movie.mkv",
                size_bytes=1000,
                audio_tracks=[{"codec": "EAC3", "channels": 6, "lang": "eng"}],
            )
        )
        repo.upsert(
            _make_record(
                filepath="movies/DtsHdMovie.mkv",
                size_bytes=1000,
                audio_tracks=[{"codec": "DTS-HD", "channels": 8, "lang": "eng"}],
            )
        )
        repo.upsert(
            _make_record(
                filepath="movies/DtsMovie.mkv",
                size_bytes=1000,
                audio_tracks=[{"codec": "DTS", "channels": 6, "lang": "eng"}],
            )
        )
        repo.upsert(
            _make_record(
                filepath="movies/OpusMovie.mkv",
                size_bytes=1000,
                audio_tracks=[{"codec": "OPUS", "channels": 2, "lang": "eng"}],
            )
        )
        repo.upsert(
            _make_record(
                filepath="movies/ScanningEac3.mkv",
                size_bytes=1000,
                audio_tracks=[{"codec": "EAC3", "channels": 6, "lang": "eng"}],
                scan_status=AppConstants.FILE_SCAN_STATUS_SCANNING,
            )
        )

        # EAC3 excludes scanning row
        r_eac3 = await api_client.get("/api/files", params={"audio_codec": "EAC3"})
        assert r_eac3.status_code == AppConstants.HTTP_OK
        assert [f["filename"] for f in r_eac3.json()["items"]] == ["Eac3Movie.mkv"]

        # DTS matches both DTS and DTS-HD
        r_dts = await api_client.get("/api/files", params={"audio_codec": "DTS"})
        assert r_dts.status_code == AppConstants.HTTP_OK
        assert sorted(f["filename"] for f in r_dts.json()["items"]) == [
            "DtsHdMovie.mkv",
            "DtsMovie.mkv",
        ]

        # Other matches non-standard codec (OPUS)
        r_other = await api_client.get("/api/files", params={"audio_codec": "Other"})
        assert r_other.status_code == AppConstants.HTTP_OK
        assert [f["filename"] for f in r_other.json()["items"]] == ["OpusMovie.mkv"]

        # Unknown codec returns empty list cleanly without 500
        r_unknown = await api_client.get("/api/files", params={"audio_codec": "NONEXISTENT"})
        assert r_unknown.status_code == AppConstants.HTTP_OK
        assert r_unknown.json()["items"] == []

    async def test_multi_lens_dashboard_storage_channels_languages_and_1to1_library_filters(
        self, api_app, api_client: httpx.AsyncClient
    ) -> None:
        """Verify storage_by_codec, storage_by_resolution, audio_channels, audio_languages ('und' normalization + Top-5 cap), and 1:1 /api/files filters."""
        repo = api_app.state.media_repo

        # 1. 4K HEVC file with 7.1 eng + 2.0 null-language (normalized to 'und'), has metadata, 2 subs
        repo.upsert(
            _make_record(
                filepath="movies/Interstellar.mkv",
                size_bytes=40 * 1024**3,
                resolution="2160p",
                video_codec="HEVC",
                video_dynamic_range="DV HDR",
                subtitle_count=2,
                container_title="Interstellar.Scene.Title",
                audio_tracks=[
                    {"codec": "TrueHD", "channels": 8, "channel_layout": "7.1", "language": "eng"},
                    {"codec": "AAC", "channels": 2, "channel_layout": "2.0", "language": None},
                ],
            )
        )
        # 2. 1080p H.264 file with 5.1 empty-string language ('') + 5.1 'UND' (both normalize to 'und'), 0 subs
        repo.upsert(
            _make_record(
                filepath="movies/LegacyH264.mkv",
                size_bytes=15 * 1024**3,
                resolution="1080p",
                video_codec="H.264",
                video_dynamic_range="SDR",
                subtitle_count=0,
                audio_tracks=[
                    {"codec": "AC3", "channels": 6, "channel_layout": "5.1", "language": ""},
                    {"codec": "AAC", "channels": 6, "channel_layout": "5.1", "language": "UND"},
                ],
            )
        )
        # 3. 1080p AV1 file with 6 tagged languages (hin, ben, jpn, spa, fra, deu) to test Top-5 cap + 'other' rollup
        repo.upsert(
            _make_record(
                filepath="movies/PolyglotAV1.mkv",
                size_bytes=5 * 1024**3,
                resolution="1080p",
                video_codec="AV1",
                video_dynamic_range="HDR10",
                subtitle_count=0,
                audio_tracks=[
                    {"codec": "EAC3", "channels": 6, "language": "hin"},
                    {"codec": "EAC3", "channels": 6, "language": "hin"},
                    {"codec": "EAC3", "channels": 6, "language": "ben"},
                    {"codec": "EAC3", "channels": 6, "language": "ben"},
                    {"codec": "AAC", "channels": 2, "language": "jpn"},
                    {"codec": "AAC", "channels": 2, "language": "jpn"},
                    {"codec": "AAC", "channels": 2, "language": "spa"},
                    {"codec": "AAC", "channels": 2, "language": "spa"},
                    {"codec": "Opus", "channels": 1, "language": "fra"},
                    {"codec": "Opus", "channels": 4, "language": "deu"},
                ],
            )
        )

        dash_resp = await api_client.get("/api/dashboard")
        assert dash_resp.status_code == AppConstants.HTTP_OK
        dash = dash_resp.json()
        stats = dash["stats"]
        analytics = dash["analytics"]

        # Guardrail 1: Both Interstellar (language=None) and LegacyH264 (language="" / "UND") count as untagged_audio
        assert stats["untagged_audio"] == 2
        assert stats["untagged_audio_bytes"] == 55 * 1024**3
        assert stats["has_metadata"] == 1
        assert stats["has_metadata_bytes"] == 40 * 1024**3
        assert stats["no_subtitles"] == 2
        assert stats["with_subtitles"] == 1

        # 1:1 Parity between stats.untagged_audio and GET /api/files?audio_language=und
        r_und = await api_client.get("/api/files", params={"audio_language": "und"})
        assert r_und.status_code == AppConstants.HTTP_OK
        assert r_und.json()["total"] == stats["untagged_audio"]
        assert sorted(f["filename"] for f in r_und.json()["items"]) == [
            "Interstellar.mkv",
            "LegacyH264.mkv",
        ]

        # Storage by Video Codec lens (HEVC 40 GiB, H.264 15 GiB, AV1 5 GiB)
        s_codec = {s["key"]: s for s in analytics["storage_by_codec"]}
        assert list(s_codec.keys()) == ["HEVC", "H.264", "AV1"]
        assert s_codec["HEVC"]["size_bytes"] == 40 * 1024**3
        assert s_codec["HEVC"]["file_count"] == 1
        assert s_codec["H.264"]["size_bytes"] == 15 * 1024**3
        assert s_codec["AV1"]["size_bytes"] == 5 * 1024**3

        # Storage by Resolution lens (2160p 40 GiB / 1 file, 1080p 20 GiB / 2 files)
        s_res = {s["key"]: s for s in analytics["storage_by_resolution"]}
        assert list(s_res.keys()) == ["2160p", "1080p"]
        assert s_res["2160p"]["size_bytes"] == 40 * 1024**3
        assert s_res["1080p"]["size_bytes"] == 20 * 1024**3
        assert s_res["1080p"]["file_count"] == 2

        # Audio Channels lens (7.1: 1, 5.1: 6, 2.0: 5, 1.0: 1, Other: 1 [4ch])
        ch_map = {c["key"]: c["count"] for c in analytics["audio_channels"]}
        assert ch_map == {"7.1": 1, "5.1": 6, "2.0": 5, "1.0": 1, "Other": 1}

        # Audio Languages lens: Top 5 tagged (ben:2, hin:2, jpn:2, spa:2, deu:1) + pinned 'und' (3) + 'other' (eng:1 + fra:1 = 2)
        lang_map = {l["key"]: l for l in analytics["audio_languages"]}
        assert "und" in lang_map
        assert lang_map["und"]["count"] == 3
        assert lang_map["und"]["label"] == "und (Untagged)"
        assert "other" in lang_map
        assert lang_map["other"]["count"] == 2

        # Video Codecs strip pills
        vc_map = {v["label"]: v["count"] for v in analytics["video_codecs"]}
        assert vc_map == {"HEVC": 1, "H.264": 1, "AV1": 1}

        # Verify all new /api/files filters match 1:1
        r_vc = await api_client.get("/api/files", params={"video_codec": "H.264"})
        assert [f["filename"] for f in r_vc.json()["items"]] == ["LegacyH264.mkv"]

        r_res = await api_client.get("/api/files", params={"resolution": "2160p"})
        assert [f["filename"] for f in r_res.json()["items"]] == ["Interstellar.mkv"]

        r_dr = await api_client.get("/api/files", params={"dynamic_range": "HDR10"})
        assert [f["filename"] for f in r_dr.json()["items"]] == ["PolyglotAV1.mkv"]

        r_ch = await api_client.get("/api/files", params={"audio_channels": "7.1"})
        assert [f["filename"] for f in r_ch.json()["items"]] == ["Interstellar.mkv"]

        r_lang_other = await api_client.get("/api/files", params={"audio_language": "other"})
        assert sorted(f["filename"] for f in r_lang_other.json()["items"]) == [
            "Interstellar.mkv",
            "PolyglotAV1.mkv",
        ]

        r_nosubs = await api_client.get("/api/files", params={"no_subtitles": "true"})
        assert r_nosubs.json()["total"] == stats["no_subtitles"]
        assert sorted(f["filename"] for f in r_nosubs.json()["items"]) == [
            "LegacyH264.mkv",
            "PolyglotAV1.mkv",
        ]


class TestDashboardUILandmarks:
    """Verify honest KPI labels, removed Quick Actions strip, and new analytics UI cards."""

    async def test_index_html_contains_redesigned_dashboard_landmarks(
        self, api_client: httpx.AsyncClient
    ) -> None:
        resp = await api_client.get("/")
        assert resp.status_code == AppConstants.HTTP_OK
        html = resp.text
        normalized_html = " ".join(html.split())

        # Honest actionable KPI wording (profile-driven Needs Conversion replaces hardcoded AAC 5.1 Only)
        assert "Show library" in normalized_html
        assert "Show incompatible" in normalized_html
        assert "Show extra metadata" in normalized_html
        # Misleading Quick Actions wording removed
        assert "Fix ${dashboard.stats.aac51_only}" not in html
        assert "Clean ${dashboard.stats.has_metadata}" not in html

        # Analytics cards & responsive landmarks
        assert "Storage Breakdown" in html
        assert "Audio Codecs" in html
        assert "openLibraryPreset({ audioCodec: seg.key })" in html
        assert "Dynamic Range" in html
        assert "Resolution" in html
        assert "/static/js/charts.js" in html
        assert 'x-effect="syncStoragePieChart($el)"' in html
        assert 'x-effect="syncAudioPieChart($el)"' in html
        assert 'onAudioPieClick($event)' in html


class TestPieChartGeometryEngine:
    """Exhaustive tests for app/static/js/charts.js split-wedge geometry and edge cases."""

    def test_build_pie_slices_multi_single_and_negative_edge_cases(self) -> None:
        import subprocess
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[2]
        script = """
        const fs = require('fs');
        const vm = require('vm');
        const sandbox = { window: {} };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync('app/static/js/charts.js', 'utf8'), sandbox);
        const Charts = sandbox.MKVForgeCharts;

        const empty = Charts.buildPieSlices([]);
        const nullInput = Charts.buildPieSlices(null);
        const zeroOnly = Charts.buildPieSlices([{ key: 'a', value: 0 }, { key: 'b', value: -5 }]);
        const single = Charts.buildPieSlices([{ key: 'mkv', label: 'MKV', value: 100 }]);
        const multi = Charts.buildPieSlices([
          { key: 'eac3', label: 'DD+', value: 60 },
          { key: 'aac', label: 'AAC', value: 35 },
          { key: 'other', label: 'Other', value: 5 },
          { key: 'zero', label: 'Zero', value: 0 },
        ]);

        process.stdout.write(JSON.stringify({ empty, nullInput, zeroOnly, single, multi }));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=True,
        )
        res = json.loads(proc.stdout)

        # Negative / empty edge cases fail safely to []
        assert res["empty"] == []
        assert res["nullInput"] == []
        assert res["zeroOnly"] == []

        # Single 100% slice renders a full two-arc annular ring (4 'A ' commands)
        assert len(res["single"]) == 1
        s0 = res["single"][0]
        assert s0["percent"] == 100
        assert s0["percentLabel"] == "100%"
        assert s0["showLabel"] is True
        assert s0["path"].count("A ") == 4
        assert s0["activePath"].count("A ") == 4

        # Multi-slice split geometry: filters zero, pops activePath, hides label on 5% sliver
        assert len(res["multi"]) == 3
        by_key = {item["key"]: item for item in res["multi"]}
        assert by_key["eac3"]["percent"] == 60
        assert by_key["eac3"]["showLabel"] is True
        assert by_key["eac3"]["activePath"] != by_key["eac3"]["path"]
        assert by_key["aac"]["percent"] == 35
        assert by_key["aac"]["showLabel"] is True
        assert by_key["other"]["percent"] == 5
        assert by_key["other"]["showLabel"] is False


class TestDedicatedAppIcon:
    """Verify dedicated SVG/PNG app icons are served offline and wired across all brand locations."""

    async def test_app_icon_assets_and_favicon_route(
        self, api_client: httpx.AsyncClient
    ) -> None:
        svg_resp = await api_client.get("/static/img/app-icon.svg")
        assert svg_resp.status_code == AppConstants.HTTP_OK
        assert "<svg" in svg_resp.text
        assert "#FFB450" in svg_resp.text

        png_resp = await api_client.get("/static/img/app-icon.png")
        assert png_resp.status_code == AppConstants.HTTP_OK
        assert png_resp.content.startswith(b"\x89PNG\r\n\x1a\n")

        fav_resp = await api_client.get("/favicon.ico")
        assert fav_resp.status_code == AppConstants.HTTP_OK
        assert "image/svg+xml" in fav_resp.headers.get("content-type", "")

        html = (await api_client.get("/")).text
        # Wired in browser tab favicon, sidebar, mobile header, onboarding, and sign-in modal (>= 5 refs)
        assert html.count("/static/img/app-icon.svg") >= 5
        assert "/static/img/app-icon.png" in html
        # Legacy placeholder play-box icon fully removed
        assert "M10 9l5 3-5 3V9z" not in html


class TestHdrClassificationAndSvgRendering:
    """Verify MKVToolNix v69+ HDR keys, force backfill, and true-namespace SVG pie rendering."""

    def test_render_pie_svg_emits_valid_svg_namespace_with_gradients_and_labels(self) -> None:
        import subprocess
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[2]
        script = """
        const fs = require('fs');
        const vm = require('vm');
        const sandbox = { window: {} };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync('app/static/js/charts.js', 'utf8'), sandbox);
        const Charts = sandbox.MKVForgeCharts;
        const slices = Charts.buildPieSlices([
          { key: 'mkv', label: 'MKV', value: 70, colorStart: '#60A5FA', colorEnd: '#2563EB' },
          { key: 'mp4', label: 'MP4', value: 30, colorStart: '#34D399', colorEnd: '#059669' },
        ]);
        const svg = Charts.renderPieSvg(slices, 'mkv', { idPrefix: 'st', ariaLabel: 'Storage' });
        const emptySvg = Charts.renderPieSvg([], null, { idPrefix: 'st', ariaLabel: 'Storage' });
        process.stdout.write(JSON.stringify({ svg, emptySvg }));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        svg = out["svg"]
        assert '<svg viewBox="0 0 200 200"' in svg
        assert '<linearGradient id="st-grad-mkv"' in svg
        assert 'data-slice-key="mkv"' in svg
        assert ">70%</text>" in svg
        assert ">30%</text>" in svg
        assert "<circle " in out["emptySvg"]

    def test_patch_pie_svg_reconciles_dom_in_place_without_remounting(self) -> None:
        import subprocess
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[2]
        script = """
        const fs = require('fs');
        const vm = require('vm');

        class FakeEl {
          constructor(tag, ns) {
            this.tagName = tag;
            this.namespaceURI = ns;
            this.attrs = {};
            this.style = {};
            this.children = [];
            this.parentNode = null;
            this.textContent = '';
            this.ownerDocument = null;
          }
          setAttribute(k, v) { this.attrs[k] = String(v); }
          getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
          appendChild(child) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = this;
            this.children.push(child);
            return child;
          }
          insertBefore(child, ref) {
            if (child.parentNode) child.parentNode.removeChild(child);
            const idx = this.children.indexOf(ref);
            child.parentNode = this;
            if (idx === -1) this.children.push(child);
            else this.children.splice(idx, 0, child);
            return child;
          }
          removeChild(child) {
            const idx = this.children.indexOf(child);
            if (idx !== -1) this.children.splice(idx, 1);
            child.parentNode = null;
            return child;
          }
        }

        const fakeDoc = {
          createElementNS(ns, tag) {
            const el = new FakeEl(tag, ns);
            el.ownerDocument = fakeDoc;
            return el;
          },
        };
        const host = new FakeEl('div', 'http://www.w3.org/1999/xhtml');
        host.ownerDocument = fakeDoc;

        const sandbox = { window: {}, document: fakeDoc };
        sandbox.window = sandbox;
        vm.runInNewContext(fs.readFileSync('app/static/js/charts.js', 'utf8'), sandbox);
        const Charts = sandbox.MKVForgeCharts;

        // Negative / edge cases fail safely
        const nullOk = Charts.patchPieSvg(null, [], null, { idPrefix: 'ac' });
        const emptyOk = Charts.patchPieSvg(host, [], null, { idPrefix: 'ac', ariaLabel: 'Audio' });
        const svgEl = host.children[0];
        const emptyRing = svgEl.children.find((c) => c.getAttribute('data-role') === 'empty-ring');
        const emptyRingVisibleWhenEmpty = emptyRing.getAttribute('display') === '';

        // First live scan batch (1 slice)
        const s1 = Charts.buildPieSlices([{ key: 'EAC3', label: 'DD+', value: 10 }]);
        Charts.patchPieSvg(host, s1, null, { idPrefix: 'ac', ariaLabel: 'Audio' });
        const sameSvgAfterData = host.children[0] === svgEl;
        const emptyRingHiddenWithData = emptyRing.getAttribute('display') === 'none';
        const slicesEl = svgEl.children.find((c) => c.getAttribute('data-role') === 'slices');
        const eac3Group1 = slicesEl.children[0];
        const eac3Path1 = eac3Group1.children[0];
        const dBefore = eac3Path1.getAttribute('d');

        // Second live scan batch (EAC3 grows + AAC arrives + hover EAC3)
        const s2 = Charts.buildPieSlices([
          { key: 'EAC3', label: 'DD+', value: 15 },
          { key: 'AAC', label: 'AAC', value: 5 },
        ]);
        Charts.patchPieSvg(host, s2, 'EAC3', { idPrefix: 'ac', ariaLabel: 'Audio' });
        const sameSvgAfterUpdate = host.children[0] === svgEl;
        const sameGroupReused = slicesEl.children[0] === eac3Group1;
        const samePathReused = slicesEl.children[0].children[0] === eac3Path1;
        const pathUpdatedInPlace = eac3Path1.getAttribute('d') !== dBefore;
        const aacDimmed = slicesEl.children[1].getAttribute('opacity') === '0.55';
        const eac3Shadowed = Boolean(eac3Path1.style.filter);

        // Removing AAC cleans up only the removed slice without remounting EAC3
        const s3 = Charts.buildPieSlices([{ key: 'EAC3', label: 'DD+', value: 20 }]);
        Charts.patchPieSvg(host, s3, null, { idPrefix: 'ac', ariaLabel: 'Audio' });
        const finalSliceCount = slicesEl.children.length;
        const eac3StillSameNode = slicesEl.children[0] === eac3Group1;

        console.log(JSON.stringify({
          nullOk,
          emptyOk,
          emptyRingVisibleWhenEmpty,
          sameSvgAfterData,
          emptyRingHiddenWithData,
          sameSvgAfterUpdate,
          sameGroupReused,
          samePathReused,
          pathUpdatedInPlace,
          aacDimmed,
          eac3Shadowed,
          finalSliceCount,
          eac3StillSameNode,
        }));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=True,
        )
        res = json.loads(proc.stdout)
        assert res["nullOk"] is False
        assert res["emptyOk"] is True
        assert res["emptyRingVisibleWhenEmpty"] is True
        assert res["sameSvgAfterData"] is True
        assert res["emptyRingHiddenWithData"] is True
        assert res["sameSvgAfterUpdate"] is True
        assert res["sameGroupReused"] is True
        assert res["samePathReused"] is True
        assert res["pathUpdatedInPlace"] is True
        assert res["aacDimmed"] is True
        assert res["eac3Shadowed"] is True
        assert res["finalSliceCount"] == 1
        assert res["eac3StillSameNode"] is True

    async def test_mkvtoolnix_v69_hdr_keys_and_force_backfill(
        self, api_app
    ) -> None:
        from app.services.probe_parser import parse_mkvmerge_json

        # MKVToolNix v69+ uses `color_transfer`, `color_matrix`, `max_luminance`, `id_type`
        hdr10_payload = {
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "HEVC/H.265/MPEG-H",
                    "properties": {
                        "codec_id": "V_MPEGH/ISO/HEVC",
                        "pixel_dimensions": "3840x2160",
                        "color_transfer": 16,
                        "max_luminance": 1000.0,
                    },
                }
            ]
        }
        assert parse_mkvmerge_json(hdr10_payload).video_dynamic_range == AppConstants.DYNAMIC_RANGE_HDR10

        bt2020_matrix_payload = {
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "HEVC/H.265/MPEG-H",
                    "properties": {
                        "codec_id": "V_MPEGH/ISO/HEVC",
                        "pixel_dimensions": "3840x2160",
                        "color_matrix": 9,
                    },
                }
            ]
        }
        assert parse_mkvmerge_json(bt2020_matrix_payload).video_dynamic_range == AppConstants.DYNAMIC_RANGE_HDR

        dv_hdr_payload = {
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "HEVC/H.265/MPEG-H",
                    "properties": {
                        "codec_id": "V_MPEGH/ISO/HEVC",
                        "pixel_dimensions": "3840x2160",
                        "color_transfer": 16,
                        "block_addition_mappings": [{"id_type": 1685485123}],
                    },
                }
            ]
        }
        assert parse_mkvmerge_json(dv_hdr_payload).video_dynamic_range == AppConstants.DYNAMIC_RANGE_DV_HDR

        # Force backfill upgrades previously-misclassified 'SDR' row using cached raw_json
        repo = api_app.state.media_repo
        rec = _make_record(
            filepath="movies/StaleHdr.mkv",
            size_bytes=5000,
            audio_tracks=[{"codec": "EAC3", "channels": 6, "lang": "eng"}],
            video_dynamic_range=AppConstants.DYNAMIC_RANGE_SDR,
        )
        # Replace raw_json with HDR10 mkvmerge payload
        from dataclasses import replace

        repo.upsert(replace(rec, raw_json=json.dumps(hdr10_payload)))
        updated = repo.backfill_video_dynamic_range(
            lambda payload: parse_mkvmerge_json(payload).video_dynamic_range,
            force=True,
        )
        assert updated >= 1
        row = repo.get_by_filepath("movies/StaleHdr.mkv")
        assert row is not None
        assert row["video_dynamic_range"] == AppConstants.DYNAMIC_RANGE_HDR10

    async def test_app_lifespan_startup_runs_versioned_dr_backfill_cleanly(
        self, api_app
    ) -> None:
        async with api_app.router.lifespan_context(api_app):
            stored_ver = api_app.state.state_repo.get(
                AppConstants.STATE_DYNAMIC_RANGE_CLASSIFIER_VERSION
            )
            assert stored_ver == AppConstants.DYNAMIC_RANGE_CLASSIFIER_VERSION




