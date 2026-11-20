"""SQLite repository layer for app_state, media_files, and jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from app.config import AppConstants
from app.db.connection import DatabaseManager


def utc_now_iso() -> str:
    """Return current UTC timestamp formatted as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards so user search terms match literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True, slots=True)
class MediaFileUpsert:
    """Payload for inserting or updating a scanned media file row."""

    filepath: str
    folder: str
    filename: str
    size_bytes: int
    mtime: float
    duration_us: int
    resolution: str | None
    video_codec: str | None
    audio_summary_json: str
    subtitle_count: int
    container_title: str
    has_global_tags: bool
    has_aac_51: bool
    has_ac3_eac3: bool
    raw_json: str
    scanned_at: str
    video_dynamic_range: str | None = None
    scan_status: str = AppConstants.FILE_SCAN_STATUS_READY


class AppStateRepository:
    """Key-value state storage for scanner timestamps and app metadata."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def get(self, key: str) -> str | None:
        """Fetch a single state value by key."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else row["value"]

    def set(self, key: str, value: str | None) -> None:
        """Upsert a state key with the current UTC timestamp."""
        now = utc_now_iso()
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def get_all(self) -> dict[str, str | None]:
        """Return all persisted key-value pairs."""
        with self._db.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_state").fetchall()
        return {row["key"]: row["value"] for row in rows}


