"""
db_manager.py — SQLite persistence with a normalized schema.

Three tables, two relationships:
    videos    — one row per VideoRecord
    comments  — one row per CommentRecord (flattened; self-referencing
                parent_comment_id preserves the reply tree), FK -> videos
    chapters  — one row per ChapterRecord (video timeline/chapter markers),
                FK -> videos. Uses a composite primary key of
                (video_id, chapter_index) since yt-dlp does not assign
                chapters their own stable ID the way it does for comments
                (comment_id comes from YouTube itself; chapters are just
                a plain ordered list) — chapter_index (1-based position
                within that video) is the natural, stable substitute.

Why normalized instead of one denormalized table: with comments up to
--comments-limit per video, a flat "one row per comment with all video
columns repeated" table would duplicate every video's metadata potentially
thousands of times, which is both wasteful and makes "how many videos did
I extract" a DISTINCT query instead of a COUNT. Three tables + joins is the
standard, correct shape for this data.

Batching: this module never holds the whole dataset in memory on the DB
side. `DBWriter.write_batch()` is called once per chunk (see output_writers
.ChunkedOutputCoordinator) and commits after each chunk, so a crash mid-run
loses at most one chunk, not the whole run.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .exceptions import DatabaseError
from .models import ChapterRecord, CommentRecord, VideoRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    title               TEXT,
    description         TEXT,
    views               INTEGER,
    likes               INTEGER,
    dislikes            INTEGER,          -- always NULL; see models.py docstring
    duration            INTEGER,
    upload_date         TEXT,
    release_date        TEXT,
    channel_name        TEXT,
    channel_id          TEXT,
    channel_url         TEXT,
    subscriber_count    INTEGER,
    thumbnail_url       TEXT,
    tags                TEXT,             -- JSON-encoded list
    categories          TEXT,             -- JSON-encoded list
    language            TEXT,
    is_live             INTEGER,          -- 0/1
    is_age_restricted   INTEGER,          -- 0/1
    comment_count       INTEGER,
    share_link          TEXT,
    webpage_url         TEXT,
    playlist_title      TEXT,
    playlist_index      INTEGER,
    status              TEXT,
    error_message       TEXT,
    extracted_at        TEXT,
    source_url          TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id          TEXT PRIMARY KEY,
    video_id            TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    parent_comment_id   TEXT,             -- 'root' for top-level comments
    author              TEXT,
    author_channel_id   TEXT,
    text                TEXT,
    like_count          INTEGER,
    reply_count         INTEGER,
    is_favorited        INTEGER,
    is_pinned           INTEGER,
    timestamp           INTEGER
);

CREATE TABLE IF NOT EXISTS chapters (
    video_id            TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    chapter_index       INTEGER NOT NULL,  -- 1-based position within this video's chapter list
    start_time          REAL NOT NULL,     -- seconds; REAL not INTEGER since yt-dlp allows fractional timestamps
    end_time            REAL,              -- seconds; NULL for an open-ended final chapter
    title               TEXT,
    PRIMARY KEY (video_id, chapter_index)
);

CREATE INDEX IF NOT EXISTS idx_comments_video_id ON comments(video_id);
CREATE INDEX IF NOT EXISTS idx_chapters_video_id ON chapters(video_id);
CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
"""

_VIDEO_COLUMNS = (
    "video_id", "title", "description", "views", "likes", "dislikes", "duration",
    "upload_date", "release_date", "channel_name", "channel_id", "channel_url",
    "subscriber_count", "thumbnail_url", "tags", "categories", "language",
    "is_live", "is_age_restricted", "comment_count", "share_link", "webpage_url",
    "playlist_title", "playlist_index", "status", "error_message", "extracted_at",
    "source_url",
)

_COMMENT_COLUMNS = (
    "comment_id", "video_id", "parent_comment_id", "author", "author_channel_id",
    "text", "like_count", "reply_count", "is_favorited", "is_pinned", "timestamp",
)

_CHAPTER_COLUMNS = ("video_id", "chapter_index", "start_time", "end_time", "title")


def _video_row(record: VideoRecord) -> tuple:
    return (
        record.video_id,
        record.title,
        record.description,
        record.views,
        record.likes,
        record.dislikes,
        record.duration,
        record.upload_date,
        record.release_date,
        record.channel_name,
        record.channel_id,
        record.channel_url,
        record.subscriber_count,
        record.thumbnail_url,
        json.dumps(record.tags, ensure_ascii=False),
        json.dumps(record.categories, ensure_ascii=False),
        record.language,
        int(record.is_live),
        int(record.is_age_restricted),
        record.comment_count,
        record.share_link,
        record.webpage_url,
        record.playlist_title,
        record.playlist_index,
        record.status.value,
        record.error_message,
        record.extracted_at,
        record.source_url,
    )


def _comment_rows(video_id: str, comments: Iterable[CommentRecord]) -> list[tuple]:
    rows: list[tuple] = []
    for top in comments:
        for c in top.flatten():
            rows.append(
                (
                    c.comment_id,
                    video_id,
                    c.parent_id,
                    c.author,
                    c.author_channel_id,
                    c.text,
                    c.like_count,
                    c.reply_count,
                    int(c.is_favorited),
                    int(c.is_pinned),
                    c.timestamp,
                )
            )
    return rows


def _chapter_rows(video_id: str, chapters: Iterable[ChapterRecord]) -> list[tuple]:
    rows: list[tuple] = []
    for idx, ch in enumerate(chapters, start=1):
        rows.append((video_id, idx, ch.start_time, ch.end_time, ch.title))
    return rows


class DBWriter:
    """Owns one SQLite connection for the lifetime of a run. Thread-safe:
    a single lock serializes writes, since SQLite itself only supports one
    writer at a time regardless — this just makes that explicit instead of
    relying on SQLite's own busy-timeout/retry behavior under concurrent
    threads, which would otherwise risk `sqlite3.OperationalError: database
    is locked` under the thread pool's concurrent completions.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to initialize SQLite database at '{db_path}': {exc}") from exc

    def write_batch(self, records: list[VideoRecord]) -> None:
        """Insert/replace a batch of VideoRecords (and their comments) in one
        transaction. Uses INSERT OR REPLACE so re-running the tool against
        the same DB (e.g. after a crash, to pick up where it left off)
        updates rather than duplicates rows.
        """
        if not records:
            return

        video_placeholders = ", ".join("?" for _ in _VIDEO_COLUMNS)
        comment_placeholders = ", ".join("?" for _ in _COMMENT_COLUMNS)
        chapter_placeholders = ", ".join("?" for _ in _CHAPTER_COLUMNS)

        video_sql = f"INSERT OR REPLACE INTO videos ({', '.join(_VIDEO_COLUMNS)}) VALUES ({video_placeholders})"
        comment_sql = f"INSERT OR REPLACE INTO comments ({', '.join(_COMMENT_COLUMNS)}) VALUES ({comment_placeholders})"
        chapter_sql = f"INSERT OR REPLACE INTO chapters ({', '.join(_CHAPTER_COLUMNS)}) VALUES ({chapter_placeholders})"

        video_rows = [_video_row(r) for r in records]
        comment_rows: list[tuple] = []
        chapter_rows: list[tuple] = []
        for r in records:
            comment_rows.extend(_comment_rows(r.video_id, r.comments))
            chapter_rows.extend(_chapter_rows(r.video_id, r.chapters))

        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._conn.executemany(video_sql, video_rows)
                if comment_rows:
                    self._conn.executemany(comment_sql, comment_rows)
                if chapter_rows:
                    self._conn.executemany(chapter_sql, chapter_rows)
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise DatabaseError(f"Failed to write batch of {len(records)} record(s) to SQLite: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "DBWriter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
