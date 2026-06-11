#!/usr/bin/env python3
"""
Upload queue manager.

Commands:
  python queue.py list                          — show full queue
  python queue.py approve <id> [<id> ...]       — mark entries approved
  python queue.py schedule                      — auto-schedule approved items (1 per 2 days)
  python queue.py schedule --days 3             — 1 every 3 days
  python queue.py next                          — show what uploads today
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

MANIFEST_DIR = Path("output/manifests")
STATUS_PENDING   = "pending"
STATUS_APPROVED  = "approved"
STATUS_SCHEDULED = "scheduled"
STATUS_UPLOADED  = "uploaded"

STATUS_STYLE = {
    STATUS_PENDING:   "dim",
    STATUS_APPROVED:  "yellow",
    STATUS_SCHEDULED: "cyan",
    STATUS_UPLOADED:  "green",
}


# ── Manifest helpers ──────────────────────────────────────────────────────────

def load_all_manifests() -> list[tuple[Path, list[dict]]]:
    manifests = []
    for p in sorted(MANIFEST_DIR.glob("*.json")):
        with open(p) as f:
            entries = json.load(f)
        manifests.append((p, entries))
    return manifests


def save_manifest(path: Path, entries: list[dict]):
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)


def all_entries() -> list[tuple[Path, int, dict]]:
    """Returns (manifest_path, index, entry) for every entry across all manifests."""
    result = []
    for path, entries in load_all_manifests():
        for i, e in enumerate(entries):
            result.append((path, i, e))
    return result


def find_entry(entry_id: str) -> Optional[tuple]:
    for path, i, e in all_entries():
        if e.get("id") == entry_id:
            return path, i, e
    return None


def ensure_ids():
    """Back-fill IDs on any entries that don't have one yet."""
    for path, entries in load_all_manifests():
        changed = False
        for i, e in enumerate(entries):
            if "id" not in e:
                stem = Path(e["file"]).stem[:30]
                e["id"] = f"{stem}_{i}"
                if "status" not in e:
                    e["status"] = STATUS_APPROVED if e.get("approved") else STATUS_PENDING
                changed = True
        if changed:
            save_manifest(path, entries)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_list(filter_status: Optional[str] = None):
    ensure_ids()
    entries = all_entries()
    if not entries:
        console.print("[dim]No manifests found. Run pipeline.py first.[/dim]")
        return

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("ID", style="dim", no_wrap=True, max_width=32)
    table.add_column("Type", width=8)
    table.add_column("Title", no_wrap=False)
    table.add_column("Brand")
    table.add_column("Duration")
    table.add_column("Status", width=10)
    table.add_column("Scheduled", width=12)
    table.add_column("Uploaded", width=20)

    shown = 0
    for _, _, e in entries:
        status = e.get("status", STATUS_PENDING)
        if filter_status and status != filter_status:
            continue

        style = STATUS_STYLE.get(status, "")
        dur = f"{e.get('duration_s', '—')}s" if e.get("duration_s") else "—"
        brand = e.get("brand") or ", ".join((e.get("brands_featured") or [])[:2])
        scheduled = e.get("scheduled_date", "—")
        uploaded_at = e.get("uploaded_at", "—")

        table.add_row(
            e.get("id", "?"),
            e["type"].upper(),
            e.get("title", "—"),
            brand or "—",
            dur,
            f"[{style}]{status}[/{style}]",
            scheduled,
            uploaded_at,
        )
        shown += 1

    if shown == 0:
        console.print(f"[dim]No entries with status '{filter_status}'[/dim]")
    else:
        console.print(table)
        console.print(f"\n[dim]{shown} entries[/dim]")


def cmd_approve(ids: list[str]):
    ensure_ids()
    for entry_id in ids:
        result = find_entry(entry_id)
        if not result:
            console.print(f"[red]Not found: {entry_id}[/red]")
            continue
        path, i, e = result
        entries = json.loads(path.read_text())
        entries[i]["approved"] = True
        entries[i]["status"] = STATUS_APPROVED
        save_manifest(path, entries)
        console.print(f"[green]✓ Approved:[/green] {e.get('title', entry_id)}")


