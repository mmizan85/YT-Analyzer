"""
cli_interface.py — the Click-based CLI entrypoint.

Wires together: input_resolver -> core_extractor -> output_writers/db_manager,
with a `rich`-powered terminal UI (banner, colored status lines, live
progress bar) and a `questionary`-powered interactive mode that activates
automatically when the tool is run with no flags at all.

Flushing / chunking: results stream in from ExtractionEngine in COMPLETION
order (not submission order, since threads finish whenever they finish).
We buffer completed VideoRecords in a plain list and flush to every writer
every `--chunk-size` records (default 50, per the blueprint's "every 50
videos" requirement), plus once more at the very end for the remainder.
This bounds peak memory to one chunk's worth of records, regardless of
how many thousand videos are in the overall job.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from .core_extractor import ExtractionEngine, ExtractorConfig
from .db_manager import DBWriter
from .exceptions import (
    DatabaseError,
    EmptyInputFileError,
    InputResolutionError,
    OutputWriterError,
    PlaylistUnavailableError,
    UnsupportedFieldError,
    UnsupportedFormatError,
    YTAnalyzerError,
)
from .input_resolver import deduplicate_jobs, resolve_input
from .logger_manager import configure_logging, get_logger
from .models import AVAILABLE_FIELDS, DEFAULT_FIELDS, ExtractionStatus, RunSummary, VideoRecord
from .output_writers import WRITER_REGISTRY, ChunkedOutputCoordinator

console = Console()
logger = get_logger(__name__)

_LOG_LEVEL_NAMES: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

_ASCII_BANNER = r"""
[bold red] __   __ _____ [/bold red][bold cyan]   _              _                     [/bold cyan]
[bold red] \ \ / /|_   _|[/bold red][bold cyan]  /_\  _ _  __ _ | |_  _ _  ___  _ _    [/bold cyan]
[bold red]  \ V /   | |  [/bold red][bold cyan] / _ \| ' \/ _` || | || '_|/ -_)| '_|   [/bold cyan]
[bold red]   |_|    |_|  [/bold red][bold cyan]/_/ \_\_||_\__,_||_|_||_|  \___||_|     [/bold cyan]
                                                
 [bold white]⚡ Advanced YouTube Metadata & Community Insights Extraction Engine[/bold white]
 ───────────────────────────────────────────────────────────────────
