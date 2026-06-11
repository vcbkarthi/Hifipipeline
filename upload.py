#!/usr/bin/env python3
"""
Mark a video as uploaded, move it to output/done/ with a timestamp prefix.

Usage:
  python upload.py --id <entry_id>            — mark one entry done
  python upload.py --id <id1> --id <id2>      — mark multiple done
  python upload.py --today                    — mark all of today's scheduled uploads done
"""

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()

DONE_DIR = Path("output/done")
MANIFEST_DIR = Path("output/manifests")
STATUS_UPLOADED = "uploaded"


def load_manifest(path: Path) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def save_manifest(path: Path, entries: list[dict]):
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)


def all_entries() -> list[tuple[Path, int, dict]]:
    result = []
    for p in sorted(MANIFEST_DIR.glob("*.json")):
        entries = load_manifest(p)
        for i, e in enumerate(entries):
            result.append((p, i, e))
    return result


def mark_uploaded(entry_id: str) -> bool:
    for path, i, e in all_entries():
        if e.get("id") != entry_id:
            continue

        video_path = Path(e["file"])
        if not video_path.exists():
            console.print(f"[red]Video file not found:[/red] {video_path}")
            console.print("[dim]It may have already been moved. Marking manifest only.[/dim]")
        else:
            # Move to done/ with timestamp prefix
            DONE_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            done_name = f"{ts}_{video_path.name}"
            done_path = DONE_DIR / done_name
            shutil.move(str(video_path), str(done_path))
            console.print(f"  [dim]Moved →[/dim] [cyan]{done_path}[/cyan]")

            # Update manifest entry with new location
            e["done_path"] = str(done_path)

        # Stamp the manifest
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        e["status"] = STATUS_UPLOADED
        e["uploaded_at"] = now
        e["approved"] = True

        entries = load_manifest(path)
        entries[i] = e
        save_manifest(path, entries)

        console.print(
            f"[green]✓ Uploaded:[/green] {e.get('title', entry_id)}  "
            f"[dim]({now})[/dim]"
        )
        return True

    console.print(f"[red]Entry not found:[/red] {entry_id}")
    return False


def mark_today_uploaded():
    today = datetime.today().date().strftime("%Y-%m-%d")
    due = [
        e for _, _, e in all_entries()
        if e.get("scheduled_date") == today and e.get("status") == "scheduled"
    ]

    if not due:
        console.print(f"[dim]No scheduled uploads for today ({today})[/dim]")
        return

    for e in due:
        mark_uploaded(e["id"])


def cmd_list_done():
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(DONE_DIR.glob("*.mp4"), reverse=True)
    if not files:
        console.print("[dim]No uploaded videos yet.[/dim]")
        return

    from rich.table import Table
    from rich import box
    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("Uploaded at")
    table.add_column("File")
    table.add_column("Size")

    for f in files:
        # Timestamp is first 15 chars of filename: YYYYMMDD_HHMMSS
        ts_raw = f.stem[:15]
        try:
            ts = datetime.strptime(ts_raw, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            ts = "—"
        size_mb = f.stat().st_size / 1_048_576
        table.add_row(ts, f.name[16:], f"{size_mb:.0f} MB")

    console.print(table)


def main():
    parser = argparse.ArgumentParser(description="Mark HiFi videos as uploaded")
    parser.add_argument("--id", dest="ids", action="append", default=[], metavar="ID",
                        help="Entry ID to mark uploaded (repeat for multiple)")
    parser.add_argument("--today", action="store_true",
                        help="Mark all of today's scheduled entries as uploaded")
    parser.add_argument("--done", action="store_true",
                        help="List all uploaded videos in done/ folder")
    args = parser.parse_args()

    if args.done:
        cmd_list_done()
    elif args.today:
        mark_today_uploaded()
    elif args.ids:
        for entry_id in args.ids:
            mark_uploaded(entry_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
