"""
yt-analyzer — Advanced YouTube Metadata & Community Insights Extraction CLI Engine.

Public API surface for programmatic use (in addition to the `yt-analyzer`
CLI command). Import from here rather than reaching into submodules
directly, so internal refactors don't break external callers.
"""

from .core_extractor import ExtractionEngine, ExtractorConfig, VideoExtractor
from .data_processor import normalize_info_dict
from .db_manager import DBWriter
from .exceptions import (
    DatabaseError,
    EmptyInputFileError,
    ExtractionError,
    InputResolutionError,
    OutputWriterError,
    VideoUnavailableError,
    YTAnalyzerError,
)
from .input_resolver import resolve_input
from .logger_manager import configure_logging, get_logger
from .models import (
    AVAILABLE_FIELDS,
    DEFAULT_FIELDS,
    ChapterRecord,
    CommentRecord,
    ExtractionJob,
    ExtractionStatus,
    RunSummary,
    VideoRecord,
)
from .output_writers import ChunkedOutputCoordinator

__version__ = "1.0.0"

__all__ = [
    "ExtractionEngine",
    "ExtractorConfig",
    "VideoExtractor",
    "normalize_info_dict",
    "DBWriter",
    "YTAnalyzerError",
    "ExtractionError",
    "InputResolutionError",
    "EmptyInputFileError",
    "VideoUnavailableError",
    "OutputWriterError",
    "DatabaseError",
    "resolve_input",
    "configure_logging",
    "get_logger",
    "VideoRecord",
    "CommentRecord",
    "ChapterRecord",
    "ExtractionJob",
    "ExtractionStatus",
    "RunSummary",
    "AVAILABLE_FIELDS",
    "DEFAULT_FIELDS",
    "ChunkedOutputCoordinator",
    "__version__",
]
