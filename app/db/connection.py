"""SQLite WAL connection management and schema initialization."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import AppConstants

_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    duration_us INTEGER NOT NULL DEFAULT 0,
    resolution TEXT,
    video_codec TEXT,
    video_dynamic_range TEXT,
    audio_summary_json TEXT NOT NULL DEFAULT '[]',
    subtitle_count INTEGER NOT NULL DEFAULT 0,
    container_title TEXT NOT NULL DEFAULT '',
    has_global_tags INTEGER NOT NULL DEFAULT 0,
    has_aac_51 INTEGER NOT NULL DEFAULT 0,
    has_ac3_eac3 INTEGER NOT NULL DEFAULT 0,
    has_cover_art INTEGER NOT NULL DEFAULT 0,
    scan_status TEXT NOT NULL DEFAULT 'ready',
    raw_json TEXT NOT NULL,
    scanned_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_files_folder
    ON media_files(folder);
CREATE INDEX IF NOT EXISTS idx_media_files_has_aac_51
    ON media_files(has_aac_51);
CREATE INDEX IF NOT EXISTS idx_media_files_has_ac3_eac3
    ON media_files(has_ac3_eac3);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filepath TEXT NOT NULL,
    output_filepath TEXT,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    stage TEXT,
    progress INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    created_by_user_id INTEGER,
    created_by_name TEXT NOT NULL DEFAULT 'Anonymous',
    batch_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_filepath
    ON jobs(filepath);
CREATE INDEX IF NOT EXISTS idx_jobs_status
    ON jobs(status);

CREATE TABLE IF NOT EXISTS job_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    rule_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'admin',
    is_anonymous INTEGER NOT NULL DEFAULT 0,
    preferences_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id
    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
    ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user_id
    ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash
    ON api_keys(key_hash);
"""


class DatabaseManager:
    """Manages SQLite connections in WAL mode and initializes schema."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        """Return configured SQLite database file path."""
        return self._db_path

    def initialize(self) -> None:
        """Create database directory, enable WAL mode, and apply schema + migrations."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.executescript(_SCHEMA_SQL)
            self._apply_migrations(conn)

    @staticmethod
    def _apply_migrations(conn: sqlite3.Connection) -> None:
        """Idempotently add newer columns to existing SQLite databases."""
        media_cols = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(media_files)").fetchall()
        }
        if "video_dynamic_range" not in media_cols:
            conn.execute("ALTER TABLE media_files ADD COLUMN video_dynamic_range TEXT")
        if "scan_status" not in media_cols:
            conn.execute(
                "ALTER TABLE media_files ADD COLUMN scan_status TEXT NOT NULL DEFAULT 'ready'"
            )
        if "has_cover_art" not in media_cols:
            conn.execute(
                "ALTER TABLE media_files ADD COLUMN has_cover_art INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                """
                UPDATE media_files
                SET has_cover_art = 1
                WHERE json_valid(raw_json)
                  AND EXISTS (
                      SELECT 1
                      FROM json_each(media_files.raw_json, '$.attachments') AS att
                      WHERE LOWER(COALESCE(json_extract(att.value, '$.content_type'), '')) LIKE 'image/%'
                         OR LOWER(COALESCE(json_extract(att.value, '$.file_name'), '')) LIKE '%.jpg'
                         OR LOWER(COALESCE(json_extract(att.value, '$.file_name'), '')) LIKE '%.jpeg'
                         OR LOWER(COALESCE(json_extract(att.value, '$.file_name'), '')) LIKE '%.png'
                         OR LOWER(COALESCE(json_extract(att.value, '$.file_name'), '')) LIKE '%.webp'
                         OR LOWER(COALESCE(json_extract(att.value, '$.file_name'), '')) LIKE '%.gif'
                  )
                """
            )

        job_cols = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "created_by_user_id" not in job_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN created_by_user_id INTEGER")
        if "created_by_name" not in job_cols:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN created_by_name TEXT NOT NULL DEFAULT 'Anonymous'"
            )
        if "batch_id" not in job_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN batch_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_batch_id ON jobs(batch_id)")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured SQLite connection with automatic transaction handling."""
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=AppConstants.SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute(f"PRAGMA busy_timeout={AppConstants.SQLITE_BUSY_TIMEOUT_MS};")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
