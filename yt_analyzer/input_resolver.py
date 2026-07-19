"""
Resolves the --input argument into a flat list of ExtractionJob objects.

--input can be:
    * a single video URL / bare video ID
    * a playlist URL (?list=...)
    * a channel URL (/channel/UC..., /@handle, /c/Name, /user/Name)
    * a path to a .txt file containing one URL per line (any mixture of
      the above three, plus blank lines and '#' comments, which are
      ignored)

This module does NOT hit the network for single-video inputs (no need —
the job is just the URL, unexpanded). For playlist/channel inputs it uses
a lightweight "flat" yt-dlp extraction (extract_flat=True) to enumerate
member video URLs without pulling each video's full metadata yet — that
full pull happens later, one job at a time, in core_extractor.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import yt_dlp

from .exceptions import EmptyInputFileError, InputResolutionError, PlaylistUnavailableError
from .models import ExtractionJob

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_MARKERS = ("list=", "/playlist")
_CHANNEL_MARKERS = ("/channel/", "/@", "/c/", "/user/")


def classify_url(raw: str) -> str:
    """Return one of 'video', 'playlist', 'channel', or 'unknown' for a raw URL/ID string.

    This is a fast, offline heuristic based on URL shape — it deliberately
    does NOT make a network call. Ambiguous/ill-formed input is handed to
    yt-dlp anyway (as 'unknown'), which will raise its own descriptive
    error at extraction time if it truly can't be parsed.
    """
    raw = raw.strip()
    if not raw:
        return "unknown"

    if _VIDEO_ID_RE.match(raw):
        return "video"

    lowered = raw.lower()

    if any(marker in lowered for marker in _PLAYLIST_MARKERS):
        # A URL can contain both a video id (v=) and list= (when a user
        # opens a video from within a playlist). We treat that as a
        # playlist input only if there's no explicit v= watch parameter,
        # since the more common intent when list= is present alongside
        # v= is "this one video", not "expand the whole playlist".
        if "v=" in lowered or "/watch" not in lowered and "youtu.be/" in lowered:
            if "list=" in lowered and "v=" not in lowered:
                return "playlist"
            return "video"
        return "playlist"

    if any(marker in lowered for marker in _CHANNEL_MARKERS):
        return "channel"

    if "youtube.com/watch" in lowered or "youtu.be/" in lowered:
        return "video"

    return "unknown"


def _read_urls_from_txt(path: Path) -> list[str]:
    if not path.exists():
        raise InputResolutionError(f"Input file not found: {path}")
    if not path.is_file():
        raise InputResolutionError(f"Input path is not a file: {path}")

    urls: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InputResolutionError(
            f"Input file '{path}' is not valid UTF-8 text: {exc}"
        ) from exc

    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        urls.append(stripped)

    if not urls:
        raise EmptyInputFileError(f"Input file '{path}' contains no usable URLs.")

    return urls


def _expand_playlist_or_channel(url: str, origin: str, max_items: int | None = None) -> list[ExtractionJob]:
    """Use yt-dlp in flat-extraction mode to enumerate member video URLs
    without downloading full metadata for each one (that's deferred to
    core_extractor, one job at a time, so rate-limiting/threading applies
    uniformly regardless of whether the input was a playlist or loose URLs).
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": "only_download",  # keep going past a single dead entry
    }
    if max_items:
        ydl_opts["playlistend"] = max_items

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise PlaylistUnavailableError(
            f"Could not expand {origin} at '{url}': {exc}", url=url
        ) from exc

    if info is None:
        raise PlaylistUnavailableError(f"{origin.capitalize()} at '{url}' returned no data.", url=url)

    entries = info.get("entries")
    if entries is None:
        # Some single-video URLs get passed here defensively; fall back
        # to treating it as a single job rather than raising.
        return [ExtractionJob(url=url, origin="direct")]

    playlist_title = info.get("title")
    jobs: list[ExtractionJob] = []
    for idx, entry in enumerate(entries, start=1):
        if entry is None:
            continue  # a single dead entry inside an otherwise-fine playlist
        entry_url = entry.get("url") or entry.get("webpage_url") or entry.get("id")
        if not entry_url:
            continue
        # extract_flat sometimes returns bare video IDs instead of full URLs
        if _VIDEO_ID_RE.match(entry_url):
            entry_url = f"https://www.youtube.com/watch?v={entry_url}"
        jobs.append(
            ExtractionJob(
                url=entry_url,
                origin=origin,
                playlist_title=playlist_title,
                playlist_index=idx,
            )
        )

    if not jobs:
        raise PlaylistUnavailableError(
            f"{origin.capitalize()} at '{url}' was reachable but contained no extractable videos "
            f"(it may be empty, or every item may be private/deleted).",
            url=url,
        )
    return jobs


def resolve_input(raw_input: str, max_playlist_items: int | None = None) -> list[ExtractionJob]:
    """Top-level entry point. Returns a flat list of ExtractionJob, ready
    to be handed to the thread pool in core_extractor.

    Raises InputResolutionError / EmptyInputFileError / PlaylistUnavailableError
    on unrecoverable problems (e.g. file not found, playlist totally dead).
    Individual video-level failures are NOT raised here — they surface
    later, per-job, in core_extractor, where they can be logged and
    skipped without aborting the whole run.
    """
    raw_input = raw_input.strip()
    if not raw_input:
        raise InputResolutionError("--input was empty.")

    candidate_path = Path(raw_input)
    if candidate_path.suffix.lower() == ".txt":
        urls = _read_urls_from_txt(candidate_path)
        jobs: list[ExtractionJob] = []
        for url in urls:
            jobs.extend(_resolve_single_entry(url, origin_hint="file", max_playlist_items=max_playlist_items))
        return jobs

    return _resolve_single_entry(raw_input, origin_hint="direct", max_playlist_items=max_playlist_items)


def _resolve_single_entry(
    entry: str, origin_hint: str, max_playlist_items: int | None
) -> list[ExtractionJob]:
    kind = classify_url(entry)

    if kind == "video":
        url = entry if entry.lower().startswith("http") else f"https://www.youtube.com/watch?v={entry}"
        return [ExtractionJob(url=url, origin=origin_hint)]

    if kind == "playlist":
        return _expand_playlist_or_channel(entry, origin="playlist", max_items=max_playlist_items)

    if kind == "channel":
        return _expand_playlist_or_channel(entry, origin="channel", max_items=max_playlist_items)

    # Unknown shape — hand it to yt-dlp's flat extractor and let it decide;
    # this covers e.g. shortened/redirect URLs we can't classify offline.
    try:
        return _expand_playlist_or_channel(entry, origin=origin_hint, max_items=max_playlist_items)
    except PlaylistUnavailableError:
        # Last resort: treat it as a single video job and let core_extractor
        # produce a proper VideoUnavailableError with full context.
        return [ExtractionJob(url=entry, origin=origin_hint)]


def deduplicate_jobs(jobs: Iterable[ExtractionJob]) -> list[ExtractionJob]:
    """Remove duplicate URLs while preserving first-seen order and
    playlist context. Useful when a .txt file lists overlapping playlists.
    """
    seen: set[str] = set()
    out: list[ExtractionJob] = []
    for job in jobs:
        if job.url in seen:
            continue
        seen.add(job.url)
        out.append(job)
    return out
