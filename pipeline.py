#!/usr/bin/env python3
"""
HiFi Pipeline — batch process a folder of show footage into Shorts + Long form.

Usage:
    python pipeline.py --input /Volumes/SSD/HiFiShow_June2026 --show "AxisHiFi 2026"
    python pipeline.py --input /Volumes/SSD/HiFiShow_June2026 --show "AxisHiFi 2026" --order "Focal,Wilson Audio,KEF"
"""

import argparse
import json
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from typing import Optional, List

# Ensure Homebrew binaries (ffmpeg, ffprobe) are on PATH
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from ingest import scan_folder
from brand import detect_brand, build_output_name
from scorer import score_clip, find_best_windows, ScoredWindow
from editor import cut_short, stitch_longform
from manifest import build_short_manifest, build_longform_manifest, save_manifest

console = Console()

# Global stop flag — set by SIGTERM or --stop signal
_stop = threading.Event()


def _handle_sigterm(sig, frame):
    console.print("\n[yellow]Stop requested — finishing current clips then exiting…[/yellow]")
    _stop.set()


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT,  _handle_sigterm)


def load_config(config_path: str = "config.json") -> dict:
    with open(config_path) as f:
        cfg = json.load(f)
    cfg.update(cfg.pop("output", {}))
    cfg.update(cfg.pop("encoding", {}))
    return cfg


def setup_output_dirs(cfg: dict, base: Path):
    for key in ["shorts_dir", "longform_dir", "transcripts_dir", "manifest_dir", "frames_dir"]:
        (base / cfg[key]).mkdir(parents=True, exist_ok=True)


def process_clip(clip, show_name: str, cfg: dict, base: Path) -> tuple:
    """Process a single clip — brand detect, score, cut shorts. Thread-safe."""
    if _stop.is_set():
        return clip, [], f"[yellow]Skipped (stopped)[/yellow]"

    frames_dir = base / cfg["frames_dir"]
    shorts_base = base / cfg["shorts_dir"]
    manifest_entries = []
    log_lines = []

    # Brand detection
    brand, product, confidence = detect_brand(clip, frames_dir, cfg["brand_dictionary"])
    clip.brand = brand
    log_lines.append(
        f"  {clip.path.name} → {brand or 'Unknown'}"
        + (f" / {product}" if product else "")
        + f" ({confidence:.0%})"
    )

    if _stop.is_set():
        return clip, [], "\n".join(log_lines)

    # Score
    scored = score_clip(clip, cfg["scoring_weights"])
    clip.scores = {"frame_count": len(scored)}

    windows = find_best_windows(
        scored,
        min_s=cfg["shorts"]["min_seconds"],
        max_s=cfg["shorts"]["max_seconds"],
        max_windows=cfg["shorts"]["max_per_clip"],
        clip_duration=clip.duration,
    )

    if not windows:
        fallback = ScoredWindow(
            start=0, end=min(cfg["shorts"]["min_seconds"], clip.duration),
            score=0.0, dominant_signal="sharpness", hook_frame_ts=0.0
        )
        windows = [fallback]

    # Cut shorts
    for i, window in enumerate(windows):
        if _stop.is_set():
            break
        suffix = f"SHORT_{i+1}" if i > 0 else "SHORT"
        base_name = build_output_name(clip, show_name, suffix)
        out_path = shorts_base / f"{base_name}.mp4"

        success = cut_short(clip, window, out_path, cfg, short_index=i)
        if success:
            clip.shorts.append(out_path)
            entry = build_short_manifest(clip, window, out_path, show_name, i, cfg)
            manifest_entries.append(entry)
            log_lines.append(
                f"    ✓ Short {i+1}: {window.dominant_signal} "
                f"{window.start:.1f}s–{window.end:.1f}s (score {window.score:.2f})"
            )
        else:
            log_lines.append(f"    ✗ Short {i+1} failed")

    return clip, manifest_entries, "\n".join(log_lines)


