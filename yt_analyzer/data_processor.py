"""
data_processor.py — turns a raw yt-dlp info-dict into a VideoRecord.

This is the ONE place in the codebase that knows yt-dlp's actual upstream
key names (verified against yt-dlp 2026.7.4's YouTube extractor source,
`yt_dlp/extractor/youtube/_video.py`). If yt-dlp ever renames a key
upstream, only this file needs to change.

Verified real key names used below (not guessed):
    id, title, description, view_count, like_count, duration,
    upload_date, release_date, release_timestamp, channel, channel_id,
    channel_url, channel_follower_count, thumbnail, thumbnails, tags,
    categories, age_limit, live_status, availability, comment_count,
    comments, chapters, webpage_url, language

The `chapters` key (verified against yt_dlp/extractor/common.py's
InfoExtractor docstring, the canonical schema definition for this field)
is a list of dicts, each with `start_time` (required, seconds),
`end_time` (usually resolved by yt-dlp itself from the next chapter's
start or the video's duration), and an optional `title`. Most videos have
no chapters at all, in which case yt-dlp omits the key entirely (not an
empty list), so we default to `[]` rather than treating absence as an error.

Confirmed NOT to exist upstream (and why):
    dislike_count — YouTube stopped exposing public dislike counts in
        December 2021; no yt-dlp field returns this. We always emit
        `dislikes=None`.
    subscriber_count — the real key is `channel_follower_count`; there is
        no separate "subscriber_count" key, they are the same number.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .models import ChapterRecord, CommentRecord, VideoRecord

# yt-dlp's `age_limit` is an integer (0, 13, 17, 18...). Anything >= 18 is
# the practical "age-restricted" signal in the wild; YouTube itself only
# really uses 0 and 18.
_AGE_RESTRICTED_THRESHOLD = 18


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_date_from_yyyymmdd(value: Optional[str]) -> Optional[str]:
    """yt-dlp's upload_date/release_date are 'YYYYMMDD' strings. Convert to
    ISO-8601 'YYYY-MM-DD' for consistent, sortable output across every
    writer (JSON/CSV/XLSX/DB all benefit from a real date format).
    """
    if not value or len(value) != 8 or not value.isdigit():
        return value  # pass through unrecognized shapes rather than dropping data
    try:
        dt = datetime.strptime(value, "%Y%m%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return value


def _best_thumbnail_url(info: dict) -> Optional[str]:
    """Prefer the largest thumbnail in the `thumbnails` list (yt-dlp sorts
    it ascending by preference/resolution, so the last entry is usually
    the best); fall back to the flat `thumbnail` key if the list is absent.
    """
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list) and thumbnails:
        # Prefer entries with explicit width/height so we don't accidentally
        # pick a tiny placeholder that happens to be last in an unsorted list.
        with_dims = [t for t in thumbnails if t.get("width") and t.get("height")]
        pool = with_dims or thumbnails
        best = max(pool, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)) if with_dims else pool[-1]
        url = best.get("url")
        if url:
            return url
    return info.get("thumbnail")


def _build_comment_tree(raw_comments: list[dict], limit: int) -> list[CommentRecord]:
    """yt-dlp returns comments as a FLAT list where each dict has a 'parent'
    key ('root' for top-level, or the parent comment's id for replies).
    We reconstruct the tree so CommentRecord.replies is populated, which
    both the JSON/JSONL writers (nested, RAG-friendly) and the CSV/XLSX
    writers (via .flatten()) can consume.

    `limit` is the already-applied yt-dlp extraction cap (see
    ExtractorConfig.comments_limit) — this function does not re-truncate,
    it just organizes what yt-dlp already returned.
    """
    if not raw_comments:
        return []

    nodes: dict[str, CommentRecord] = {}
    order: list[str] = []

    for raw in raw_comments:
        cid = raw.get("id")
        if not cid:
            continue
        nodes[cid] = CommentRecord(
            comment_id=cid,
            author=raw.get("author"),
            author_channel_id=raw.get("author_id"),
            text=raw.get("text") or "",
            like_count=_safe_int(raw.get("like_count")),
            is_favorited=bool(raw.get("is_favorited") or False),
            is_pinned=bool(raw.get("is_pinned") or False),
            timestamp=_safe_int(raw.get("timestamp")),
            parent_id=raw.get("parent") or "root",
        )
        order.append(cid)

    roots: list[CommentRecord] = []
    for cid in order:
        node = nodes[cid]
        if node.parent_id == "root" or node.parent_id not in nodes:
            roots.append(node)
        else:
            parent = nodes[node.parent_id]
            parent.replies.append(node)
            parent.reply_count += 1

    return roots


def _build_chapters(raw_chapters: Any) -> list[ChapterRecord]:
    """Convert yt-dlp's raw `chapters` list (list of dicts with start_time/
    end_time/title) into ChapterRecord objects. Defensive against
    malformed entries: a single bad chapter dict (missing start_time, or
    a non-numeric value) is skipped rather than raising and losing every
    other valid chapter in the same video — the same "one bad item must
    not sink the batch" principle applied at the per-video level in
    core_extractor also applies here at the per-chapter level.
    """
    if not raw_chapters or not isinstance(raw_chapters, list):
        return []

    chapters: list[ChapterRecord] = []
    for raw in raw_chapters:
        if not isinstance(raw, dict):
            continue
        start = _safe_float(raw.get("start_time"))
        if start is None:
            # start_time is the one required field per yt-dlp's own schema;
            # without it there's no valid timeline position for this marker.
            continue
        chapters.append(
            ChapterRecord(
                start_time=start,
                end_time=_safe_float(raw.get("end_time")),
                title=raw.get("title") or None,
            )
        )
    return chapters


def normalize_info_dict(
    info: dict,
    source_url: str,
    playlist_title: Optional[str] = None,
    playlist_index: Optional[int] = None,
    comments_limit: int = 0,
) -> VideoRecord:
    """Convert one yt-dlp info-dict (as returned by extract_info) into a
    fully-populated VideoRecord. Never raises — if a field is missing
    upstream, the corresponding VideoRecord attribute is simply None/empty,
    which is itself meaningful signal (not every video has every field).
    """
    video_id = info.get("id") or ""
    upload_date_iso = _iso_date_from_yyyymmdd(info.get("upload_date"))

    # release_date is only meaningfully different from upload_date for
    # scheduled premieres; yt-dlp gives release_date as YYYYMMDD too, and
    # release_timestamp as a unix epoch fallback when release_date is absent.
    release_date_iso = _iso_date_from_yyyymmdd(info.get("release_date"))
    if not release_date_iso and info.get("release_timestamp"):
        try:
            release_date_iso = datetime.fromtimestamp(
                info["release_timestamp"], tz=timezone.utc
            ).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            release_date_iso = None

    raw_comments = info.get("comments") or []
    comment_records = _build_comment_tree(raw_comments, limit=comments_limit) if comments_limit > 0 else []

    # Unlike comments, chapters require no separate extraction pass or
    # --comments-limit-style budget — yt-dlp includes them in the same
    # info-dict pulled for basic metadata, at no extra request cost. So we
    # always parse them if present, rather than gating behind a flag.
    chapter_records = _build_chapters(info.get("chapters"))

    age_limit = _safe_int(info.get("age_limit")) or 0

    record = VideoRecord(
        video_id=video_id,
        title=info.get("title"),
        description=info.get("description"),
        views=_safe_int(info.get("view_count")),
        likes=_safe_int(info.get("like_count")),
        dislikes=None,  # see module docstring: no upstream source exists
        duration=_safe_int(info.get("duration")),
        upload_date=upload_date_iso,
        release_date=release_date_iso,
        channel_name=info.get("channel") or info.get("uploader"),
        channel_id=info.get("channel_id"),
        channel_url=info.get("channel_url") or info.get("uploader_url"),
        subscriber_count=_safe_int(info.get("channel_follower_count")),
        thumbnail_url=_best_thumbnail_url(info),
        tags=list(info.get("tags") or []),
        categories=list(info.get("categories") or []),
        language=info.get("language"),
        is_live=bool(info.get("live_status") in ("is_live", "was_live", "post_live")),
        is_age_restricted=age_limit >= _AGE_RESTRICTED_THRESHOLD,
        comment_count=_safe_int(info.get("comment_count")),
        comments=comment_records,
        chapters=chapter_records,
        share_link=f"https://youtu.be/{video_id}" if video_id else None,
        webpage_url=info.get("webpage_url"),
        playlist_title=playlist_title or info.get("playlist_title"),
        playlist_index=playlist_index if playlist_index is not None else _safe_int(info.get("playlist_index")),
        source_url=source_url,
    )
    return record