"""

_STATUS_ICONS: dict[ExtractionStatus, str] = {
    ExtractionStatus.SUCCESS: "[green]✅[/green]",
    ExtractionStatus.FAILED_UNAVAILABLE: "[yellow]⚠️[/yellow]",
    ExtractionStatus.FAILED_COMMENTS_DISABLED: "[yellow]⚠️[/yellow]",
    ExtractionStatus.FAILED_RATE_LIMITED: "[red]🛑[/red]",
    ExtractionStatus.FAILED_NETWORK: "[red]🌐[/red]",
    ExtractionStatus.FAILED_UNKNOWN: "[red]❓[/red]",
}

_SUPPORTED_FORMATS = tuple(WRITER_REGISTRY.keys()) + ("db",)

class RichHelpCommand(click.Command):
    """Custom Click command to render a beautiful, structured, and modern help screen using rich."""
    def format_help(self, ctx, formatter):
        
        _print_banner()
        
        
        dev_info = (
            "[bold white]yt-analyzer[/bold white] — [cyan]Advanced YouTube Metadata & Community Insights Extraction CLI Engine.[/cyan]\n\n"
            "[bold magenta]👤 Developer:[/bold magenta] Mohammad Mizan\n"
            "[bold yellow]🚀 Version:[/bold yellow] 1.0.0 (Stable)\n"
            "[bold blue]💡 Mode:[/bold blue] Run with [bold green]zero flags[/bold green] to launch interactive UI mode."
        )
        console.print(Panel(dev_info, border_style="cyan", title="[bold cyan]⚙️ About Tool[/bold cyan]", title_align="left"))
        console.print()

        
        sections = {
            "📥 Input Options": ['--input', '-i', '--max-playlist-items'],
            "📊 Data Extraction Filtering": ['--fields', '-f', '--comments-limit'],
            "💾 Output & Storage Configuration": ['--format', '-fmt', '--output-dir', '-o', '--output-name'],
            "⚡ Performance & Network Optimization": ['--workers', '--chunk-size', '--delay', '--proxy'],
            "📝 Logging & Diagnostics": ['--log-dir', '--log-level', '--quiet']
        }

        
        opt_map = {opt.name: opt for opt in self.params if isinstance(opt, click.Option)}

        for sec_title, opt_names in sections.items():
            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column("Flag", style="bold green", width=30)
            table.add_column("Description", style="white")

            has_options = False
            for param in self.params:
                
                if any(name in opt_names for name in param.opts) or param.name in opt_names:
                    has_options = True
                    opts_str = ", ".join(param.opts)
                    
                    
                    type_str = ""
                    if isinstance(param.type, click.Choice):
                        type_str = f" [dim][{'/'.join(param.type.choices)}][/dim]"
                    elif param.type.name != 'boolean':
                        type_str = f" [dim]<{param.type.name}>[/dim]"

                    desc = param.help or ""
                    if param.default is not None and param.default != "0,0" and not isinstance(param.default, bool):
                        desc += f" [yellow](Default: {param.default})[/yellow]"

                    table.add_row(f"{opts_str}{type_str}", desc)

            if has_options:
                console.print(Panel(table, border_style="gray30", title=f"[bold gold1]{sec_title}[/bold gold1]", title_align="left"))
                console.print()

        
        demo_panel = (
            "[bold cyan]Interactive UI Mode (Recommended):[/bold cyan]\n"
            "  $ [green]yta[/green]\n\n"
            "[bold cyan]Quick Video Metadata Extract (JSON default):[/bold cyan]\n"
            "  $ [green]yta -i \"https://www.youtube.com/watch?v=VIDEO_ID\"[/green]\n\n"
            "[bold cyan]Multi-threaded Playlist Analytics with Comments & Excel/DB Export:[/bold cyan]\n"
            "  $ [green]yta -i \"PLAYLIST_URL\" -f \"title,views,likes,comments\" -fmt \"xlsx,db\" --workers 6[/green]\n\n"
            "[bold yellow]⚠️ Note:[/bold yellow] If the tool throws network/rate-limit exceptions, use [bold magenta]--delay 1,3[/bold magenta] and a valid proxy."
        )
        console.print(Panel(demo_panel, border_style="orange1", title="[bold orange1]💡 Quick Usage Examples (Demos)[/bold orange1]", title_align="left"))

def _print_banner() -> None:
    console.print(_ASCII_BANNER)


def _parse_fields(raw: Optional[str]) -> list[str]:
    if not raw:
        return list(DEFAULT_FIELDS)
    requested = [f.strip() for f in raw.split(",") if f.strip()]
    unknown = [f for f in requested if f not in AVAILABLE_FIELDS]
    if unknown:
        valid = ", ".join(sorted(AVAILABLE_FIELDS.keys()))
        raise UnsupportedFieldError(
            f"Unknown field(s): {', '.join(unknown)}. Available fields are: {valid}"
        )
    return requested


def _parse_formats(raw: Optional[str]) -> list[str]:
    if not raw:
        return ["json"]
    requested = [f.strip().lower() for f in raw.split(",") if f.strip()]
    unknown = [f for f in requested if f not in _SUPPORTED_FORMATS]
    if unknown:
        valid = ", ".join(_SUPPORTED_FORMATS)
        raise UnsupportedFormatError(
            f"Unknown format(s): {', '.join(unknown)}. Supported formats are: {valid}"
        )
    return requested


def _interactive_prompt() -> dict:
    """Launched automatically when the tool is invoked with zero arguments.
    Uses questionary for arrow-key multi-select, per the blueprint's
    interactive-mode requirement.
    """
    import questionary
    from questionary import Choice, Separator, Style

    console.print(
        Panel.fit(
            "[bold]No flags detected — launching interactive mode.[/bold]\n"
            "[dim]Use arrow keys + space to select, enter to confirm.[/dim]",
            border_style="cyan",
        )
    )
    custom_style = Style([
        ('separator', 'fg:#ffb300 bold'),      
        ('qmark', 'fg:#00ffff bold'),          
        ('question', 'fg:#ffffff bold'),       
        ('selected', 'fg:#00ff00 bold'),       
        ('pointer', 'fg:#00ff00 bold'),        
        ('highlighted', 'fg:#00ff00'),         
        ('text', 'fg:#e0e0e0'),               
        ('answer', 'fg:#00ffff bold'),         
    ])


    input_value = questionary.text(
        "Video / playlist / channel URL, or path to a .txt file:",
        style=custom_style
    ).ask()
    if not input_value:
        console.print("[red]No input provided. Exiting.[/red]")
        raise SystemExit(1)
    
    from .models import AVAILABLE_FIELDS_CATEGORIZED

   
    field_choices = []
    for category, fields in AVAILABLE_FIELDS_CATEGORIZED.items():
        
        field_choices.append(Separator(f"\n{category}"))
        
        for name, desc in fields.items():
            
            display_title = f"{name:<18} │ {desc}"
            field_choices.append(
                Choice(title=display_title, value=name, checked=name in DEFAULT_FIELDS)
            )

    selected_fields = questionary.checkbox(
        "Select the data fields to extract:", 
        choices=field_choices,
        style=custom_style
    ).ask()
    if not selected_fields:
        selected_fields = list(DEFAULT_FIELDS)

    
    FORMAT_DESCRIPTIONS = {
        "json":  "Standard structured format, best for general integration and programmatic parsing.",
        "jsonl": "JSON Lines, ideal for large streaming datasets and seamless Vector-DB ingestion.",
        "csv":   "Flat tabular layout, separate files for nested comments/chapters (Universal compatibility).",
        "xlsx":  "Rich Microsoft Excel spreadsheet with structured multi-tab sheets for deep analysis.",
        "txt":   "Clean, human- and LLM-readable text block layout tailored for prompt/RAG contexts.",
        "db":    "Persistent local SQLite database table architecture for rapid custom SQL querying."
    }

    
    format_choices = []
    for fmt in _SUPPORTED_FORMATS:
        desc = FORMAT_DESCRIPTIONS.get(fmt, "Standard output extraction format.")
        display_title = f"{fmt:<12} │ {desc}"
        
        format_choices.append(
            Choice(title=display_title, value=fmt, checked=(fmt == "json"))
        )
    
    selected_formats = questionary.checkbox(
        "Select output format(s):", 
        choices=format_choices,
        style=custom_style
    ).ask()
    
    if not selected_formats:
        selected_formats = ["json"]

    comments_limit = 0
    if "comments" in selected_fields:
        raw_limit = questionary.text(
            "Max comments per video to extract (0 = skip comments):", 
            default="20",
            style=custom_style
        ).ask()
        try:
            comments_limit = int(raw_limit)
        except (TypeError, ValueError):
            comments_limit = 20

    output_dir = questionary.text(
        "Output directory:", 
        default="./output", 
        style=custom_style
    ).ask() or "./output"
    

    return {
        "input": input_value,
        "fields": ",".join(selected_fields),
        "format": ",".join(selected_formats),
        "comments_limit": comments_limit,
        "output_dir": output_dir,
    }


def _render_summary_table(summary: RunSummary, elapsed_seconds: float) -> Table:
    table = Table(title="📊 Extraction Summary", show_header=True, header_style="bold cyan")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    table.add_row("✅ Succeeded", str(summary.succeeded))
    table.add_row("⚠️  Unavailable (private/deleted/region-blocked)", str(summary.failed_unavailable))
    table.add_row("⚠️  Comments disabled", str(summary.failed_comments_disabled))
    table.add_row("🛑 Rate-limited", str(summary.failed_rate_limited))
    table.add_row("🌐 Network errors", str(summary.failed_network))
    table.add_row("❓ Unknown errors", str(summary.failed_unknown))
    table.add_row("💬 Total comments extracted", str(summary.total_comments_extracted))
    table.add_row("⏱️  Elapsed time", f"{elapsed_seconds:.1f}s")
    table.add_row("[bold]Total jobs[/bold]", f"[bold]{summary.total_jobs}[/bold]")
    return table


@click.command(cls=RichHelpCommand, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--input", "-i", "input_value", default=None, help="Video/playlist/channel URL, or path to a .txt file of URLs.")
@click.option("--fields", "-f", default=None, help=f"Comma-separated fields to extract. Available: {', '.join(AVAILABLE_FIELDS)}")
@click.option("--format", "-fmt", "format_value", default=None, help=f"Comma-separated output format(s). Available: {', '.join(_SUPPORTED_FORMATS)}")
@click.option("--comments-limit", default=0, type=int, help="Max comments to extract per video (0 = skip comments entirely).")
@click.option("--delay", default="0,0", help="Random delay range in seconds between requests, as 'min,max' (e.g. '1,3'). Helps avoid HTTP 429s.")
@click.option("--proxy", default=None, help="Proxy URL to pass through to yt-dlp (e.g. socks5://127.0.0.1:1080).")
@click.option("--workers", default=4, type=int, help="Number of concurrent extraction threads.")
@click.option("--chunk-size", default=50, type=int, help="Flush to output every N processed videos (memory management).")
@click.option("--output-dir", "-o", default="./output", help="Directory to write output files into.")
@click.option("--output-name", default="yt_analyzer_results", help="Base filename (without extension) for output files.")
@click.option("--log-dir", default="./output", help="Directory to write the persistent rotating log file into (yt_analyzer.log).")
@click.option("--log-level", default="info", type=click.Choice(list(_LOG_LEVEL_NAMES.keys()), case_sensitive=False), help="Minimum severity written to the log file.")
@click.option("--max-playlist-items", default=None, type=int, help="Cap how many videos to pull from a single playlist/channel.")
@click.option("--quiet", is_flag=True, default=False, help="Suppress the banner and per-item status lines; only show the final summary.")
def cli(
    input_value: Optional[str],
    fields: Optional[str],
    format_value: Optional[str],
    comments_limit: int,
    delay: str,
    proxy: Optional[str],
    workers: int,
    chunk_size: int,
    output_dir: str,
    output_name: str,
    log_dir: str,
    log_level: str,
    max_playlist_items: Optional[int],
    quiet: bool,
) -> None:
    """yt-analyzer — Advanced YouTube Metadata & Community Insights Extraction CLI Engine.

    Run with no arguments at all to launch interactive mode.
    """
    configure_logging(log_dir=log_dir, level=_LOG_LEVEL_NAMES.get(log_level.lower(), logging.INFO))
    logger.info("yt-analyzer invoked. input=%r fields=%r format=%r workers=%d", input_value, fields, format_value, workers)

    no_flags_given = all(
        v is None
        for v in (input_value, fields, format_value)
    ) and comments_limit == 0 and delay == "0,0" and proxy is None

    if no_flags_given:
        answers = _interactive_prompt()
        input_value = answers["input"]
        fields = answers["fields"]
        format_value = answers["format"]
        comments_limit = answers["comments_limit"]
        output_dir = answers["output_dir"]

    if not quiet:
        _print_banner()

    if not input_value:
        console.print("[bold red]Error:[/bold red] --input is required (or run with no flags for interactive mode). Use --help for usage.")
        raise SystemExit(2)

    try:
        selected_fields = _parse_fields(fields)
        selected_formats = _parse_formats(format_value)
    except (UnsupportedFieldError, UnsupportedFormatError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(2) from exc

    try:
        delay_parts = [float(x.strip()) for x in delay.split(",")]
        if len(delay_parts) != 2:
            raise ValueError
        delay_min, delay_max = delay_parts
        if delay_min < 0 or delay_max < 0 or delay_min > delay_max:
            raise ValueError
    except ValueError:
        console.print(f"[bold red]Error:[/bold red] --delay must be 'min,max' with 0 <= min <= max (got '{delay}').")
        raise SystemExit(2)

    if comments_limit > 0 and "comments" not in selected_fields:
        selected_fields.append("comments")

    console.print(f"[bold]🚀 Starting extraction[/bold] — input: [cyan]{input_value}[/cyan]")
    logger.info("Starting extraction for input: %s", input_value)

    # --- Resolve input into jobs -------------------------------------------------
    try:
        with console.status("[bold cyan]Resolving input (expanding playlists/channels if needed)...[/bold cyan]"):
            jobs = resolve_input(input_value, max_playlist_items=max_playlist_items)
            jobs = deduplicate_jobs(jobs)
    except EmptyInputFileError as exc:
        logger.error("Empty input file: %s", exc)
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(1) from exc
    except InputResolutionError as exc:
        logger.error("Could not resolve --input '%s': %s", input_value, exc)
        console.print(f"[bold red]Error:[/bold red] Could not resolve --input: {exc}")
        raise SystemExit(1) from exc
    except PlaylistUnavailableError as exc:
        logger.error("Playlist/channel unavailable for --input '%s': %s", input_value, exc)
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(1) from exc

    if not jobs:
        logger.warning("Input '%s' resolved to zero jobs; nothing to extract.", input_value)
        console.print("[bold yellow]⚠️  No videos found to extract. Nothing to do.[/bold yellow]")
        raise SystemExit(0)

    console.print(f"[green]📥 Resolved {len(jobs)} video(s) to extract.[/green]")
    logger.info("Resolved %d job(s) from input '%s'.", len(jobs), input_value)

    # --- Set up output writers ----------------------------------------------------
    output_path = Path(output_dir)
    db_writer = None
    if "db" in selected_formats:
        db_path = output_path / f"{output_name}.db"
        try:
            db_writer = DBWriter(db_path)
        except DatabaseError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            raise SystemExit(1) from exc

    try:
        coordinator = ChunkedOutputCoordinator(
            formats=selected_formats,
            output_dir=output_path,
            base_filename=output_name,
            fields=selected_fields,
            db_writer=db_writer,
        )
    except OutputWriterError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(1) from exc

    # --- Run extraction with live progress -----------------------------------------
    config = ExtractorConfig(
        comments_limit=comments_limit,
        delay_min_seconds=delay_min,
        delay_max_seconds=delay_max,
        proxy=proxy,
        max_workers=max(1, workers),
    )

    summary = RunSummary()
    buffer: list[VideoRecord] = []
    start_time = time.monotonic()

    def flush_buffer() -> None:
        if not buffer:
            return
        try:
            coordinator.write_chunk(list(buffer))
        except OutputWriterError as exc:
            logger.error("Fatal write error while flushing a batch of %d record(s): %s", len(buffer), exc)
            console.print(f"[bold red]Fatal write error:[/bold red] {exc}")
            coordinator.close()
            raise SystemExit(1) from exc
        buffer.clear()

    progress_columns = (
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    )

    # NOTE: we deliberately do NOT use `with coordinator:` here. That was
    # tried initially, but a `with` block runs __exit__ (which calls
    # coordinator.close(), closing every writer's file handle) as the
    # exception propagates OUT of the block — meaning by the time an
    # `except KeyboardInterrupt:` clause runs, every writer is already
    # closed and a final flush_buffer() call fails with
    # "write_batch called before open()". Managing open()/close() manually
    # lets us flush into STILL-OPEN writers first, and only close them
    # afterward, on every exit path (success, interrupt, or fatal error).
    coordinator.open()
    try:
        with Progress(*progress_columns, console=console, disable=quiet) as progress:
            task_id = progress.add_task("📥 Extracting videos...", total=len(jobs))

            def on_result(record: VideoRecord) -> None:
                buffer.append(record)
                if not quiet:
                    icon = _STATUS_ICONS.get(record.status, "❓")
                    label = record.title or record.video_id or record.source_url
                    progress.console.print(f"  {icon} {label}")
                if len(buffer) >= chunk_size:
                    flush_buffer()

            def on_progress(done: int, total: int, record: VideoRecord) -> None:
                progress.update(task_id, completed=done)

            engine = ExtractionEngine(config, on_result=on_result, on_progress=on_progress)
            engine.run(jobs, summary)

        flush_buffer()  # final partial chunk
    except KeyboardInterrupt:
        logger.warning("Run interrupted by user (Ctrl+C) after %d/%d job(s) completed.", summary.succeeded + summary.failed, summary.total_jobs)
        console.print("\n[bold yellow]⚠️  Interrupted by user — flushing partial results before exit...[/bold yellow]")
        flush_buffer()  # writers are still open here, unlike the `with coordinator:` version
        coordinator.close()
        console.print("[yellow]Partial results saved. Exiting.[/yellow]")
        raise SystemExit(130)
    else:
        coordinator.close()

    elapsed = time.monotonic() - start_time
    logger.info(
        "Run complete in %.1fs — succeeded=%d, failed=%d (unavailable=%d, comments_disabled=%d, "
        "rate_limited=%d, network=%d, unknown=%d), total_comments=%d",
        elapsed, summary.succeeded, summary.failed, summary.failed_unavailable,
        summary.failed_comments_disabled, summary.failed_rate_limited, summary.failed_network,
        summary.failed_unknown, summary.total_comments_extracted,
    )

    console.print()
    console.print(_render_summary_table(summary, elapsed))
    console.print()
    console.print("[bold green]✅ Done![/bold green] Output written to:")
    for path in coordinator.output_paths:
        console.print(f"  📄 {path}")

    # Exit code reflects the actual outcome, not just "the process didn't
    # crash" — a script/cron job/CI pipeline invoking this tool needs to be
    # able to tell "every job succeeded" apart from "the batch ran, but
    # every single item failed" apart from "some items failed." Without
    # this, a network outage that fails 100% of jobs would still report
    # exit code 0 and print "Done!", which is misleading for automation.
    #   0 = every job succeeded (or there were zero jobs to run — see the
    #       earlier `if not jobs: raise SystemExit(0)` branch above)
    #   1 = reserved elsewhere in this file for fatal setup/write errors
    #   2 = reserved elsewhere in this file for CLI argument validation errors
    #   3 = the run completed, but EVERY job failed (0 successes)
    #   4 = the run completed with a MIX of successes and failures
    if summary.succeeded == 0 and summary.failed > 0:
        logger.warning("Every job failed (0/%d succeeded) — exiting with code 3.", summary.total_jobs)
        console.print("[bold red]⚠️  Every job failed — no data was successfully extracted.[/bold red]")
        raise SystemExit(3)
    if summary.failed > 0:
        logger.warning("Run completed with partial failures (%d/%d succeeded) — exiting with code 4.", summary.succeeded, summary.total_jobs)
        console.print(f"[bold yellow]⚠️  {summary.failed} of {summary.total_jobs} job(s) failed — see the summary above.[/bold yellow]")
        raise SystemExit(4)


def main() -> None:
    try:
        cli()
    except YTAnalyzerError as exc:
        logger.critical("Fatal error at top level: %s", exc, exc_info=True)
        console.print(f"[bold red]Fatal error:[/bold red] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
