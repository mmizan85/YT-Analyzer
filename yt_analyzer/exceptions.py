"""
Custom exception hierarchy for yt-analyzer.

Having a dedicated hierarchy (instead of catching bare `Exception` everywhere)
lets the extraction pipeline distinguish between failure modes that should
skip-and-continue (a single private video) versus failure modes that should
abort the whole run (a malformed output path, a corrupt SQLite file).
"""

from __future__ import annotations


class YTAnalyzerError(Exception):
    """Base class for every exception raised intentionally by this package."""


# --------------------------------------------------------------------------- #
# Input / resolution errors
# --------------------------------------------------------------------------- #
class InputResolutionError(YTAnalyzerError):
    """Raised when --input cannot be parsed into at least one valid job."""


class EmptyInputFileError(InputResolutionError):
    """Raised when a supplied .txt file contains no usable URLs."""


# --------------------------------------------------------------------------- #
# Extraction errors (per-item; the pipeline should log & continue on these)
# --------------------------------------------------------------------------- #
class ExtractionError(YTAnalyzerError):
    """Base class for errors that occur while extracting a single item."""

    def __init__(self, message: str, url: str | None = None, video_id: str | None = None):
        self.url = url
        self.video_id = video_id
        super().__init__(message)


class VideoUnavailableError(ExtractionError):
    """Video is private, deleted, region-blocked, age-restricted, or a member's-only video."""


class PlaylistUnavailableError(ExtractionError):
    """Playlist/channel is private, deleted, or otherwise inaccessible."""


class CommentsDisabledError(ExtractionError):
    """Comments could not be fetched because the uploader disabled them."""


class RateLimitedError(ExtractionError):
    """The extractor hit an HTTP 429 / bot-check wall. Caller should back off."""


class NetworkError(ExtractionError):
    """Generic connectivity failure (DNS, timeout, connection reset, SSL)."""


# --------------------------------------------------------------------------- #
# Processing / persistence errors (usually fatal for the whole run)
# --------------------------------------------------------------------------- #
class DataProcessingError(YTAnalyzerError):
    """Raised when normalizing a raw yt-dlp info-dict into a model fails."""


class OutputWriterError(YTAnalyzerError):
    """Raised when a writer (JSON/CSV/XLSX/DB) cannot persist a batch."""


class DatabaseError(OutputWriterError):
    """Raised for SQLite-specific failures (locked file, schema mismatch)."""


class UnsupportedFieldError(YTAnalyzerError):
    """Raised when --fields references a field the tool does not know about."""


class UnsupportedFormatError(YTAnalyzerError):
    """Raised when --format references a format the tool does not support."""