def run(input_dir: str, show_name: str, order: Optional[List[str]],
        config_path: str, workers: int = 2):
    cfg = load_config(config_path)
    base = Path(".")
    setup_output_dirs(cfg, base)

    console.rule(f"[bold cyan]HiFi Pipeline — {show_name}")

    # ── 1. Ingest ─────────────────────────────────────────────────────────────
    console.print(f"\n[bold]Step 1/4[/bold] Scanning folder...")
    try:
        clips = scan_folder(input_dir)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    if not clips:
        console.print(f"[red]No video files found in {input_dir}[/red]")
        console.print("[dim]Supported formats: .mp4 .mov .MP4 .MOV .m4v[/dim]")
        return

    console.print(f"  Found [cyan]{len(clips)}[/cyan] clips — processing {workers} at a time")

    manifest_entries = []
    processed_clips = []
    lock = threading.Lock()

    # ── 2-4. Concurrent per-clip processing ───────────────────────────────────
    console.print(f"\n[bold]Steps 2–3/4[/bold] Brand detect + score + cut shorts...")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Processing...", total=len(clips))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(process_clip, clip, show_name, cfg, base): clip
                for clip in clips
            }

            for future in as_completed(futures):
                if _stop.is_set():
                    # Cancel remaining futures
                    for f in futures:
                        f.cancel()
                    break

                clip, entries, log = future.result()
                console.print(log)

                with lock:
                    manifest_entries.extend(entries)
                    if entries:
                        processed_clips.append(clip)

                progress.advance(task)

    if _stop.is_set():
        console.print("[yellow]Pipeline stopped by user.[/yellow]")
        if manifest_entries:
            _save_and_summarise(manifest_entries, processed_clips, show_name, cfg, base)
        sys.exit(0)

    # ── 5. Stitch Long Form ───────────────────────────────────────────────────
    if not processed_clips:
        console.print("[red]No clips processed — skipping long form.[/red]")
    else:
        console.print(f"\n[bold]Step 4/4[/bold] Stitching long-form video...")
        safe_show = show_name.replace(" ", "_")
        longform_path = base / cfg["longform_dir"] / f"{safe_show}_LONGFORM.mp4"

        success = stitch_longform(processed_clips, longform_path, show_name, cfg, order=order)
        if success:
            lf_entry = build_longform_manifest(processed_clips, longform_path, show_name, cfg)
            manifest_entries.append(lf_entry)
            console.print(f"  ✓ Long form: [cyan]{longform_path}[/cyan]")
        else:
            console.print("  [red]✗ Long form stitch failed[/red]")

    _save_and_summarise(manifest_entries, processed_clips, show_name, cfg, base)


def _save_and_summarise(manifest_entries, processed_clips, show_name, cfg, base):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_name = f"{show_name.replace(' ', '_')}_{ts}"
    manifest_path = save_manifest(base / cfg["manifest_dir"], manifest_name, manifest_entries)

    console.rule("[bold green]Done")
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Type")
    table.add_column("File")
    table.add_column("Brand")
    table.add_column("Signal")

    for e in manifest_entries:
        table.add_row(
            e["type"].upper(),
            Path(e["file"]).name,
            e.get("brand") or ", ".join(e.get("brands_featured", [])[:3]),
            e.get("dominant_signal", "—"),
        )

    console.print(table)
    console.print(f"\n[bold]Manifest:[/bold] [cyan]{manifest_path}[/cyan]\n")


def main():
    parser = argparse.ArgumentParser(description="HiFi YouTube Pipeline")
    parser.add_argument("--input",   required=True)
    parser.add_argument("--show",    required=True)
    parser.add_argument("--order",   default="")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--config",  default="config.json")
    args = parser.parse_args()

    order = [b.strip() for b in args.order.split(",") if b.strip()] if args.order else None
    run(args.input, args.show, order, args.config, workers=args.workers)


if __name__ == "__main__":
    main()