def cmd_schedule(days_between: int = 2):
    ensure_ids()

    # Collect approved + already-scheduled entries across all manifests,
    # sorted so Shorts and Long forms interleave sensibly
    to_schedule = []
    for path, entries in load_all_manifests():
        for i, e in enumerate(entries):
            if e.get("status") in (STATUS_APPROVED, STATUS_SCHEDULED):
                to_schedule.append((path, i, e))

    if not to_schedule:
        console.print("[yellow]No approved entries to schedule. Run 'approve' first.[/yellow]")
        return

    # Find the latest already-scheduled date so we don't overlap
    latest = datetime.today().date()
    for _, _, e in to_schedule:
        d = e.get("scheduled_date")
        if d and d != "—":
            try:
                parsed = datetime.strptime(d, "%Y-%m-%d").date()
                if parsed > latest:
                    latest = parsed
            except ValueError:
                pass

    # Assign dates: Shorts first (higher algo reach early), then long form
    shorts = [(p, i, e) for p, i, e in to_schedule if e["type"] == "short" and not e.get("scheduled_date")]
    longforms = [(p, i, e) for p, i, e in to_schedule if e["type"] == "longform" and not e.get("scheduled_date")]

    ordered = shorts + longforms
    next_date = latest + timedelta(days=days_between) if to_schedule else datetime.today().date()

    updated = 0
    for path, i, e in ordered:
        entries = json.loads(path.read_text())
        entries[i]["scheduled_date"] = next_date.strftime("%Y-%m-%d")
        entries[i]["status"] = STATUS_SCHEDULED
        save_manifest(path, entries)
        console.print(
            f"[cyan]{next_date.strftime('%Y-%m-%d')}[/cyan]  "
            f"{e['type'].upper():8s}  {e.get('title', '?')[:60]}"
        )
        next_date += timedelta(days=days_between)
        updated += 1

    console.print(f"\n[green]✓ Scheduled {updated} videos[/green]  (1 every {days_between} days)")


def cmd_next():
    ensure_ids()
    today = datetime.today().date().strftime("%Y-%m-%d")
    due = [
        e for _, _, e in all_entries()
        if e.get("scheduled_date") == today and e.get("status") == STATUS_SCHEDULED
    ]

    if not due:
        console.print(f"[dim]Nothing scheduled for today ({today})[/dim]")

        # Show next upcoming
        upcoming = sorted(
            [e for _, _, e in all_entries() if e.get("status") == STATUS_SCHEDULED],
            key=lambda e: e.get("scheduled_date", "9999")
        )
        if upcoming:
            nxt = upcoming[0]
            console.print(f"Next up: [cyan]{nxt.get('scheduled_date')}[/cyan]  {nxt.get('title', '?')}")
        return

    for e in due:
        console.print(f"[bold green]→ Upload today:[/bold green] {e.get('title')}")
        console.print(f"  File:  [cyan]{e.get('file')}[/cyan]")
        console.print(f"  ID:    [dim]{e.get('id')}[/dim]")
        console.print(f"  Tags:  {', '.join(e.get('tags', [])[:6])}")
        console.print()
    console.print(f"[dim]Run: python upload.py --id <ID>  after uploading[/dim]")


def main():
    parser = argparse.ArgumentParser(description="HiFi upload queue manager")
    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="Show full queue")
    p_list.add_argument("--status", choices=["pending","approved","scheduled","uploaded"], default=None)

    p_approve = sub.add_parser("approve", help="Approve entries for scheduling")
    p_approve.add_argument("ids", nargs="+")

    p_sched = sub.add_parser("schedule", help="Auto-assign upload dates")
    p_sched.add_argument("--days", type=int, default=2)

    sub.add_parser("next", help="Show what to upload today")

    args = parser.parse_args()

    if args.cmd == "list":
        cmd_list(args.status)
    elif args.cmd == "approve":
        cmd_approve(args.ids)
    elif args.cmd == "schedule":
        cmd_schedule(args.days)
    elif args.cmd == "next":
        cmd_next()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