class MediaFileRepository:
    """CRUD and filtered query operations for cached media_files rows."""

    _LIST_COLUMNS: str = """
        id, filepath, folder, filename, size_bytes, mtime, duration_us,
        resolution, video_codec, video_dynamic_range, audio_summary_json, subtitle_count,
        container_title, has_global_tags, has_aac_51, has_ac3_eac3, scan_status, scanned_at
    """

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def upsert(self, record: MediaFileUpsert) -> int:
        """Insert or update a media file record by unique filepath and return row ID."""
        scan_status = (
            record.scan_status
            if record.scan_status in AppConstants.ALLOWED_FILE_SCAN_STATUSES
            else AppConstants.FILE_SCAN_STATUS_READY
        )
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO media_files (
                    filepath, folder, filename, size_bytes, mtime, duration_us,
                    resolution, video_codec, video_dynamic_range, audio_summary_json,
                    subtitle_count, container_title, has_global_tags, has_aac_51,
                    has_ac3_eac3, scan_status, raw_json, scanned_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    folder = excluded.folder,
                    filename = excluded.filename,
                    size_bytes = excluded.size_bytes,
                    mtime = excluded.mtime,
                    duration_us = excluded.duration_us,
                    resolution = excluded.resolution,
                    video_codec = excluded.video_codec,
                    video_dynamic_range = excluded.video_dynamic_range,
                    audio_summary_json = excluded.audio_summary_json,
                    subtitle_count = excluded.subtitle_count,
                    container_title = excluded.container_title,
                    has_global_tags = excluded.has_global_tags,
                    has_aac_51 = excluded.has_aac_51,
                    has_ac3_eac3 = excluded.has_ac3_eac3,
                    scan_status = excluded.scan_status,
                    raw_json = excluded.raw_json,
                    scanned_at = excluded.scanned_at
                """,
                (
                    record.filepath,
                    record.folder,
                    record.filename,
                    record.size_bytes,
                    record.mtime,
                    record.duration_us,
                    record.resolution,
                    record.video_codec,
                    record.video_dynamic_range,
                    record.audio_summary_json,
                    record.subtitle_count,
                    record.container_title,
                    int(record.has_global_tags),
                    int(record.has_aac_51),
                    int(record.has_ac3_eac3),
                    scan_status,
                    record.raw_json,
                    record.scanned_at,
                ),
            )
            row = conn.execute(
                "SELECT id FROM media_files WHERE filepath = ?",
                (record.filepath,),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def upsert_scanning_placeholder(
        self,
        *,
        filepath: str,
        folder: str,
        filename: str,
        size_bytes: int,
        mtime: float,
    ) -> tuple[int, bool]:
        """Insert a `scan_status='scanning'` placeholder if `filepath` does not exist yet.

        Returns `(file_id, inserted_new)`. Never overwrites an already-probed `'ready'` row.
        """
        now = utc_now_iso()
        with self._db.connect() as conn:
            existing = conn.execute(
                "SELECT id, scan_status FROM media_files WHERE filepath = ?",
                (filepath,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"]), False

            conn.execute(
                """
                INSERT INTO media_files (
                    filepath, folder, filename, size_bytes, mtime, duration_us,
                    resolution, video_codec, video_dynamic_range, audio_summary_json,
                    subtitle_count, container_title, has_global_tags, has_aac_51,
                    has_ac3_eac3, scan_status, raw_json, scanned_at
                )
                VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, NULL, '[]', 0, '', 0, 0, 0, ?, '{}', ?)
                """,
                (
                    filepath,
                    folder,
                    filename,
                    max(0, int(size_bytes)),
                    float(mtime),
                    AppConstants.FILE_SCAN_STATUS_SCANNING,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM media_files WHERE filepath = ?",
                (filepath,),
            ).fetchone()
        assert row is not None
        return int(row["id"]), True

    def mark_scan_error(
        self,
        *,
        filepath: str,
        folder: str,
        filename: str,
        size_bytes: int,
        mtime: float,
    ) -> int:
        """Mark a file row as `scan_status='error'` (upserting if absent) so it never stalls."""
        now = utc_now_iso()
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO media_files (
                    filepath, folder, filename, size_bytes, mtime, duration_us,
                    resolution, video_codec, video_dynamic_range, audio_summary_json,
                    subtitle_count, container_title, has_global_tags, has_aac_51,
                    has_ac3_eac3, scan_status, raw_json, scanned_at
                )
                VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, NULL, '[]', 0, '', 0, 0, 0, ?, '{}', ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    mtime = excluded.mtime,
                    scan_status = excluded.scan_status,
                    scanned_at = excluded.scanned_at
                """,
                (
                    filepath,
                    folder,
                    filename,
                    max(0, int(size_bytes)),
                    float(mtime),
                    AppConstants.FILE_SCAN_STATUS_ERROR,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM media_files WHERE filepath = ?",
                (filepath,),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def get_by_id(self, file_id: int) -> dict[str, Any] | None:
        """Return full media_files row including raw_json by primary key."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM media_files WHERE id = ?",
                (file_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_by_filepath(self, filepath: str) -> dict[str, Any] | None:
        """Return full media_files row including raw_json by unique filepath."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM media_files WHERE filepath = ?",
                (filepath,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_scan_index(self) -> dict[str, tuple[float, int, str]]:
        """Return lightweight {filepath: (mtime, size_bytes, scan_status)} index for incremental scans."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT filepath, mtime, size_bytes, scan_status FROM media_files"
            ).fetchall()
        return {
            row["filepath"]: (
                float(row["mtime"]),
                int(row["size_bytes"]),
                str(row["scan_status"] or AppConstants.FILE_SCAN_STATUS_READY),
            )
            for row in rows
        }

    def list_all_filepaths(self) -> list[tuple[int, str]]:
        """Return all indexed `(id, filepath)` pairs without reading raw_json."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT id, filepath FROM media_files ORDER BY id ASC"
            ).fetchall()
        return [(int(row["id"]), str(row["filepath"])) for row in rows]

    def delete_by_filepaths(self, filepaths: Iterable[str]) -> int:
        """Delete rows matching the given filepaths and return count deleted."""
        paths = list(filepaths)
        if not paths:
            return 0
        with self._db.connect() as conn:
            cursor = conn.executemany(
                "DELETE FROM media_files WHERE filepath = ?",
                [(path,) for path in paths],
            )
            return int(cursor.rowcount)

    def delete_by_id(self, file_id: int) -> bool:
        """Delete a single row by primary key."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM media_files WHERE id = ?",
                (file_id,),
            )
            return cursor.rowcount > 0

    def update_file_location(
        self,
        file_id: int,
        *,
        filepath: str,
        folder: str,
        filename: str,
    ) -> dict[str, Any] | None:
        """Update filepath/folder/filename after rename or move without re-probing."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE media_files
                SET filepath = ?, folder = ?, filename = ?
                WHERE id = ?
                """,
                (filepath, folder, filename, file_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM media_files WHERE id = ?",
                (file_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_folder_file_counts(self) -> dict[str, int]:
        """Return cumulative media file counts for every folder and its ancestors."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT folder, COUNT(*) AS cnt FROM media_files GROUP BY folder"
            ).fetchall()

        counts: dict[str, int] = {}
        for row in rows:
            folder = str(row["folder"] or "/")
            cnt = int(row["cnt"] or 0)
            counts["/"] = counts.get("/", 0) + cnt
            if folder == "/":
                continue
            segments = folder.strip("/").split("/")
            current = ""
            for seg in segments:
                if not seg:
                    continue
                current = f"{current}/{seg}"
                counts[current] = counts.get(current, 0) + cnt
        return counts

    def count_by_folder_prefix(self, display_folder: str) -> int:
        """Return count of indexed media files inside `display_folder` or its descendants."""
        normalized = f"/{display_folder.strip('/')}" if display_folder.strip("/") else "/"
        with self._db.connect() as conn:
            if normalized == "/":
                row = conn.execute("SELECT COUNT(*) AS cnt FROM media_files").fetchone()
            else:
                prefix_pattern = f"{_escape_like(normalized)}/%"
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM media_files
                    WHERE folder = ? OR folder LIKE ? ESCAPE '\\'
                    """,
                    (normalized, prefix_pattern),
                ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def update_folder_prefix(
        self,
        old_display_folder: str,
        new_display_folder: str,
    ) -> list[tuple[int, str]]:
        """Atomically rewrite `folder` and `filepath` for all files under a moved/renamed folder."""
        old_norm = f"/{old_display_folder.strip('/')}"
        new_norm = f"/{new_display_folder.strip('/')}"
        old_rel_prefix = old_norm.lstrip("/")
        new_rel_prefix = new_norm.lstrip("/")
        prefix_pattern = f"{_escape_like(old_norm)}/%"

        updated: list[tuple[int, str]] = []
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, filepath, folder
                FROM media_files
                WHERE folder = ? OR folder LIKE ? ESCAPE '\\'
                """,
                (old_norm, prefix_pattern),
            ).fetchall()

            for row in rows:
                file_id = int(row["id"])
                old_fp = str(row["filepath"])
                old_f = str(row["folder"])
                if old_fp == old_rel_prefix:
                    new_fp = new_rel_prefix
                elif old_fp.startswith(f"{old_rel_prefix}/"):
                    new_fp = f"{new_rel_prefix}{old_fp[len(old_rel_prefix):]}"
                else:
                    new_fp = old_fp

                if old_f == old_norm:
                    new_f = new_norm
                elif old_f.startswith(f"{old_norm}/"):
                    new_f = f"{new_norm}{old_f[len(old_norm):]}"
                else:
                    new_f = old_f

                conn.execute(
                    "UPDATE media_files SET filepath = ?, folder = ? WHERE id = ?",
                    (new_fp, new_f, file_id),
                )
                updated.append((file_id, new_fp))
        return updated

    def delete_by_folder_prefix(self, display_folder: str) -> list[tuple[int, str]]:
        """Delete all media rows under `display_folder` and return `(id, filepath)` pairs."""
        norm = f"/{display_folder.strip('/')}"
        prefix_pattern = f"{_escape_like(norm)}/%"
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, filepath
                FROM media_files
                WHERE folder = ? OR folder LIKE ? ESCAPE '\\'
                """,
                (norm, prefix_pattern),
            ).fetchall()
            deleted = [(int(r["id"]), str(r["filepath"])) for r in rows]
            if deleted:
                conn.execute(
                    """
                    DELETE FROM media_files
                    WHERE folder = ? OR folder LIKE ? ESCAPE '\\'
                    """,
                    (norm, prefix_pattern),
                )
        return deleted

    @staticmethod
    def _build_filter_where(
        *,
        q: str | None = None,
        folder: str | None = None,
        audio_type: str | None = None,
        audio_codec: str | None = None,
        aac51_only: bool = False,
        aac51: bool = False,
        has_metadata: bool = False,
        no_dolby: bool = False,
    ) -> tuple[str, list[Any]]:
        """Build parameterized SQL WHERE clause shared by list and facet queries."""
        clauses: list[str] = []
        params: list[Any] = []

        if q and q.strip():
            pattern = f"%{_escape_like(q.strip())}%"
            clauses.append(
                "(filename LIKE ? ESCAPE '\\' OR folder LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern])

        if folder and folder.strip():
            clauses.append("folder = ?")
            params.append(folder.strip())

        if audio_type and audio_type.strip():
            clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM json_each(media_files.audio_summary_json) AS je
                    WHERE LOWER(TRIM(
                        COALESCE(json_extract(je.value, '$.codec'), '') ||
                        CASE
                            WHEN COALESCE(json_extract(je.value, '$.channel_layout'), '') != ''
                            THEN ' ' || json_extract(je.value, '$.channel_layout')
                            ELSE ''
                        END
                    )) = LOWER(?)
                )
                """
            )
            params.append(audio_type.strip())

        if audio_codec and audio_codec.strip():
            codec_key = audio_codec.strip().upper()
            clauses.append("scan_status = 'ready'")
            if codec_key == "DTS":
                clauses.append(
                    """
                    EXISTS (
                        SELECT 1
                        FROM json_each(media_files.audio_summary_json) AS je
                        WHERE UPPER(COALESCE(json_extract(je.value, '$.codec'), '')) IN ('DTS', 'DTS-HD')
                    )
                    """
                )
            elif codec_key == "OTHER":
                clauses.append(
                    """
                    EXISTS (
                        SELECT 1
                        FROM json_each(media_files.audio_summary_json) AS je
                        WHERE UPPER(COALESCE(json_extract(je.value, '$.codec'), '')) NOT IN ('EAC3', 'AC3', 'AAC', 'DTS', 'DTS-HD', 'TRUEHD')
                          AND COALESCE(json_extract(je.value, '$.codec'), '') != ''
                    )
                    """
                )
            else:
                clauses.append(
                    """
                    EXISTS (
                        SELECT 1
                        FROM json_each(media_files.audio_summary_json) AS je
                        WHERE UPPER(COALESCE(json_extract(je.value, '$.codec'), '')) = ?
                    )
                    """
                )
                params.append(codec_key)

        if aac51_only:
            clauses.append(
                "(scan_status = 'ready' AND has_aac_51 = 1 AND has_ac3_eac3 = 0)"
            )

        if aac51:
            clauses.append("(scan_status = 'ready' AND has_aac_51 = 1)")

        if has_metadata:
            clauses.append(
                "(scan_status = 'ready' AND (COALESCE(container_title, '') != '' OR has_global_tags = 1))"
            )

        if no_dolby:
            clauses.append("(scan_status = 'ready' AND has_ac3_eac3 = 0)")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params

    def list_files(
        self,
        *,
        q: str | None = None,
        folder: str | None = None,
        audio_type: str | None = None,
        audio_codec: str | None = None,
        aac51_only: bool = False,
        aac51: bool = False,
        has_metadata: bool = False,
        no_dolby: bool = False,
        sort: str = AppConstants.DEFAULT_SORT_FIELD,
        order: str = AppConstants.DEFAULT_SORT_ORDER,
        page: int = AppConstants.DEFAULT_PAGE,
        limit: int = AppConstants.DEFAULT_PAGE_LIMIT,
    ) -> tuple[list[dict[str, Any]], int]:
        """Query paginated, filtered media files without deserializing raw_json."""
        sort_col = (
            sort
            if sort in AppConstants.ALLOWED_SORT_FIELDS
            else AppConstants.DEFAULT_SORT_FIELD
        )
        sort_dir = (
            order.upper()
            if order.lower() in AppConstants.ALLOWED_SORT_ORDERS
            else AppConstants.DEFAULT_SORT_ORDER.upper()
        )
        safe_page = max(AppConstants.DEFAULT_PAGE, page)
        safe_limit = max(
            AppConstants.MIN_PAGE_LIMIT,
            min(AppConstants.MAX_PAGE_LIMIT, limit),
        )
        offset = (safe_page - 1) * safe_limit

        where_sql, params = self._build_filter_where(
            q=q,
            folder=folder,
            audio_type=audio_type,
            audio_codec=audio_codec,
            aac51_only=aac51_only,
            aac51=aac51,
            has_metadata=has_metadata,
            no_dolby=no_dolby,
        )

        with self._db.connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM media_files {where_sql}",
                params,
            ).fetchone()
            total = int(total_row["cnt"]) if total_row is not None else 0

            rows = conn.execute(
                f"""
                SELECT {self._LIST_COLUMNS}
                FROM media_files
                {where_sql}
                ORDER BY {sort_col} COLLATE NOCASE {sort_dir}, id ASC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, offset],
            ).fetchall()

        return [dict(row) for row in rows], total

    def list_folders(
        self,
        *,
        q: str | None = None,
        audio_type: str | None = None,
        aac51_only: bool = False,
        aac51: bool = False,
        has_metadata: bool = False,
        no_dolby: bool = False,
    ) -> list[str]:
        """Return distinct sorted folder paths matching all non-folder filters."""
        where_sql, params = self._build_filter_where(
            q=q,
            folder=None,
            audio_type=audio_type,
            aac51_only=aac51_only,
            aac51=aac51,
            has_metadata=has_metadata,
            no_dolby=no_dolby,
        )
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT folder FROM media_files {where_sql} ORDER BY folder COLLATE NOCASE ASC",
                params,
            ).fetchall()
        return [str(row["folder"]) for row in rows]

    def list_audio_types(
        self,
        *,
        q: str | None = None,
        folder: str | None = None,
        aac51_only: bool = False,
        aac51: bool = False,
        has_metadata: bool = False,
        no_dolby: bool = False,
    ) -> list[dict[str, Any]]:
        """Return distinct sorted audio types with file counts matching non-audio filters."""
        where_sql, params = self._build_filter_where(
            q=q,
            folder=folder,
            audio_type=None,
            aac51_only=aac51_only,
            aac51=aac51,
            has_metadata=has_metadata,
            no_dolby=no_dolby,
        )
        extra_pred = "COALESCE(json_extract(je.value, '$.codec'), '') != ''"
        combined_where = (
            f"{where_sql} AND {extra_pred}" if where_sql else f"WHERE {extra_pred}"
        )
        with self._db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    TRIM(
                        COALESCE(json_extract(je.value, '$.codec'), '') ||
                        CASE
                            WHEN COALESCE(json_extract(je.value, '$.channel_layout'), '') != ''
                            THEN ' ' || json_extract(je.value, '$.channel_layout')
                            ELSE ''
                        END
                    ) AS label,
                    COUNT(DISTINCT media_files.id) AS cnt
                FROM media_files, json_each(media_files.audio_summary_json) AS je
                {combined_where}
                GROUP BY label COLLATE NOCASE
                HAVING label != ''
                ORDER BY label COLLATE NOCASE ASC
                """,
                params,
            ).fetchall()
        return [
            {"label": str(row["label"]), "count": int(row["cnt"])}
            for row in rows
            if row["label"]
        ]

    def get_workflow_facet_counts(
        self,
        *,
        q: str | None = None,
        folder: str | None = None,
        audio_type: str | None = None,
    ) -> dict[str, int]:
        """Return cascading workflow chip counts for the current search/folder/audio view."""
        where_sql, params = self._build_filter_where(
            q=q,
            folder=folder,
            audio_type=audio_type,
        )
        with self._db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN scan_status = 'ready' AND has_aac_51 = 1 AND has_ac3_eac3 = 0 THEN 1 ELSE 0 END), 0) AS aac51_only,
                    COALESCE(SUM(CASE WHEN scan_status = 'ready' AND (COALESCE(container_title, '') != '' OR has_global_tags = 1) THEN 1 ELSE 0 END), 0) AS has_metadata,
                    COALESCE(SUM(CASE WHEN scan_status = 'ready' AND has_ac3_eac3 = 0 THEN 1 ELSE 0 END), 0) AS no_dolby
                FROM media_files
                {where_sql}
                """,
                params,
            ).fetchone()

        if row is None:
            return {"aac51_only": 0, "has_metadata": 0, "no_dolby": 0}
        return {
            "aac51_only": int(row["aac51_only"]),
            "has_metadata": int(row["has_metadata"]),
            "no_dolby": int(row["no_dolby"]),
        }

    def get_dashboard_counts(self) -> dict[str, int]:
        """Return aggregate Dashboard counts in a single SQL query."""
        with self._db.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_files,
                    COALESCE(SUM(CASE WHEN scan_status = 'ready' AND has_aac_51 = 1 AND has_ac3_eac3 = 0 THEN 1 ELSE 0 END), 0) AS aac51_only,
                    COALESCE(SUM(CASE WHEN scan_status = 'ready' AND (COALESCE(container_title, '') != '' OR has_global_tags = 1) THEN 1 ELSE 0 END), 0) AS has_metadata
                FROM media_files
                """
            ).fetchone()

        if row is None:
            return {"total_files": 0, "aac51_only": 0, "has_metadata": 0}

        return {
            "total_files": int(row["total_files"]),
            "aac51_only": int(row["aac51_only"]),
            "has_metadata": int(row["has_metadata"]),
        }

    def get_dashboard_analytics(self) -> dict[str, Any]:
        """Aggregate container sizes, audio codec track counts, and resolution/HDR pills.

        Strictly excludes `scan_status != 'ready'` rows from codec/resolution/HDR metrics.
        """
        with self._db.connect() as conn:
            container_row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN LOWER(filename) LIKE '%.mkv' THEN size_bytes ELSE 0 END), 0) AS mkv_bytes,
                    COALESCE(SUM(CASE WHEN LOWER(filename) LIKE '%.mkv' THEN 1 ELSE 0 END), 0) AS mkv_count,
                    COALESCE(SUM(CASE WHEN LOWER(filename) LIKE '%.mp4' THEN size_bytes ELSE 0 END), 0) AS mp4_bytes,
                    COALESCE(SUM(CASE WHEN LOWER(filename) LIKE '%.mp4' THEN 1 ELSE 0 END), 0) AS mp4_count,
                    COALESCE(SUM(size_bytes), 0) AS media_bytes
                FROM media_files
                """
            ).fetchone()

            codec_rows = conn.execute(
                """
                SELECT
                    CASE
                        WHEN UPPER(COALESCE(json_extract(je.value, '$.codec'), '')) = 'EAC3' THEN 'EAC3'
                        WHEN UPPER(COALESCE(json_extract(je.value, '$.codec'), '')) = 'AC3' THEN 'AC3'
                        WHEN UPPER(COALESCE(json_extract(je.value, '$.codec'), '')) = 'AAC' THEN 'AAC'
                        WHEN UPPER(COALESCE(json_extract(je.value, '$.codec'), '')) IN ('DTS', 'DTS-HD') THEN 'DTS'
                        WHEN UPPER(COALESCE(json_extract(je.value, '$.codec'), '')) = 'TRUEHD' THEN 'TrueHD'
                        ELSE 'Other'
                    END AS codec_family,
                    COUNT(*) AS cnt
                FROM media_files, json_each(media_files.audio_summary_json) AS je
                WHERE media_files.scan_status = 'ready'
                  AND COALESCE(json_extract(je.value, '$.codec'), '') != ''
                GROUP BY codec_family
                """
            ).fetchall()

            res_rows = conn.execute(
                """
                SELECT resolution, COUNT(*) AS cnt
                FROM media_files
                WHERE scan_status = 'ready' AND COALESCE(resolution, '') != ''
                GROUP BY resolution
                """
            ).fetchall()

            dr_rows = conn.execute(
                """
                SELECT video_dynamic_range AS dr, COUNT(*) AS cnt
                FROM media_files
                WHERE scan_status = 'ready' AND COALESCE(video_dynamic_range, '') != ''
                GROUP BY video_dynamic_range
                """
            ).fetchall()

        mkv_bytes = int(container_row["mkv_bytes"]) if container_row else 0
        mkv_count = int(container_row["mkv_count"]) if container_row else 0
        mp4_bytes = int(container_row["mp4_bytes"]) if container_row else 0
        mp4_count = int(container_row["mp4_count"]) if container_row else 0
        media_bytes = int(container_row["media_bytes"]) if container_row else 0

        codec_counts: dict[str, int] = {
            str(r["codec_family"]): int(r["cnt"]) for r in codec_rows
        }
        res_counts: dict[str, int] = {
            str(r["resolution"]): int(r["cnt"]) for r in res_rows
        }
        dr_counts: dict[str, int] = {
            str(r["dr"]): int(r["cnt"]) for r in dr_rows
        }

        return {
            "mkv_bytes": mkv_bytes,
            "mkv_count": mkv_count,
            "mp4_bytes": mp4_bytes,
            "mp4_count": mp4_count,
            "media_bytes": media_bytes,
            "codec_counts": codec_counts,
            "res_counts": res_counts,
            "dr_counts": dr_counts,
        }

    def list_direct_file_states(
        self, folder: str
    ) -> dict[str, tuple[int, int, float, str]]:
        """Return `{filepath: (id, size_bytes, mtime, scan_status)}` for files directly inside `folder`."""
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, filepath, size_bytes, mtime, scan_status
                FROM media_files
                WHERE folder = ?
                """,
                (folder,),
            ).fetchall()
        return {
            str(row["filepath"]): (
                int(row["id"]),
                int(row["size_bytes"]),
                float(row["mtime"]),
                str(row["scan_status"] or AppConstants.FILE_SCAN_STATUS_READY),
            )
            for row in rows
        }

    def backfill_video_dynamic_range(
        self, classifier_fn: Any, *, force: bool = False
    ) -> int:
        """Backfill `video_dynamic_range` from cached `raw_json` with zero disk I/O."""
        where_clause = (
            "WHERE COALESCE(video_codec, '') != ''"
            if force
            else "WHERE video_dynamic_range IS NULL AND COALESCE(video_codec, '') != ''"
        )
        with self._db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, raw_json
                FROM media_files
                {where_clause}
                """
            ).fetchall()
            if not rows:
                return 0

            updates: list[tuple[str, int]] = []
            for row in rows:
                dr: str | None = None
                raw_text = str(row["raw_json"] or "{}")
                try:
                    payload = json.loads(raw_text)
                except json.JSONDecodeError:
                    payload = {}
                if isinstance(payload, dict):
                    dr = classifier_fn(payload)
                updates.append(
                    (dr or AppConstants.DYNAMIC_RANGE_SDR, int(row["id"]))
                )

            conn.executemany(
                "UPDATE media_files SET video_dynamic_range = ? WHERE id = ?",
                updates,
            )
        return len(updates)


class JobRepository:
    """SQLite persistence and state transitions for queued/running/historical jobs."""

    _JOB_COLUMNS: str = """
        id, filepath, output_filepath, status, config_json, stage, progress,
        warnings_json, error_message, created_at, completed_at,
        created_by_user_id, created_by_name
    """

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def create_job(
        self,
        *,
        job_id: str,
        filepath: str,
        config_json: str,
        created_by_user_id: int | None = None,
        created_by_name: str = AppConstants.AUTH_ANONYMOUS_DISPLAY_NAME,
    ) -> dict[str, Any]:
        """Insert a new queued job row and return it."""
        now = utc_now_iso()
        author_name = (
            created_by_name.strip()
            if isinstance(created_by_name, str) and created_by_name.strip()
            else AppConstants.AUTH_ANONYMOUS_DISPLAY_NAME
        )
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, filepath, output_filepath, status, config_json,
                    stage, progress, warnings_json, error_message,
                    created_at, completed_at, created_by_user_id, created_by_name
                )
                VALUES (?, ?, NULL, ?, ?, NULL, 0, NULL, NULL, ?, NULL, ?, ?)
                """,
                (
                    job_id,
                    filepath,
                    AppConstants.JOB_STATUS_QUEUED,
                    config_json,
                    now,
                    created_by_user_id,
                    author_name,
                ),
            )
            row = conn.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def get_by_id(self, job_id: str) -> dict[str, Any] | None:
        """Fetch a single job row by UUID."""
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_active_job_for_filepath(self, filepath: str) -> dict[str, Any] | None:
        """Return an existing queued or running job for `filepath`, if any."""
        with self._db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT {self._JOB_COLUMNS}
                FROM jobs
                WHERE filepath = ? AND status IN (?, ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    filepath,
                    AppConstants.JOB_STATUS_QUEUED,
                    AppConstants.JOB_STATUS_RUNNING,
                ),
            ).fetchone()
        return None if row is None else dict(row)

    def get_active_job_in_folder(self, display_folder: str) -> dict[str, Any] | None:
        """Return any queued or running job targeting a file inside `display_folder`."""
        rel_prefix = display_folder.strip("/")
        if not rel_prefix:
            with self._db.connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT {self._JOB_COLUMNS}
                    FROM jobs
                    WHERE status IN (?, ?)
                    LIMIT 1
                    """,
                    (AppConstants.JOB_STATUS_QUEUED, AppConstants.JOB_STATUS_RUNNING),
                ).fetchone()
            return None if row is None else dict(row)

        like_pattern = f"{_escape_like(rel_prefix)}/%"
        with self._db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT {self._JOB_COLUMNS}
                FROM jobs
                WHERE status IN (?, ?)
                  AND (filepath = ? OR filepath LIKE ? ESCAPE '\\')
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    AppConstants.JOB_STATUS_QUEUED,
                    AppConstants.JOB_STATUS_RUNNING,
                    rel_prefix,
                    like_pattern,
                ),
            ).fetchone()
        return None if row is None else dict(row)

    def claim_next_queued_job(self) -> dict[str, Any] | None:
        """Atomically claim the oldest queued job and mark it running."""
        with self._db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT {self._JOB_COLUMNS}
                FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """,
                (AppConstants.JOB_STATUS_QUEUED,),
            ).fetchone()
            if row is None:
                return None

            job_id = str(row["id"])
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, progress = 0, stage = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    AppConstants.JOB_STATUS_RUNNING,
                    job_id,
                    AppConstants.JOB_STATUS_QUEUED,
                ),
            )
            updated = conn.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return None if updated is None else dict(updated)

    def update_progress(
        self,
        job_id: str,
        *,
        stage: str | None,
        progress: int,
    ) -> None:
        """Persist current stage and percentage for a running job."""
        bounded = max(0, min(100, int(progress)))
        with self._db.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET stage = ?, progress = ?
                WHERE id = ? AND status = ?
                """,
                (stage, bounded, job_id, AppConstants.JOB_STATUS_RUNNING),
            )

    def mark_completed(
        self,
        job_id: str,
        *,
        output_filepath: str,
        warnings: list[str] | None = None,
    ) -> None:
        """Mark a job completed with output path and optional warnings."""
        warnings_json = (
            json.dumps(warnings, separators=(",", ":")) if warnings else None
        )
        now = utc_now_iso()
        with self._db.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, output_filepath = ?, progress = 100,
                    warnings_json = ?, error_message = NULL, completed_at = ?
                WHERE id = ?
                """,
                (
                    AppConstants.JOB_STATUS_COMPLETED,
                    output_filepath,
                    warnings_json,
                    now,
                    job_id,
                ),
            )

    def mark_failed(self, job_id: str, *, error_message: str) -> None:
        """Mark a job failed with a persisted error message."""
        now = utc_now_iso()
        with self._db.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, completed_at = ?
                WHERE id = ?
                """,
                (AppConstants.JOB_STATUS_FAILED, error_message, now, job_id),
            )

    def mark_cancelled(
        self,
        job_id: str,
        *,
        error_message: str = AppConstants.CANCELLED_BY_USER_MESSAGE,
    ) -> bool:
        """Mark a queued or running job cancelled; returns True if updated."""
        now = utc_now_iso()
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, completed_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    AppConstants.JOB_STATUS_CANCELLED,
                    error_message,
                    now,
                    job_id,
                    AppConstants.JOB_STATUS_QUEUED,
                    AppConstants.JOB_STATUS_RUNNING,
                ),
            )
            return cursor.rowcount > 0

    def fail_stale_running_jobs(
        self,
        error_message: str = AppConstants.CRASH_RECOVERY_ERROR_MESSAGE,
    ) -> int:
        """Mark any leftover `running` jobs as `failed` after a container restart."""
        now = utc_now_iso()
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, completed_at = ?
                WHERE status = ?
                """,
                (
                    AppConstants.JOB_STATUS_FAILED,
                    error_message,
                    now,
                    AppConstants.JOB_STATUS_RUNNING,
                ),
            )
            return int(cursor.rowcount)

    def get_active(self) -> dict[str, Any] | None:
        """Return the currently running job, if any."""
        with self._db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT {self._JOB_COLUMNS}
                FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (AppConstants.JOB_STATUS_RUNNING,),
            ).fetchone()
        return None if row is None else dict(row)

    def list_queued(self) -> list[dict[str, Any]]:
        """Return all queued jobs in FIFO execution order."""
        with self._db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {self._JOB_COLUMNS}
                FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (AppConstants.JOB_STATUS_QUEUED,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_history(
        self, limit: int = AppConstants.JOB_HISTORY_LIMIT
    ) -> list[dict[str, Any]]:
        """Return completed, failed, and cancelled jobs newest first."""
        safe_limit = max(1, min(AppConstants.MAX_PAGE_LIMIT, limit))
        with self._db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {self._JOB_COLUMNS}
                FROM jobs
                WHERE status IN (?, ?, ?)
                ORDER BY COALESCE(completed_at, created_at) DESC, rowid DESC
                LIMIT ?
                """,
                (
                    AppConstants.JOB_STATUS_COMPLETED,
                    AppConstants.JOB_STATUS_FAILED,
                    AppConstants.JOB_STATUS_CANCELLED,
                    safe_limit,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_history_item(self, job_id: str) -> bool:
        """Delete one terminal job history row (`completed`/`failed`/`cancelled`)."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE id = ? AND status IN (?, ?, ?)
                """,
                (
                    job_id,
                    AppConstants.JOB_STATUS_COMPLETED,
                    AppConstants.JOB_STATUS_FAILED,
                    AppConstants.JOB_STATUS_CANCELLED,
                ),
            )
            return cursor.rowcount > 0

    def clear_history(self) -> int:
        """Delete all terminal job history rows while preserving queued/running jobs."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE status IN (?, ?, ?)
                """,
                (
                    AppConstants.JOB_STATUS_COMPLETED,
                    AppConstants.JOB_STATUS_FAILED,
                    AppConstants.JOB_STATUS_CANCELLED,
                ),
            )
            return int(cursor.rowcount)

    def count_terminal_jobs(self) -> int:
        """Return count of completed, failed, and cancelled job rows."""
        with self._db.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM jobs
                WHERE status IN (?, ?, ?)
                """,
                (
                    AppConstants.JOB_STATUS_COMPLETED,
                    AppConstants.JOB_STATUS_FAILED,
                    AppConstants.JOB_STATUS_CANCELLED,
                ),
            ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def prune_terminal_jobs_older_than(self, cutoff_iso: str) -> int:
        """Delete terminal job rows older than `cutoff_iso`, never touching active/queued."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE status IN (?, ?, ?)
                  AND COALESCE(completed_at, created_at) < ?
                """,
                (
                    AppConstants.JOB_STATUS_COMPLETED,
                    AppConstants.JOB_STATUS_FAILED,
                    AppConstants.JOB_STATUS_CANCELLED,
                    cutoff_iso,
                ),
            )
            return int(cursor.rowcount)

    def list_active_and_queued_configs(self) -> list[str]:
        """Return config_json payloads for all queued and running jobs."""
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT config_json
                FROM jobs
                WHERE status IN (?, ?)
                """,
                (
                    AppConstants.JOB_STATUS_QUEUED,
                    AppConstants.JOB_STATUS_RUNNING,
                ),
            ).fetchall()
        return [str(row["config_json"]) for row in rows if row["config_json"]]

    def list_recent(
        self, limit: int = AppConstants.DEFAULT_RECENT_JOBS_LIMIT
    ) -> list[dict[str, Any]]:
        """Return most recent jobs ordered by creation time descending."""
        safe_limit = max(1, min(AppConstants.MAX_PAGE_LIMIT, limit))
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, filepath, output_filepath, status, stage, progress,
                       error_message, created_at, completed_at, created_by_name
                FROM jobs
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]


class AuthRepository:
    """SQLite persistence for local users and hashed session tokens."""

    _USER_COLUMNS: str = """
        id, username, display_name, password_hash, role,
        is_anonymous, preferences_json, created_at
    """

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def get_or_create_anonymous_user(self) -> dict[str, Any]:
        """Return the built-in Anonymous user record, creating it idempotently."""
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {self._USER_COLUMNS} FROM users WHERE is_anonymous = 1 LIMIT 1"
            ).fetchone()
            if row is not None:
                return dict(row)

            now = utc_now_iso()
            conn.execute(
                """
                INSERT OR IGNORE INTO users (
                    username, display_name, password_hash, role,
                    is_anonymous, preferences_json, created_at
                )
                VALUES (?, ?, '', ?, 1, '{}', ?)
                """,
                (
                    AppConstants.AUTH_ANONYMOUS_USERNAME,
                    AppConstants.AUTH_ANONYMOUS_DISPLAY_NAME,
                    AppConstants.AUTH_ROLE_ADMIN,
                    now,
                ),
            )
            created = conn.execute(
                f"SELECT {self._USER_COLUMNS} FROM users WHERE is_anonymous = 1 LIMIT 1"
            ).fetchone()
        assert created is not None
        return dict(created)

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password_hash: str,
        role: str = AppConstants.AUTH_ROLE_ADMIN,
        preferences_json: str = "{}",
    ) -> dict[str, Any]:
        """Insert a new non-anonymous local user and return the row."""
        now = utc_now_iso()
        normalized_username = username.strip().lower()
        clean_display = display_name.strip() or normalized_username
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (
                    username, display_name, password_hash, role,
                    is_anonymous, preferences_json, created_at
                )
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    normalized_username,
                    clean_display,
                    password_hash,
                    role,
                    preferences_json,
                    now,
                ),
            )
            row = conn.execute(
                f"SELECT {self._USER_COLUMNS} FROM users WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
        assert row is not None
        return dict(row)

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Fetch one user by primary key."""
        with self._db.connect() as conn:
            row = conn.execute(
                f"SELECT {self._USER_COLUMNS} FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Fetch one non-anonymous user by lowercase username."""
        normalized = username.strip().lower()
        if not normalized:
            return None
        with self._db.connect() as conn:
            row = conn.execute(
                f"""
                SELECT {self._USER_COLUMNS}
                FROM users
                WHERE username = ? AND is_anonymous = 0
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return None if row is None else dict(row)

    def list_local_users(self) -> list[dict[str, Any]]:
        """List all non-anonymous local accounts ordered by creation time."""
        with self._db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {self._USER_COLUMNS}
                FROM users
                WHERE is_anonymous = 0
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def count_local_users(self) -> int:
        """Return number of non-anonymous local accounts."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE is_anonymous = 0"
            ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def update_password_hash(self, user_id: int, password_hash: str) -> bool:
        """Update a local user's scrypt password hash."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ? AND is_anonymous = 0",
                (password_hash, user_id),
            )
            return cursor.rowcount > 0

    def get_user_preferences_json(self, user_id: int) -> str:
        """Return stored preferences_json for `user_id`, defaulting to '{}'."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT preferences_json FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None or not row["preferences_json"]:
            return "{}"
        return str(row["preferences_json"])

    def set_user_preferences_json(self, user_id: int, preferences_json: str) -> bool:
        """Persist updated preferences_json for `user_id`."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET preferences_json = ? WHERE id = ?",
                (preferences_json, user_id),
            )
            return cursor.rowcount > 0

    def delete_user(self, user_id: int) -> bool:
        """Delete a non-anonymous user (cascading its sessions)."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM users WHERE id = ? AND is_anonymous = 0",
                (user_id,),
            )
            return cursor.rowcount > 0

    def create_session(
        self,
        *,
        token_hash: str,
        user_id: int,
        expires_at: str,
    ) -> None:
        """Store a new hashed session token in SQLite."""
        now = utc_now_iso()
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (token_hash, user_id, expires_at, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, expires_at, now, now),
            )

    def get_session_with_user(self, token_hash: str) -> dict[str, Any] | None:
        """Return joined session + user row for a hashed token, or None."""
        with self._db.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    s.token_hash, s.user_id, s.expires_at, s.created_at AS session_created_at,
                    s.last_seen_at,
                    u.id, u.username, u.display_name, u.password_hash, u.role,
                    u.is_anonymous, u.preferences_json, u.created_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND u.is_anonymous = 0
                LIMIT 1
                """,
                (token_hash,),
            ).fetchone()
        return None if row is None else dict(row)

    def touch_session(self, token_hash: str, new_expires_at: str) -> None:
        """Extend sliding session expiry and update last_seen_at."""
        now = utc_now_iso()
        with self._db.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET expires_at = ?, last_seen_at = ?
                WHERE token_hash = ?
                """,
                (new_expires_at, now, token_hash),
            )

    def delete_session(self, token_hash: str) -> None:
        """Revoke a single session token."""
        with self._db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_all_sessions(self) -> int:
        """Revoke all active sessions."""
        with self._db.connect() as conn:
            cursor = conn.execute("DELETE FROM sessions")
            return int(cursor.rowcount)

    def delete_expired_sessions(self, now_iso: str) -> int:
        """Prune sessions whose expires_at is in the past."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?",
                (now_iso,),
            )
            return int(cursor.rowcount)

    def create_api_key(
        self,
        *,
        user_id: int,
        name: str,
        key_prefix: str,
        key_hash: str,
    ) -> dict[str, Any]:
        """Store a new hashed API key for a user and return the row."""
        now = utc_now_iso()
        with self._db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO api_keys (user_id, name, key_prefix, key_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, name, key_prefix, key_hash, now),
            )
            row = conn.execute(
                """
                SELECT id, user_id, name, key_prefix, created_at, last_used_at
                FROM api_keys
                WHERE id = ?
                """,
                (int(cursor.lastrowid),),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_api_keys(self, user_id: int) -> list[dict[str, Any]]:
        """Return all active API keys for a specific user ordered by creation descending."""
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, name, key_prefix, created_at, last_used_at
                FROM api_keys
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_api_key_with_user(self, key_hash: str) -> dict[str, Any] | None:
        """Return joined api_key + user record for a hashed token."""
        with self._db.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    k.id AS api_key_id, k.name AS api_key_name, k.key_prefix,
                    k.created_at AS api_key_created_at, k.last_used_at,
                    u.id, u.username, u.display_name, u.password_hash, u.role,
                    u.is_anonymous, u.preferences_json, u.created_at
                FROM api_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.key_hash = ? AND u.is_anonymous = 0
                LIMIT 1
                """,
                (key_hash,),
            ).fetchone()
        return None if row is None else dict(row)

    def touch_api_key(self, api_key_id: int) -> None:
        """Update last_used_at timestamp on successful API key authentication."""
        now = utc_now_iso()
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (now, api_key_id),
            )

    def delete_api_key(self, api_key_id: int, user_id: int) -> bool:
        """Delete an API key belonging to a specific user."""
        with self._db.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM api_keys WHERE id = ? AND user_id = ?",
                (api_key_id, user_id),
            )
            return cursor.rowcount > 0


def decode_warnings_json(raw_json: str | None) -> list[str]:
    """Safely deserialize persisted job warnings JSON."""
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def decode_audio_summary_json(raw_json: str | None) -> list[dict[str, Any]]:
    """Safely deserialize denormalized audio_summary_json."""
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []
