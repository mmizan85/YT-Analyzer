"""
Canonical data models for yt-analyzer.

Every other module (extractor, processor, writers, db) imports these
dataclasses instead of passing raw dicts around. This is the single
source of truth for field names, so `--fields title,views` and the
JSON/CSV/XLSX/DB column headers can never silently drift out of sync.

IMPORTANT — a note on fields that DO NOT reliably exist upstream:
    * YouTube stopped exposing public dislike counts in December 2021.
      There is no API, page payload, or yt-dlp field that returns it.
      `dislike_count` is kept in the schema (per the client blueprint's
      "like and dislike" requirement) but will always be populated as
      `None`, with `is_estimated=False`, so downstream consumers can
      tell "we don't have this" apart from "this video has 0 dislikes".
    * `like_count` itself is best-effort. YouTube periodically changes
      the internal button payload yt-dlp scrapes it from; when parsing
      fails upstream, yt-dlp itself returns None for it, and we pass
      that through rather than defaulting to 0 (0 is a real value).
    * `language` is only populated when the uploader explicitly set a
      metadata language tag or auto-caption language is detected; most
      videos do not have this set, so it is frequently None.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class ExtractionStatus(str, Enum):
    """Outcome of attempting to extract a single item."""

    SUCCESS = "success"
    FAILED_UNAVAILABLE = "failed_unavailable"   # private / deleted / region-blocked
    FAILED_COMMENTS_DISABLED = "failed_comments_disabled"
    FAILED_RATE_LIMITED = "failed_rate_limited"
    FAILED_NETWORK = "failed_network"
    FAILED_UNKNOWN = "failed_unknown"


# Canonical field names exposed via --fields. Keys are the user-facing
# names; values are short human descriptions used in --help / interactive
# mode. This dict is also what UnsupportedFieldError validates against.
AVAILABLE_FIELDS_CATEGORIZED: dict[str, dict[str, str]] = {
    "📊 Core Identifiers & Metadata": {
        "video_id": "The 11-character YouTube video ID",
        "title": "Video title",
        "description": "Full video description text",
        "thumbnail_url": "URL of the highest-resolution thumbnail available",
        "share_link": "Canonical short share URL (youtu.be/<id>)",
        "webpage_url": "Full canonical watch-page URL",
    },
    "📈 Engagement Metrics": {
        "views": "View count (maps to yt-dlp's view_count)",
        "likes": "Like count, best-effort (maps to yt-dlp's like_count)",
        "dislikes": "Dislike count — always null (Removed by YouTube in 2021)",
        "comment_count": "Total number of comments reported by YouTube",
    },
    "⏱️ Time & Structural Data": {
        "duration": "Duration in seconds",
        "timeline": "Alias for duration + upload/release timestamps as a compact block",
        "chapters": "Video chapter markers (start_time, end_time, title)",
        "upload_date": "Upload date, ISO-8601 (YYYY-MM-DD)",
        "release_date": "Scheduled/actual public release date, ISO-8601",
    },
    "📢 Channel & Community Insights": {
        "channel_name": "Uploading channel's display name",
        "channel_id": "Uploading channel's stable channel ID (UC...)",
        "channel_url": "Canonical URL of the uploading channel",
        "subscriber_count": "Uploading channel's subscriber count, best-effort",
        "comments": "The extracted comment objects themselves (see --comments-limit)",
    },
    "🏷️ Classifications & Settings": {
        "tags": "Uploader-supplied video tags",
        "categories": "YouTube category classification(s)",
        "language": "Declared metadata language, if set by uploader (often null)",
        "is_live": "Whether this was/is a livestream",
        "is_age_restricted": "Whether the video is age-gated",
    },
    "📂 Playlist Context": {
        "playlist_title": "Title of the parent playlist/channel-uploads feed",
        "playlist_index": "1-based position of this video within the parent playlist",
    }
}

AVAILABLE_FIELDS: dict[str, str] = {}
for fields_dict in AVAILABLE_FIELDS_CATEGORIZED.values():
    AVAILABLE_FIELDS.update(fields_dict)

DEFAULT_FIELDS: tuple[str, ...] = (
    "video_id",
    "title",
    "channel_name",
    "views",
    "likes",
    "upload_date",
    "duration",
    "description",
)


@dataclass
class ChapterRecord:
    """A single chapter marker within a video's timeline, as reported by
    YouTube's chapter feature (uploader-defined, timestamped sections).

    Field names mirror yt-dlp's own documented `chapters` schema exactly
    (see yt_dlp/extractor/common.py's InfoExtractor docstring): each entry
    is `start_time`/`end_time` in seconds plus an optional `title`.
    `end_time` is often filled in by yt-dlp itself from the next chapter's
    start (or the video's total duration for the last chapter) rather than
    being present in the raw upstream data, but by the time it reaches us
    in the info-dict it is normally already resolved.
    """

    start_time: float
    end_time: Optional[float] = None
    title: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class CommentRecord:
    """A single top-level YouTube comment (replies are nested under `replies`)."""

    comment_id: str
    author: Optional[str]
    author_channel_id: Optional[str]
    text: str
    like_count: Optional[int]
    reply_count: int = 0
    is_favorited: bool = False
    is_pinned: bool = False
    timestamp: Optional[int] = None  # unix epoch, as returned by yt-dlp
    parent_id: Optional[str] = None  # "root" for top-level comments
    replies: list["CommentRecord"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["replies"] = [r.to_dict() for r in self.replies]
        return d

    def flatten(self) -> list["CommentRecord"]:
        """Return this comment plus all descendants as a flat list (for CSV/XLSX rows)."""
        out = [self]
        for r in self.replies:
            out.extend(r.flatten())
        return out


@dataclass
class VideoRecord:
    """Normalized representation of a single YouTube video, built from a
    yt-dlp info-dict. Field names here are intentionally decoupled from
    yt-dlp's own key names (see data_processor.RawFieldMap for the mapping)
    so that if yt-dlp renames an upstream key, only one mapping needs to
    change rather than every consumer of this dataclass.
    """

    video_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    views: Optional[int] = None
    likes: Optional[int] = None
    dislikes: Optional[int] = None  # always None; see module docstring
    duration: Optional[int] = None  # seconds
    upload_date: Optional[str] = None  # ISO-8601 date string
    release_date: Optional[str] = None
    channel_name: Optional[str] = None
    channel_id: Optional[str] = None
    channel_url: Optional[str] = None
    subscriber_count: Optional[int] = None
    thumbnail_url: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    language: Optional[str] = None
    is_live: bool = False
    is_age_restricted: bool = False
    comment_count: Optional[int] = None
    comments: list[CommentRecord] = field(default_factory=list)
    chapters: list[ChapterRecord] = field(default_factory=list)
    share_link: Optional[str] = None
    webpage_url: Optional[str] = None
    playlist_title: Optional[str] = None
    playlist_index: Optional[int] = None

    # Bookkeeping — not user-selectable via --fields, always present so
    # every writer can report provenance/failures without a schema branch.
    status: ExtractionStatus = ExtractionStatus.SUCCESS
    error_message: Optional[str] = None
    extracted_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    source_url: str = ""

    def to_dict(self, fields_subset: Optional[list[str]] = None) -> dict[str, Any]:
        """Serialize to a plain dict, optionally restricted to `fields_subset`
        (user-facing field names from AVAILABLE_FIELDS). Bookkeeping fields
        (status/error_message/extracted_at/source_url) are always included
        so downstream tooling can tell success from failure at a glance.
        """
        full = dataclasses.asdict(self)
        full["status"] = self.status.value
        full["comments"] = [c.to_dict() for c in self.comments]
        full["chapters"] = [c.to_dict() for c in self.chapters]

        if fields_subset is None:
            return full

        bookkeeping = {"status", "error_message", "extracted_at", "source_url", "video_id"}
        keep = set(fields_subset) | bookkeeping

        # "timeline" is a synthetic/composite field: expand it into the
        # underlying raw fields rather than trying to keep a nested dict,
        # so CSV/XLSX (which need flat rows) don't choke on it.
        if "timeline" in keep:
            keep |= {"duration", "upload_date", "release_date"}

        # Selecting the actual comment objects should also surface the
        # aggregate comment_count for free — it's the natural companion
        # stat (e.g. "here are 20 comments out of comment_count total"),
        # and withholding it silently would be a surprising gap for anyone
        # who explicitly asked for comments.
        if "comments" in keep:
            keep |= {"comment_count"}

        return {k: v for k, v in full.items() if k in keep}


@dataclass
class ExtractionJob:
    """One unit of work fed into the thread pool: a single URL plus the
    context needed to process it (whether it came from a playlist, its
    position, etc.). Channels/playlists are expanded into one job per
    video by the input resolver *before* extraction begins.
    """

    url: str
    origin: str = "direct"  # "direct" | "playlist" | "channel" | "file"
    playlist_title: Optional[str] = None
    playlist_index: Optional[int] = None


@dataclass
class RunSummary:
    """Aggregate stats reported at the end of a run (also used for the
    rich progress UI's live counters).
    """

    total_jobs: int = 0
    succeeded: int = 0
    failed_unavailable: int = 0
    failed_comments_disabled: int = 0
    failed_rate_limited: int = 0
    failed_network: int = 0
    failed_unknown: int = 0
    total_comments_extracted: int = 0

    @property
    def failed(self) -> int:
        return (
            self.failed_unavailable
            + self.failed_comments_disabled
            + self.failed_rate_limited
            + self.failed_network
            + self.failed_unknown
        )

    def record(self, status: ExtractionStatus) -> None:
        if status is ExtractionStatus.SUCCESS:
            self.succeeded += 1
        elif status is ExtractionStatus.FAILED_UNAVAILABLE:
            self.failed_unavailable += 1
        elif status is ExtractionStatus.FAILED_COMMENTS_DISABLED:
            self.failed_comments_disabled += 1
        elif status is ExtractionStatus.FAILED_RATE_LIMITED:
            self.failed_rate_limited += 1
        elif status is ExtractionStatus.FAILED_NETWORK:
            self.failed_network += 1
        else:
            self.failed_unknown += 1
