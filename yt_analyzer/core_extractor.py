"""
core_extractor.py — the yt-dlp wrapper layer.

Two classes, one responsibility each:
    * VideoExtractor   — pulls full metadata (+ optionally comments) for one URL
    * ExtractionEngine — owns the thread pool, rate-limit delay, and batching/
                         flush-to-writer callback that the CLI layer drives

Design notes:
    * yt-dlp does not expose distinct exception *classes* per failure reason
      (private vs deleted vs age-restricted vs rate-limited all raise the
      same DownloadError/ExtractorError). We classify by matching known
      substrings in the exception message — this is inherently best-effort
      pattern matching, not a guarantee, so `FAILED_UNKNOWN` is a real,
      expected outcome for messages we don't recognize, and is handled the
      same "log and continue" way as recognized ones.
    * Every yt-dlp call for a single job runs inside one big try/except so a
      single bad video/network blip cannot crash the batch (blueprint
      requirement: log and continue, never crash).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

import yt_dlp

from .data_processor import normalize_info_dict
from .exceptions import (
    CommentsDisabledError,
    ExtractionError,
    NetworkError,
    RateLimitedError,
    VideoUnavailableError,
)
from .logger_manager import get_logger
from .models import ExtractionJob, ExtractionStatus, RunSummary, VideoRecord

logger = get_logger(__name__)

# Substrings observed in real yt-dlp error messages for each category.
# Matching is case-insensitive. This list is intentionally conservative —
# an unmatched message becomes FAILED_UNKNOWN rather than being guessed at.
_UNAVAILABLE_MARKERS = (
    "private video",
    "video unavailable",
    "video is no longer available",
    "has been removed",
    "account associated with this video has been terminated",
    "this video is not available",
    "sign in to confirm your age",
    "content isn't available",
    "members-only content",
    "join this channel",
    "not available in your country",
)
_RATE_LIMIT_MARKERS = (
    "429",
    "too many requests",
    "http error 429",
    "rate-limit",
    "rate limit",
    "sign in to confirm you're not a bot",
)
_NETWORK_MARKERS = (
    "urlopen error",
    "connection reset",
    "timed out",
    "timeout",
    "name or service not known",
    "failed to establish a new connection",
    "ssl",
    "certificate verify failed",
    "temporary failure in name resolution",
)
_COMMENTS_DISABLED_MARKERS = (
    "comments are disabled",
    "comment extraction is not supported",
)


def classify_yt_dlp_error(exc: BaseException) -> ExtractionError:
    """Turn a raw yt-dlp exception into one of our typed exceptions,
    preserving the original message for logging/debugging.
    """
    message = str(exc)
    lowered = message.lower()

    if any(marker in lowered for marker in _COMMENTS_DISABLED_MARKERS):
        return CommentsDisabledError(message)
    if any(marker in lowered for marker in _UNAVAILABLE_MARKERS):
        return VideoUnavailableError(message)
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return RateLimitedError(message)
    if any(marker in lowered for marker in _NETWORK_MARKERS):
        return NetworkError(message)
    # Fall through: still an ExtractionError, just not one we can classify
    # further. Callers treat this as FAILED_UNKNOWN and move on.
    return ExtractionError(message)


_STATUS_FOR_EXC: dict[type, ExtractionStatus] = {
    VideoUnavailableError: ExtractionStatus.FAILED_UNAVAILABLE,
    CommentsDisabledError: ExtractionStatus.FAILED_COMMENTS_DISABLED,
    RateLimitedError: ExtractionStatus.FAILED_RATE_LIMITED,
    NetworkError: ExtractionStatus.FAILED_NETWORK,
}


def _status_for(exc: ExtractionError) -> ExtractionStatus:
    return _STATUS_FOR_EXC.get(type(exc), ExtractionStatus.FAILED_UNKNOWN)


@dataclass
class ExtractorConfig:
    """All the tunables that map directly to CLI flags."""

    comments_limit: int = 0          # 0 == do not fetch comments at all
    delay_min_seconds: float = 0.0
    delay_max_seconds: float = 0.0
    proxy: Optional[str] = None
    max_workers: int = 4
    retries: int = 2                  # yt-dlp-internal retries per request
    socket_timeout: int = 20


class VideoExtractor:
    """Extracts full metadata (and optionally comments) for exactly one video URL."""

    def __init__(self, config: ExtractorConfig):
        self.config = config

    def _build_ydl_opts(self, fetch_comments: bool) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": self.config.socket_timeout,
            "retries": self.config.retries,
            "extractor_retries": self.config.retries,
            # Don't let a dead comments feed abort metadata we already have;
            # we handle that case explicitly instead via a distinct pass.
            "ignoreerrors": False,
        }
        if self.config.proxy:
            opts["proxy"] = self.config.proxy
        if fetch_comments and self.config.comments_limit > 0:
            opts["getcomments"] = True
            # max_comments: [total, max_parents, max_replies_per_thread, max_replies_total]
            # yt-dlp's own contract for this list — we cap total comments to
            # the user's limit and let per-thread reply caps follow suit so
            # a single mega-thread can't blow the whole budget.
            per_thread_cap = max(5, self.config.comments_limit // 4)
            opts["extractor_args"] = {
                "youtube": {
                    "max_comments": [
                        str(self.config.comments_limit),
                        "all",
                        str(per_thread_cap),
                        "all",
                    ]
                }
            }
        return opts

    def extract(self, job: ExtractionJob) -> VideoRecord:
        """Extract one video. Raises ExtractionError subclasses on failure;
        never raises a raw yt-dlp exception, and never lets an exception
        from comment-fetching destroy metadata already successfully pulled.
        """
        want_comments = self.config.comments_limit > 0
        opts = self._build_ydl_opts(fetch_comments=want_comments)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job.url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise classify_yt_dlp_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - last-resort safety net, never crash the batch
            raise classify_yt_dlp_error(exc) from exc

        if info is None:
            raise VideoUnavailableError(f"yt-dlp returned no data for '{job.url}'", url=job.url)

        record = normalize_info_dict(
            info,
            source_url=job.url,
            playlist_title=job.playlist_title,
            playlist_index=job.playlist_index,
            comments_limit=self.config.comments_limit,
        )
        return record


class ExtractionEngine:
    """Owns the thread pool + rate-limit delay + progress callback. This is
    what cli_interface.py actually drives; it never touches yt-dlp directly.
    """

    def __init__(
        self,
        config: ExtractorConfig,
        on_result: Callable[[VideoRecord], None],
        on_progress: Optional[Callable[[int, int, VideoRecord], None]] = None,
    ):
        """
        on_result(record)               — called once per completed job (success or failure),
                                           in COMPLETION order (not submission order). Must be
                                           thread-safe from the caller's side if it mutates shared
                                           state (the CLI wraps this in a lock before batching).
        on_progress(done, total, record) — optional, for a live rich progress bar.
        """
        self.config = config
        self.on_result = on_result
        self.on_progress = on_progress
        self._extractor = VideoExtractor(config)
        self._lock = threading.Lock()
        self._completed = 0

    def _sleep_for_rate_limit(self) -> None:
        lo, hi = self.config.delay_min_seconds, self.config.delay_max_seconds
        if hi <= 0:
            return
        lo = min(lo, hi)
        time.sleep(random.uniform(lo, hi))

    def _run_one(self, job: ExtractionJob, total: int) -> VideoRecord:
        self._sleep_for_rate_limit()
        try:
            record = self._extractor.extract(job)
            logger.debug("Extracted '%s' (video_id=%s) successfully.", job.url, record.video_id)
        except ExtractionError as exc:
            # This is the expected "log and continue" path for recognized
            # failure modes (private/deleted/age-restricted videos, comments
            # disabled, rate-limiting, network blips): WARNING, not ERROR,
            # since these are routine, anticipated outcomes of scraping a
            # large batch of real-world URLs, not bugs in this tool.
            logger.warning(
                "Extraction failed for '%s' — classified as %s: %s",
                job.url, _status_for(exc).value, exc,
            )
            record = VideoRecord(
                video_id=job.url,  # best-effort id substitute; real id unknown on failure
                status=_status_for(exc),
                error_message=str(exc),
                source_url=job.url,
                playlist_title=job.playlist_title,
                playlist_index=job.playlist_index,
            )
        except Exception as exc:  # noqa: BLE001 - a bug in yt-dlp/normalization must not skip bookkeeping below
            # Deliberately broader than ExtractionError: an unclassified exception
            # (e.g. a bug surfaced deep inside yt-dlp's own parsing, or a defect in
            # our normalize_info_dict) must still increment the completed counter
            # and fire on_progress/on_result exactly like any other outcome, or the
            # progress bar and final summary would silently under-report — which is
            # worse than an ugly but honest "unknown" row.
            # ERROR level (not WARNING) with exc_info=True: this branch means
            # something genuinely unanticipated happened, so the full
            # traceback belongs in the persistent log for later debugging,
            # not just a one-line summary.
            logger.error(
                "Unhandled exception while extracting '%s' — this is unexpected and likely indicates a bug: %s",
                job.url, exc, exc_info=True,
            )
            record = VideoRecord(
                video_id=job.url,
                status=ExtractionStatus.FAILED_UNKNOWN,
                error_message=f"Unhandled exception during extraction: {exc}",
                source_url=job.url,
                playlist_title=job.playlist_title,
                playlist_index=job.playlist_index,
            )
        with self._lock:
            self._completed += 1
            done = self._completed
        if self.on_progress:
            self.on_progress(done, total, record)
        return record

    def run(self, jobs: list[ExtractionJob], summary: RunSummary) -> None:
        """Process every job, calling on_result for each as it completes.
        Uses a ThreadPoolExecutor sized by config.max_workers. Because
        every yt-dlp network call releases the GIL while blocked on I/O,
        threads (not processes) give real concurrency here without the
        pickling overhead multiprocessing would add for this workload.
        """
        total = len(jobs)
        summary.total_jobs = total
        if total == 0:
            return

        with ThreadPoolExecutor(max_workers=max(1, self.config.max_workers)) as pool:
            futures = {pool.submit(self._run_one, job, total): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001 - absolute last resort: _run_one already catches
                    # extraction failures internally (so on_progress fires for those); this branch only
                    # guards against a bug in _run_one's OWN bookkeeping (the lock/counter code itself),
                    # which is why on_progress is deliberately NOT called again here — _run_one is the
                    # single source of truth for progress accounting, this is purely a safety net so a
                    # freak failure there can't take down the whole batch.
                    # CRITICAL, not ERROR: _run_one's own try/except is supposed to catch every
                    # exception from extraction; reaching this branch means the bookkeeping code
                    # itself (the lock, the counter) broke, which is a more severe defect than an
                    # ordinary extraction failure and deserves the highest log level in this module.
                    logger.critical(
                        "Worker thread bookkeeping failed unexpectedly for '%s': %s",
                        job.url, exc, exc_info=True,
                    )
                    record = VideoRecord(
                        video_id=job.url,
                        status=ExtractionStatus.FAILED_UNKNOWN,
                        error_message=f"Unhandled exception in worker thread: {exc}",
                        source_url=job.url,
                    )
                summary.record(record.status)
                summary.total_comments_extracted += len(record.comments)
                self.on_result(record)
