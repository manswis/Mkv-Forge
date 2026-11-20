"""Integration and unit tests for lazy storage folder tree API and folder_tree.js."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import AppConstants, Settings


class TestFoldersApi:
    """Verify GET /api/folders and POST /api/folders security, lazy children, and error cases."""

    async def test_lazy_folder_listing_ignores_hidden_and_symlinks_outside_storage(
        self, api_client: httpx.AsyncClient, test_settings: Settings, tmp_path: Path
    ) -> None:
        storage = test_settings.storage_dir
        (storage / "movies" / "4K").mkdir(parents=True)
        (storage / "shows" / "Dark" / "Season 1").mkdir(parents=True)
        (storage / "empty_dir").mkdir()
        (storage / ".hidden_dir").mkdir()
        (storage / "movies" / "file.mkv").write_bytes(b"not-a-folder")

        # Symlink pointing outside storage must be excluded from tree!
        outside = tmp_path / "outside_secret"
        outside.mkdir()
        (storage / "escape_link").symlink_to(outside)

        # Root listing
        root_resp = await api_client.get("/api/folders", params={"path": "/"})
        assert root_resp.status_code == AppConstants.HTTP_OK
        root_data = root_resp.json()
        assert root_data["path"] == "/"
        by_name = {entry["name"]: entry for entry in root_data["entries"]}
        assert list(by_name.keys()) == ["empty_dir", "movies", "shows"]
        assert by_name["empty_dir"]["has_children"] is False
        assert by_name["movies"]["has_children"] is True
        assert by_name["shows"]["has_children"] is True
        assert ".hidden_dir" not in by_name
        assert "escape_link" not in by_name

        # Nested listing under /shows
        shows_resp = await api_client.get("/api/folders", params={"path": "/shows"})
        assert shows_resp.status_code == AppConstants.HTTP_OK
        shows_data = shows_resp.json()
        assert shows_data["path"] == "/shows"
        assert shows_data["entries"] == [
            {"name": "Dark", "path": "/shows/Dark", "has_children": True}
        ]

        # Leaf listing under /shows/Dark/Season 1
        leaf_resp = await api_client.get(
            "/api/folders", params={"path": "/shows/Dark/Season 1"}
        )
        assert leaf_resp.status_code == AppConstants.HTTP_OK
        assert leaf_resp.json()["entries"] == []

    async def test_folder_listing_rejects_traversal_and_missing_dirs(
        self, api_client: httpx.AsyncClient
    ) -> None:
        trav = await api_client.get("/api/folders", params={"path": "/../etc"})
        assert trav.status_code == AppConstants.HTTP_BAD_REQUEST

        bslash = await api_client.get("/api/folders", params={"path": "movies\\4k"})
        assert bslash.status_code == AppConstants.HTTP_BAD_REQUEST

        missing = await api_client.get("/api/folders", params={"path": "/does_not_exist"})
        assert missing.status_code == AppConstants.HTTP_NOT_FOUND

    async def test_create_subfolder_happy_path_and_guards(
        self, api_client: httpx.AsyncClient, test_settings: Settings
    ) -> None:
        (test_settings.storage_dir / "test").mkdir(parents=True, exist_ok=True)

        created = await api_client.post(
            "/api/folders",
            json={"parent": "/test", "name": "Dolby Exports"},
        )
        assert created.status_code == AppConstants.HTTP_CREATED
        assert created.json() == {
            "name": "Dolby Exports",
            "path": "/test/Dolby Exports",
            "has_children": False,
        }
        assert (test_settings.storage_dir / "test" / "Dolby Exports").is_dir()

        # Duplicate -> 409 Conflict
        dup = await api_client.post(
            "/api/folders",
            json={"parent": "/test", "name": "Dolby Exports"},
        )
        assert dup.status_code == AppConstants.HTTP_CONFLICT

        # Invalid names (empty, traversal, slash, hidden dot) -> 400
        for bad_name in ("", "   ", "..", "a/b", "a\\b", ".secret"):
            bad = await api_client.post(
                "/api/folders",
                json={"parent": "/test", "name": bad_name},
            )
            assert bad.status_code == AppConstants.HTTP_BAD_REQUEST

        # Non-existent parent -> 404
        missing_parent = await api_client.post(
            "/api/folders",
            json={"parent": "/nope", "name": "sub"},
        )
        assert missing_parent.status_code == AppConstants.HTTP_NOT_FOUND


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestFolderTreeJsLogic:
    """Execute app/static/js/folder_tree.js in Node to verify expand/collapse flattening."""

    def test_expand_collapse_ancestors_and_flattening(
        self, project_root: Path
    ) -> None:
        script = f"""
        global.window = global;
        require({json.dumps(str(project_root / 'app/static/js/constants.js'))});
        require({json.dumps(str(project_root / 'app/static/js/folder_tree.js'))});

        const FT = window.MKVForgeFolderTree;
        const ancestors = FT.getAncestorPaths("/shows/Dark/Season 1");
        const state = FT.createFolderTreeState("/shows/Dark");

        FT.mergeFolderChildren(state, "/", [
          {{ name: "shows", path: "/shows", has_children: true }},
          {{ name: "movies", path: "/movies", has_children: false }},
        ]);
        FT.mergeFolderChildren(state, "/shows", [
          {{ name: "Dark", path: "/shows/Dark", has_children: true }},
        ]);

        const expandedRows = FT.flattenFolderTree(state);

        // Collapse /shows -> /shows/Dark must hide
        delete state.expanded["/shows"];
        const collapsedRows = FT.flattenFolderTree(state);

        console.log(JSON.stringify({{ ancestors, expandedRows, collapsedRows }}));
        """
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)

        assert out["ancestors"] == ["/", "/shows", "/shows/Dark", "/shows/Dark/Season 1"]
        assert [r["path"] for r in out["expandedRows"]] == [
            "/",
            "/movies",
            "/shows",
            "/shows/Dark",
        ]
        assert out["expandedRows"][3]["isSelected"] is True
        assert out["expandedRows"][3]["depth"] == 2
        assert [r["path"] for r in out["collapsedRows"]] == ["/", "/movies", "/shows"]
