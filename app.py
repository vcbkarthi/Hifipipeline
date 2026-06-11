#!/usr/bin/env python3
"""
HiFi Pipeline — local review & upload UI.
Run: python app.py
Open: http://localhost:5000
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from typing import Optional
from datetime import datetime
from pathlib import Path

os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

from flask import Flask, Response, jsonify, render_template_string, request, send_file, abort

import keychain as kc
from youtube import UploadProgress, upload_video

app = Flask(__name__)

MANIFEST_DIR = Path("output/manifests")
DONE_DIR     = Path("output/done")
FRAMES_DIR   = Path("output/frames")

_upload_progress: dict[str, UploadProgress] = {}
_pipeline_log: list[str] = []
_pipeline_running = False
_pipeline_proc: Optional[subprocess.Popen] = None


# ── Folder picker (native macOS dialog) ──────────────────────────────────────

def _pick_folder(title: str) -> str:
    script = f'POSIX path of (choose folder with prompt "{title}")'
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


# ── Manifest helpers ──────────────────────────────────────────────────────────

def load_all_entries():
    entries = []
    for p in sorted(MANIFEST_DIR.glob("*.json")):
        with open(p) as f:
            data = json.load(f)
        for i, e in enumerate(data):
            e.setdefault("id", f"{Path(e['file']).stem[:30]}_{i}")
            e.setdefault("status", "approved" if e.get("approved") else "pending")
            e["_manifest"] = str(p)
            e["_index"] = i
            entries.append(e)
    return entries


def save_entry(entry: dict):
    path = Path(entry["_manifest"])
    with open(path) as f:
        data = json.load(f)
    clean = {k: v for k, v in entry.items() if not k.startswith("_")}
    data[entry["_index"]] = clean
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_entry(entry_id: str):
    for e in load_all_entries():
        if e.get("id") == entry_id:
            return e
    return None


def delete_entry(entry_id: str):
    e = get_entry(entry_id)
    if not e:
        return
    path = Path(e["_manifest"])
    idx  = e["_index"]
    with open(path) as f:
        data = json.load(f)
    if 0 <= idx < len(data):
        data.pop(idx)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def thumbnail_for(entry: dict) -> str:
    video = Path(entry.get("file", ""))
    if not video.exists():
        return ""
    thumb = FRAMES_DIR / f"thumb_{video.stem}.jpg"
    if not thumb.exists():
        ts = entry.get("thumbnail_ts") or (entry.get("duration_s") or 4) / 2
        FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(ts), "-i", str(video),
            "-vframes", "1", "-q:v", "3", str(thumb)
        ], capture_output=True)
    return f"/thumb/{video.stem}" if thumb.exists() else ""


# ── Routes — Tab 1: Pipeline ──────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/browse", methods=["POST"])
def api_browse():
    kind = request.json.get("kind", "source")
    title = "Select Source Folder" if kind == "source" else "Select Output Folder"
    path = _pick_folder(title)
    return jsonify({"path": path})


@app.route("/api/pipeline/run", methods=["POST"])
def api_pipeline_run():
    global _pipeline_running, _pipeline_log
    if _pipeline_running:
        return jsonify({"ok": False, "error": "Pipeline already running"}), 400

    data = request.json or {}
    source      = data.get("source", "").strip()
    output      = data.get("output", "").strip()
    show        = data.get("show", "").strip()
    order       = data.get("order", "").strip()
    orientation  = data.get("orientation", "all").strip()
    skip_brand   = data.get("skip_brand", True)

    if not source or not Path(source).exists():
        return jsonify({"ok": False, "error": "Source folder not found"}), 400
    if not show:
        return jsonify({"ok": False, "error": "Show name is required"}), 400

    workers = data.get("workers", 2)
    _pipeline_log = []
    _pipeline_running = True

    def run():
        global _pipeline_running, _pipeline_proc
        try:
            cmd = [
                sys.executable, "pipeline.py",
                "--input", source,
                "--show", show,
                "--workers", str(workers),
                "--orientation", orientation,
            ]
            if skip_brand:
                cmd += ["--skip-brand"]
            if order:
                cmd += ["--order", order]
            if output:
                Path(output).mkdir(parents=True, exist_ok=True)

            _pipeline_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            for line in _pipeline_proc.stdout:
                _pipeline_log.append(line.rstrip())
            _pipeline_proc.wait()
            _pipeline_log.append(f"\n--- Pipeline finished (exit {_pipeline_proc.returncode}) ---")
        except Exception as ex:
            _pipeline_log.append(f"ERROR: {ex}")
        finally:
            _pipeline_running = False
            _pipeline_proc = None

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/pipeline/log")
def api_pipeline_log():
    def stream():
        import time
        sent = 0
        while _pipeline_running or sent < len(_pipeline_log):
            while sent < len(_pipeline_log):
                line = _pipeline_log[sent].replace("\n", " ")
                yield f"data: {json.dumps({'line': line, 'done': False})}\n\n"
                sent += 1
            if not _pipeline_running:
                yield f"data: {json.dumps({'line': '', 'done': True})}\n\n"
                break
            time.sleep(0.3)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/pipeline/status")
def api_pipeline_status():
    return jsonify({"running": _pipeline_running})


@app.route("/api/pipeline/stop", methods=["POST"])
def api_pipeline_stop():
    global _pipeline_proc
    if _pipeline_proc and _pipeline_proc.poll() is None:
        _pipeline_proc.terminate()   # sends SIGTERM → pipeline finishes current clips cleanly
        _pipeline_log.append("\n--- Stop requested — finishing current clips… ---")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "No pipeline running"})


# ── Routes — Tab 1: Video review & upload ────────────────────────────────────

@app.route("/api/entries")
def api_entries():
    result = []
    for e in load_all_entries():
        prog = _upload_progress.get(e.get("id"))
        result.append({
            "id":          e.get("id"),
            "type":        e.get("type"),
            "title":       e.get("title"),
            "brand":       e.get("brand") or ", ".join((e.get("brands_featured") or [])[:2]),
            "duration_s":  e.get("duration_s"),
            "status":      e.get("status", "pending"),
            "signal":      e.get("dominant_signal", "—"),
            "uploaded_at": e.get("uploaded_at"),
            "youtube_url": e.get("youtube_url", ""),
            "tags":        e.get("tags", []),
            "description": e.get("description", ""),
            "file_exists": Path(e.get("file", "")).exists(),
            "thumb":       thumbnail_for(e),
            "uploading":   prog.status == "uploading" if prog else False,
        })
    return jsonify(result)


@app.route("/api/approve/<entry_id>", methods=["POST"])
def api_approve(entry_id):
    e = get_entry(entry_id)
    if not e:
        return jsonify({"ok": False, "error": "Not found"}), 404
    data = request.json or {}
    e["approved"] = True
    e["status"] = "approved"
    if data.get("brand"):
        e["brand"] = data["brand"].strip()
    save_entry(e)
    return jsonify({"ok": True})


@app.route("/api/brand/<entry_id>", methods=["POST"])
def api_set_brand(entry_id):
    e = get_entry(entry_id)
    if not e:
        return jsonify({"ok": False, "error": "Not found"}), 404
    brand = (request.json or {}).get("brand", "").strip()
    if brand:
        e["brand"] = brand
        save_entry(e)
    return jsonify({"ok": True})


@app.route("/api/upload/<entry_id>", methods=["POST"])
def api_upload(entry_id):
    e = get_entry(entry_id)
    if not e:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if e.get("status") == "uploaded":
        return jsonify({"ok": False, "error": "Already uploaded"}), 400
    if not kc.all_set():
        return jsonify({"ok": False, "error": "YouTube credentials not configured. Go to Settings tab."}), 400

    privacy = (request.json or {}).get("privacy", "private")
    prog = UploadProgress(entry_id)
    _upload_progress[entry_id] = prog

    def run():
        try:
            video_id = upload_video(e, prog, privacy=privacy)
            video_path = Path(e.get("file", ""))
            done_path = ""
            if video_path.exists():
                DONE_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = DONE_DIR / f"{ts}_{video_path.name}"
                shutil.move(str(video_path), str(dest))
                done_path = str(dest)
            fresh = get_entry(entry_id)
            if fresh:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                fresh.update({"status": "uploaded", "uploaded_at": now,
                               "youtube_id": video_id,
                               "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
                               "done_path": done_path})
                save_entry(fresh)
        except Exception as ex:
            prog.status = "error"
            prog.error  = str(ex)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/upload/progress/<entry_id>")
def api_upload_progress(entry_id):
    def stream():
        import time
        last = -1
        while True:
            prog = _upload_progress.get(entry_id)
            if not prog:
                yield f"data: {json.dumps({'status': 'idle'})}\n\n"; break
            if prog.percent != last or prog.status in ("done", "error"):
                yield f"data: {json.dumps({'status': prog.status, 'percent': prog.percent, 'youtube_url': prog.youtube_url, 'error': prog.error})}\n\n"
                last = prog.percent
            if prog.status in ("done", "error"):
                break
            time.sleep(0.5)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Routes — Tab 2: Settings ──────────────────────────────────────────────────

@app.route("/api/settings/status")
def api_settings_status():
    return jsonify({
        "keychain": kc.status(),
        "all_set": kc.all_set(),
    })


@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    data = request.json or {}
    saved = []
    for key in ("yt_client_id", "yt_client_secret"):
        val = data.get(key, "").strip()
        if val:
            kc.save(key, val)
            saved.append(key)
    return jsonify({"ok": True, "saved": saved})


@app.route("/api/settings/clear", methods=["POST"])
def api_settings_clear():
    for key in ("yt_client_id", "yt_client_secret"):
        kc.delete(key)
    # Also remove generated client_secret.json
    p = Path("credentials/client_secret.json")
    if p.exists():
        p.unlink()
    return jsonify({"ok": True})


@app.route("/api/entry/delete/<entry_id>", methods=["POST"])
def api_entry_delete(entry_id):
    e = get_entry(entry_id)
    if not e:
        return jsonify({"ok": False, "error": "Not found"}), 404
    # Delete the generated output file (never touches source)
    for key in ("file", "done_path"):
        val = e.get(key, "")
        if not val:
            continue
        p = Path(val)
        if p.exists() and p.is_file():
            p.unlink()
    # Remove from manifest
    delete_entry(entry_id)
    return jsonify({"ok": True})


# ── Static helpers ────────────────────────────────────────────────────────────

@app.route("/video/<entry_id>")
def serve_video(entry_id):
    e = get_entry(entry_id)
    if not e: abort(404)
    p = Path(e.get("file", ""))
    if not p.exists():
        p = Path(e.get("done_path", ""))
    if not p.exists(): abort(404)
    return send_file(p, mimetype="video/mp4", conditional=True)


@app.route("/thumb/<stem>")
def serve_thumb(stem):
    p = FRAMES_DIR / f"thumb_{stem}.jpg"
    if not p.exists(): abort(404)
    return send_file(p, mimetype="image/jpeg")


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HiFi Pipeline</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:#0f0f0f; --surface:#1a1a1a; --surface2:#242424; --surface3:#2e2e2e;
  --border:#333; --text:#e8e8e8; --muted:#777; --muted2:#555;
  --accent:#f5a623; --green:#4caf50; --red:#f44336;
  --blue:#42a5f5; --purple:#ab47bc; --yt:#ff0000;
  --radius:10px;
}
body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; min-height:100vh; font-size:14px; }

/* ── Top bar ── */
.topbar { display:flex; align-items:center; gap:0; padding:0 24px;
  border-bottom:1px solid var(--border); background:var(--surface); height:52px; }
.logo { font-size:16px; font-weight:700; color:var(--text); margin-right:32px; }
.logo span { color:var(--accent); }
.tab { padding:0 18px; height:52px; display:flex; align-items:center; gap:8px;
  cursor:pointer; color:var(--muted); font-size:13px; font-weight:500;
  border-bottom:2px solid transparent; transition:all .15s; user-select:none; }
.tab:hover { color:var(--text); }
.tab.active { color:var(--accent); border-bottom-color:var(--accent); }
.tab svg { width:15px; height:15px; }

/* ── Panels ── */
.panel { display:none; }
.panel.active { display:flex; }

/* ══════════════════════════════════════════════════
   TAB 1 — PIPELINE + REVIEW
══════════════════════════════════════════════════ */
#tab-pipeline { flex-direction:column; height:calc(100vh - 52px); }

/* Pipeline setup bar */
.setup-bar { background:var(--surface); border-bottom:1px solid var(--border);
  padding:16px 24px; display:flex; flex-direction:column; gap:12px; flex-shrink:0; }
.setup-row { display:flex; gap:12px; align-items:flex-end; flex-wrap:wrap; }
.field { display:flex; flex-direction:column; gap:5px; flex:1; min-width:200px; }
.field label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; }
.folder-input { display:flex; gap:6px; }
.folder-input input { flex:1; background:var(--surface2); border:1px solid var(--border);
  color:var(--text); border-radius:6px; padding:8px 12px; font-size:13px; font-family:monospace; }
.folder-input input:focus { outline:none; border-color:var(--accent); }
.folder-input input[readonly] { cursor:default; opacity:.75; }
.folder-input input[readonly]:focus { border-color:var(--border); }
.btn-browse { background:var(--surface3); border:1px solid var(--border); color:var(--text);
  border-radius:6px; padding:8px 14px; cursor:pointer; font-size:13px; white-space:nowrap;
  transition:background .15s; }
.btn-browse:hover { background:var(--border); }
.field input[type=text] { background:var(--surface2); border:1px solid var(--border);
  color:var(--text); border-radius:6px; padding:8px 12px; font-size:13px; }
.field input[type=text]:focus { outline:none; border-color:var(--accent); }
.btn-run { background:var(--accent); color:#000; border:none; border-radius:8px;
  padding:10px 28px; font-size:14px; font-weight:700; cursor:pointer;
  transition:opacity .15s; white-space:nowrap; align-self:flex-end; }
.btn-run:hover { opacity:.88; }
.btn-run:disabled { opacity:.4; cursor:not-allowed; }

/* Log console */
.log-console { background:#0a0a0a; border-bottom:1px solid var(--border);
  font-family:monospace; font-size:12px; color:#aaa; padding:10px 18px;
  height:130px; overflow-y:auto; flex-shrink:0; line-height:1.6; }
.log-console .log-ok  { color:#4caf50; }
.log-console .log-err { color:#f44336; }
.log-console .log-dim { color:#555; }

/* Review grid + detail — fills remaining height */
.review-area { display:grid; grid-template-columns:1fr 400px; flex:1; overflow:hidden; }

.grid-pane { overflow-y:auto; padding:18px;
  display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
  gap:12px; align-content:start; }

.card { background:var(--surface); border:1.5px solid var(--border); border-radius:var(--radius);
  cursor:pointer; transition:border-color .15s,transform .12s; overflow:hidden; }
.card:hover { border-color:#555; transform:translateY(-2px); }
.card.selected { border-color:var(--accent); box-shadow:0 0 0 2px #f5a62322; }
.card-thumb { width:100%; aspect-ratio:9/16; background:#111; position:relative; overflow:hidden; }
.card-thumb img { width:100%; height:100%; object-fit:cover; }
.card-thumb .no-thumb { display:flex; align-items:center; justify-content:center;
  height:100%; color:var(--muted2); font-size:30px; }
.badge { position:absolute; top:8px; left:8px; padding:3px 8px; border-radius:4px;
  font-size:10px; font-weight:700; text-transform:uppercase; }
.badge-short    { background:var(--purple); color:#fff; }
.badge-longform { background:var(--blue); color:#fff; }
.status-dot { position:absolute; top:8px; right:8px; width:9px; height:9px; border-radius:50%; }
.dot-pending  { background:var(--muted2); }
.dot-approved { background:var(--accent); }
.dot-uploaded { background:var(--green); }
.dot-uploading{ background:var(--yt); animation:pulse 1s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
.card-body { padding:9px 11px; }
.card-title { font-size:11px; font-weight:600; line-height:1.4;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.card-meta { font-size:10px; color:var(--muted); margin-top:3px; }

.detail-pane { border-left:1px solid var(--border); background:var(--surface);
  overflow-y:auto; display:flex; flex-direction:column; }
.empty-detail { flex:1; display:flex; flex-direction:column; align-items:center;
  justify-content:center; color:var(--muted); gap:10px; font-size:13px; }
.detail-video { width:100%; aspect-ratio:9/16; background:#000; flex-shrink:0; }
.detail-video video { width:100%; height:100%; object-fit:contain; }
.detail-body { padding:14px; flex:1; display:flex; flex-direction:column; gap:11px; }
.detail-title { font-size:14px; font-weight:700; line-height:1.4; }
.detail-meta { display:grid; grid-template-columns:1fr 1fr; gap:7px; }
.meta-item { background:var(--surface2); border-radius:6px; padding:7px 10px; }
.meta-label { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.4px; }
.meta-value { font-size:12px; margin-top:2px; font-weight:500; }
.tags { display:flex; flex-wrap:wrap; gap:5px; }
.tag { background:var(--surface2); border:1px solid var(--border); border-radius:20px;
  font-size:10px; padding:2px 8px; color:var(--muted); }
.description { font-size:11px; color:var(--muted); line-height:1.6;
  background:var(--surface2); border-radius:6px; padding:9px 11px;
  max-height:80px; overflow-y:auto; }
.status-banner { text-align:center; padding:8px 12px; border-radius:6px;
  font-size:12px; font-weight:600; }
.banner-uploaded { background:#4caf5018; color:var(--green); border:1px solid #4caf5033; }
.banner-approved { background:#f5a62318; color:var(--accent); border:1px solid #f5a62333; }
.actions { padding:12px 14px; border-top:1px solid var(--border);
  display:flex; flex-direction:column; gap:9px; }
.privacy-row { display:flex; gap:8px; align-items:center; }
.privacy-row label { font-size:11px; color:var(--muted); white-space:nowrap; }
.privacy-row select { background:var(--surface2); border:1px solid var(--border);
  color:var(--text); border-radius:6px; padding:6px 10px; font-size:12px; flex:1; }
.btn { display:flex; align-items:center; justify-content:center; gap:7px;
  padding:11px 16px; border-radius:8px; font-size:13px; font-weight:600;
  cursor:pointer; border:none; transition:opacity .15s; width:100%; }
.btn:hover { opacity:.87; }
.btn:disabled { opacity:.32; cursor:not-allowed; }
.btn-approve { background:var(--accent); color:#000; }
.btn-upload  { background:var(--yt); color:#fff; }
.btn-done    { background:var(--surface2); color:var(--muted); border:1px solid var(--border); cursor:default; }
.progress-wrap { display:none; flex-direction:column; gap:5px; }
.progress-wrap.visible { display:flex; }
.prog-bg { background:var(--surface3); border-radius:4px; height:7px; overflow:hidden; }
.prog-fill { background:var(--yt); height:100%; border-radius:4px; transition:width .3s; width:0%; }
.prog-label { font-size:11px; color:var(--muted); text-align:center; }
.yt-link { display:flex; align-items:center; justify-content:center; gap:6px;
  padding:9px; background:#ff000018; border:1px solid #ff000033;
  border-radius:8px; color:var(--yt); font-size:12px; font-weight:600;
  text-decoration:none; }
.yt-link:hover { background:#ff000028; }

/* ══════════════════════════════════════════════════
   TAB 2 — SETTINGS
══════════════════════════════════════════════════ */
#tab-settings { flex-direction:column; align-items:center; padding:40px 24px;
  height:calc(100vh - 52px); overflow-y:auto; }
.settings-card { background:var(--surface); border:1px solid var(--border);
  border-radius:14px; width:100%; max-width:560px; overflow:hidden;
  margin-bottom:20px; }
.settings-header { padding:18px 22px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; gap:12px; }
.settings-header h2 { font-size:15px; font-weight:700; }
.settings-header p  { font-size:12px; color:var(--muted); margin-top:2px; }
.settings-icon { width:36px; height:36px; border-radius:8px;
  display:flex; align-items:center; justify-content:center; flex-shrink:0; }
.icon-yt  { background:#ff000022; color:var(--yt); }
.icon-key { background:#f5a62322; color:var(--accent); }
.settings-body { padding:20px 22px; display:flex; flex-direction:column; gap:14px; }
.s-field { display:flex; flex-direction:column; gap:5px; }
.s-field label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; }
.s-input-row { display:flex; gap:8px; }
.s-input { flex:1; background:var(--surface2); border:1px solid var(--border);
  color:var(--text); border-radius:7px; padding:9px 12px; font-size:13px;
  font-family:monospace; }
.s-input:focus { outline:none; border-color:var(--accent); }
.s-input.filled { border-color:#4caf5055; }
.eye-btn { background:var(--surface2); border:1px solid var(--border); color:var(--muted);
  border-radius:7px; padding:0 12px; cursor:pointer; font-size:15px;
  transition:background .15s; }
.eye-btn:hover { background:var(--border); }
.settings-footer { padding:14px 22px; border-top:1px solid var(--border);
  display:flex; gap:10px; align-items:center; }
.btn-save  { background:var(--accent); color:#000; border:none; border-radius:7px;
  padding:9px 22px; font-size:13px; font-weight:700; cursor:pointer; transition:opacity .15s; }
.btn-save:hover { opacity:.88; }
.btn-clear { background:transparent; border:1px solid var(--border); color:var(--muted);
  border-radius:7px; padding:9px 16px; font-size:13px; cursor:pointer; transition:all .15s; }
.btn-clear:hover { border-color:var(--red); color:var(--red); }
.cred-status { margin-left:auto; display:flex; align-items:center; gap:6px;
  font-size:12px; font-weight:600; }
.cred-status.ok  { color:var(--green); }
.cred-status.missing { color:var(--muted); }
.setup-guide { background:var(--surface2); border-radius:8px; padding:13px 15px;
  font-size:12px; color:var(--muted); line-height:1.7; }
.setup-guide a { color:var(--accent); text-decoration:none; }
.setup-guide a:hover { text-decoration:underline; }
.setup-guide ol { padding-left:18px; }
.setup-guide li { margin-bottom:3px; }

/* Toast */
.toast { position:fixed; bottom:24px; right:24px; background:var(--surface);
  border:1px solid var(--border); border-radius:8px; padding:11px 18px;
  font-size:13px; box-shadow:0 4px 24px #0008; transform:translateY(80px);
  transition:transform .25s; z-index:200; }
.toast.show { transform:translateY(0); }
.toast.ok  { border-color:var(--green); }
.toast.err { border-color:var(--red); }
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">HiFi <span>Pipeline</span></div>
  <div class="tab active" data-tab="pipeline" onclick="switchTab('pipeline')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <polygon points="5,3 19,12 5,21"/></svg>
    Pipeline & Review
  </div>
  <div class="tab" data-tab="settings" onclick="switchTab('settings')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </svg>
    Settings
  </div>
</div>

<!-- ═══════════════ TAB 1: PIPELINE ═══════════════ -->
<div class="panel active" id="tab-pipeline">

  <div class="setup-bar">
    <div class="setup-row">
      <div class="field">
        <label>Source Folder (your SSD footage)</label>
        <div class="folder-input">
          <input type="text" id="src-path" placeholder="/Volumes/SSD/HiFiShow_June2026" readonly>
          <button class="btn-browse" onclick="browse('source')">Browse…</button>
        </div>
      </div>
      <div class="field">
        <label>Output Folder</label>
        <div class="folder-input">
          <input type="text" id="out-path" placeholder="Default: ./output" readonly>
          <button class="btn-browse" onclick="browse('output')">Browse…</button>
        </div>
      </div>
    </div>
    <div class="setup-row">
      <div class="field" style="max-width:260px">
        <label>Show Name</label>
        <input type="text" id="show-name" placeholder='e.g. "AxisHiFi 2026"'>
      </div>
      <div class="field">
        <label>Room Order for Long Form (optional, comma-separated brands)</label>
        <input type="text" id="room-order" placeholder="e.g. Focal, Wilson Audio, KEF, McIntosh">
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <label style="font-size:11px;color:var(--muted)">
          <input type="checkbox" id="skip-brand" checked style="margin-right:4px">
          Skip brand detection (faster)
        </label>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <label style="font-size:11px;color:var(--muted)">Orientation:</label>
        <select id="orientation-select" style="background:var(--surface2);border:1px solid var(--border);
          color:var(--text);border-radius:6px;padding:6px 10px;font-size:13px;">
          <option value="all" selected>All clips</option>
          <option value="landscape">Landscape only (16:9)</option>
          <option value="portrait">Portrait only (9:16)</option>
        </select>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <label style="font-size:11px;color:var(--muted)">Workers:</label>
        <select id="workers-select" style="background:var(--surface2);border:1px solid var(--border);
          color:var(--text);border-radius:6px;padding:6px 10px;font-size:13px;">
          <option value="1">1 — safe</option>
          <option value="2" selected>2 — faster</option>
          <option value="3">3 — max</option>
        </select>
      </div>
      <button class="btn-run" id="btn-run" onclick="runPipeline()">▶ Run Pipeline</button>
      <button id="btn-stop" onclick="stopPipeline()"
        style="display:none;background:var(--red);color:#fff;border:none;border-radius:8px;
        padding:10px 20px;font-size:14px;font-weight:700;cursor:pointer;">■ Stop</button>
    </div>
  </div>

  <div class="log-console" id="log-console">
    <span class="log-dim">Pipeline output will appear here…</span>
  </div>

  <div class="review-area">
    <div class="grid-pane" id="grid"></div>
    <div class="detail-pane" id="detail">
      <div class="empty-detail">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>
        </svg>
        <span>Select a video to review</span>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════ TAB 2: SETTINGS ═══════════════ -->
<div class="panel" id="tab-settings">

  <!-- YouTube OAuth card -->
  <div class="settings-card">
    <div class="settings-header">
      <div class="settings-icon icon-yt">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
          <path d="M23 7s-.3-2-1.2-2.8c-1.1-1.2-2.4-1.2-3-1.3C16.2 2.8 12 2.8 12 2.8s-4.2 0-6.8.1c-.6.1-1.9.1-3 1.3C1.3 5 1 7 1 7S.7 9.1.7 11.3v2c0 2.1.3 4.2.3 4.2s.3 2 1.2 2.8c1.1 1.2 2.6 1.1 3.3 1.2C7.5 21.7 12 21.7 12 21.7s4.2 0 6.8-.2c.6-.1 1.9-.1 3-1.3.9-.8 1.2-2.8 1.2-2.8s.3-2.1.3-4.2v-2C23.3 9.1 23 7 23 7zM9.7 15.5v-7.3l8.1 3.7-8.1 3.6z"/>
        </svg>
      </div>
      <div>
        <h2>YouTube OAuth</h2>
        <p>Client credentials from Google Cloud Console — stored in macOS Keychain</p>
      </div>
    </div>
    <div class="settings-body">
      <div class="setup-guide">
        <strong>One-time setup:</strong>
        <ol>
          <li>Go to <a href="#" onclick="return false">console.cloud.google.com</a> → New Project → <strong>Enable YouTube Data API v3</strong></li>
          <li>Credentials → Create OAuth 2.0 Client ID → <strong>Desktop app</strong></li>
          <li>Copy the <strong>Client ID</strong> and <strong>Client Secret</strong> into the fields below</li>
          <li>First upload opens a browser window for Google sign-in — once only</li>
        </ol>
      </div>
      <div class="s-field">
        <label>Client ID</label>
        <input class="s-input" id="yt-client-id" type="text" placeholder="xxxx.apps.googleusercontent.com" autocomplete="off">
      </div>
      <div class="s-field">
        <label>Client Secret</label>
        <div class="s-input-row">
          <input class="s-input" id="yt-client-secret" type="password" placeholder="GOCSPX-…" autocomplete="new-password">
          <button class="eye-btn" onclick="toggleSecret('yt-client-secret', this)">👁</button>
        </div>
      </div>
    </div>
    <div class="settings-footer">
      <button class="btn-save" onclick="saveCredentials()">Save to Keychain</button>
      <button class="btn-clear" onclick="clearCredentials()">Clear</button>
      <div class="cred-status missing" id="cred-status">
        <span id="cred-status-dot">●</span>
        <span id="cred-status-text">Not configured</span>
      </div>
    </div>
  </div>

  <!-- Keychain note -->
  <div class="settings-card">
    <div class="settings-header">
      <div class="settings-icon icon-key">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>
        </svg>
      </div>
      <div>
        <h2>Security</h2>
        <p>How your credentials are stored</p>
      </div>
    </div>
    <div class="settings-body">
      <div class="setup-guide">
        <strong>Secrets are stored in macOS Keychain</strong> — the same secure enclave used by Safari and 1Password.
        They are never written to any file on disk, never leave your Mac, and are protected by your login password.<br><br>
        The only file written is a temporary <code>credentials/client_secret.json</code> that is regenerated each run
        from Keychain and is excluded from git via <code>.gitignore</code>.
      </div>
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
let entries = [];
let selectedId = null;
let activeSSE = null;

// ── Tab switching ─────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'settings') loadCredentialStatus();
}

// ── Browse (native folder picker) ─────────────────────────────────────────
async function browse(kind) {
  const res  = await fetch('/api/browse', { method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({kind}) });
  const data = await res.json();
  if (data.path) {
    document.getElementById(kind === 'source' ? 'src-path' : 'out-path').value = data.path;
  }
}

// ── Pipeline ──────────────────────────────────────────────────────────────
async function runPipeline() {
  const source = document.getElementById('src-path').value.trim();
  const output = document.getElementById('out-path').value.trim();
  const show   = document.getElementById('show-name').value.trim();
  const order  = document.getElementById('room-order').value.trim();

  if (!source) { toast('Select a source folder first', 'err'); return; }
  if (!show)   { toast('Enter a show name', 'err'); return; }

  const workers     = document.getElementById('workers-select').value;
  const orientation = document.getElementById('orientation-select').value;
  const skip_brand  = document.getElementById('skip-brand').checked;
  const btn         = document.getElementById('btn-run');
  const btnStop = document.getElementById('btn-stop');
  btn.disabled = true;
  btn.textContent = '⏳ Running…';
  btnStop.style.display = 'inline-block';

  const log = document.getElementById('log-console');
  log.innerHTML = '';

  const res = await fetch('/api/pipeline/run', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({source, output, show, order, workers: parseInt(workers), orientation, skip_brand})
  });
  const data = await res.json();
  if (!data.ok) {
    toast(data.error, 'err');
    btn.disabled = false; btn.textContent = '▶ Run Pipeline';
    btnStop.style.display = 'none';
    return;
  }

  const sse = new EventSource('/api/pipeline/log');
  sse.onmessage = (evt) => {
    const d = JSON.parse(evt.data);
    if (d.line) {
      const el = document.createElement('div');
      el.textContent = d.line;
      if (d.line.includes('✓')) el.className = 'log-ok';
      else if (d.line.includes('✗') || d.line.toLowerCase().includes('error')) el.className = 'log-err';
      else if (d.line.startsWith('─') || d.line.startsWith('━')) el.className = 'log-dim';
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
    }
    if (d.done) {
      sse.close();
      btn.disabled = false;
      btn.textContent = '▶ Run Pipeline';
      btnStop.style.display = 'none';
      toast('Pipeline complete ✓', 'ok');
      loadEntries();
    }
  };
}

async function stopPipeline() {
  const btnStop = document.getElementById('btn-stop');
  btnStop.disabled = true;
  btnStop.textContent = '⏳ Stopping…';
  await fetch('/api/pipeline/stop', {method:'POST'});
  toast('Stop signal sent — finishing current clips…', 'ok');
}

// ── Video grid ────────────────────────────────────────────────────────────
async function loadEntries() {
  const res = await fetch('/api/entries');
  entries = await res.json();
  renderGrid();
}

function renderGrid() {
  const grid = document.getElementById('grid');
  if (!entries.length) {
    grid.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:20px">No videos yet — run the pipeline first.</div>';
    return;
  }
  // Sort: uploaded to bottom, rest by score descending
  const sorted = [...entries].sort((a, b) => {
    if (a.status === 'uploaded' && b.status !== 'uploaded') return 1;
    if (b.status === 'uploaded' && a.status !== 'uploaded') return -1;
    return (b.score || 0) - (a.score || 0);
  });
  grid.innerHTML = sorted.map(cardHTML).join('');
  grid.querySelectorAll('.card').forEach(el =>
    el.addEventListener('click', () => select(el.dataset.id))
  );
}

function cardHTML(e) {
  const thumb = e.thumb
    ? `<img src="${e.thumb}" loading="lazy">`
    : `<div class="no-thumb">🎵</div>`;
  const badge = e.type === 'short'
    ? `<span class="badge badge-short">Short</span>`
    : `<span class="badge badge-longform">Long</span>`;
  const dot = e.uploading ? 'dot-uploading' : `dot-${e.status}`;
  const dur = e.duration_s ? ` · ${e.duration_s}s` : '';
  const scorePct = Math.round((e.score || 0) * 100);
  const scoreColor = scorePct >= 60 ? 'var(--green)' : scorePct >= 30 ? 'var(--accent)' : 'var(--muted)';
  const scoreBar = e.type === 'short' ? `
    <div style="margin:5px 0 2px;display:flex;align-items:center;gap:6px">
      <div style="flex:1;height:3px;background:var(--surface3);border-radius:2px;overflow:hidden">
        <div style="width:${scorePct}%;height:100%;background:${scoreColor};border-radius:2px"></div>
      </div>
      <span style="font-size:10px;color:${scoreColor};font-weight:600">${scorePct}</span>
    </div>` : '';
  const delBtn = e.status !== 'uploaded'
    ? `<button onclick="event.stopPropagation();deleteEntry('${e.id}')"
        title="Delete"
        style="position:absolute;top:6px;right:6px;background:#0008;border:none;color:#fff;
        border-radius:50%;width:24px;height:24px;font-size:13px;cursor:pointer;line-height:24px;
        text-align:center;padding:0">✕</button>` : '';
  return `
    <div class="card ${e.id===selectedId?'selected':''}" data-id="${e.id}" style="position:relative">
      <div class="card-thumb">${thumb}${badge}
        <div class="status-dot ${dot}"></div>
        ${delBtn}
      </div>
      <div class="card-body">
        <div class="card-title">${e.title||'—'}</div>
        ${scoreBar}
        <div class="card-meta">${e.brand||''}${dur}</div>
      </div>
    </div>`;
}

function select(id) {
  selectedId = id;
  renderGrid();
  renderDetail(entries.find(e => e.id === id));
}

function renderDetail(e) {
  if (!e) return;
  const tags = (e.tags||[]).map(t=>`<span class="tag">#${t}</span>`).join('');
  const dur  = e.duration_s ? `${e.duration_s}s` : '—';
  let banner = '';
  if (e.status === 'uploaded') banner = `<div class="status-banner banner-uploaded">✓ Uploaded ${e.uploaded_at||''}</div>`;
  else if (e.status === 'approved') banner = `<div class="status-banner banner-approved">✓ Approved — ready to upload</div>`;

  const approveBtn = e.status === 'pending'
    ? `<button class="btn btn-approve" onclick="approve('${e.id}')">✓ Approve</button>` : '';

  const privacyRow = e.status !== 'uploaded' ? `
    <div class="privacy-row">
      <label>Upload as:</label>
      <select id="privacy-select">
        <option value="private">Private</option>
        <option value="unlisted">Unlisted</option>
        <option value="public">Public</option>
      </select>
    </div>` : '';

  const uploadBtn = e.status === 'uploaded'
    ? `<button class="btn btn-done" disabled>✓ On YouTube</button>`
    : `<button class="btn btn-upload" id="btn-upload"
        ${e.status==='pending'?'disabled':''}
        onclick="startUpload('${e.id}')">▲ Upload to YouTube</button>`;

  const ytLink = e.youtube_url
    ? `<a href="${e.youtube_url}" target="_blank" class="yt-link">▶ View on YouTube</a>` : '';

  document.getElementById('detail').innerHTML = `
    <div class="detail-video">
      <video controls playsinline src="/video/${e.id}" preload="metadata"></video>
    </div>
    <div class="detail-body">
      <div class="detail-title">${e.title||'—'}</div>
      ${banner}
      <div class="detail-meta">
        <div class="meta-item"><div class="meta-label">Type</div><div class="meta-value">${(e.type||'').toUpperCase()}</div></div>
        <div class="meta-item"><div class="meta-label">Brand <span style="color:var(--muted);font-size:9px">(editable)</span></div>
          <input id="brand-input" value="${e.brand||''}" placeholder="Type brand name…"
            style="background:var(--surface3);border:1px solid var(--border);color:var(--text);
            border-radius:5px;padding:3px 7px;font-size:12px;width:100%;margin-top:2px"
            onchange="setBrand('${e.id}', this.value)">
        </div>
        <div class="meta-item"><div class="meta-label">Duration</div><div class="meta-value">${dur}</div></div>
        <div class="meta-item"><div class="meta-label">Signal</div><div class="meta-value">${e.signal||'—'}</div></div>
      </div>
      <div class="description">${e.description||'—'}</div>
      <div class="tags">${tags}</div>
    </div>
    <div class="actions">
      ${approveBtn}
      ${privacyRow}
      <div class="progress-wrap" id="progress-wrap">
        <div class="prog-bg"><div class="prog-fill" id="prog-fill"></div></div>
        <div class="prog-label" id="prog-label">Uploading…</div>
      </div>
      ${uploadBtn}
      ${ytLink}
      ${e.status !== 'uploaded' ? `<button class="btn" onclick="deleteEntry('${e.id}')"
        style="background:transparent;border:1px solid var(--red);color:var(--red);margin-top:6px">
        🗑 Delete</button>` : ''}
    </div>`;
}

async function setBrand(id, brand) {
  await fetch(`/api/brand/${id}`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({brand})});
  // Update local entry so card reflects change without full reload
  const e = entries.find(x => x.id === id);
  if (e) { e.brand = brand; renderGrid(); }
}

async function deleteEntry(id) {
  if (!confirm('Delete this clip? The output file will be removed (source footage is never touched).')) return;
  const r = await fetch(`/api/entry/delete/${id}`, {method:'POST'});
  if ((await r.json()).ok) {
    toast('Deleted','ok');
    selectedId = null;
    document.getElementById('detail').innerHTML = '<div class="empty-detail"><span>Select a video to review</span></div>';
    await loadEntries();
  }
}

async function approve(id) {
  const brand = document.getElementById('brand-input')?.value.trim() || '';
  const r = await fetch(`/api/approve/${id}`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({brand})});
  if ((await r.json()).ok) { toast('Approved ✓','ok'); await loadEntries(); }
}

async function startUpload(id) {
  const privacy = document.getElementById('privacy-select')?.value || 'private';
  document.getElementById('btn-upload').disabled = true;
  const r = await fetch(`/api/upload/${id}`, {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({privacy})});
  const d = await r.json();
  if (!d.ok) { toast('Upload error: '+d.error,'err'); document.getElementById('btn-upload').disabled=false; return; }
  toast('Upload started ▲','ok');
  watchProgress(id);
}

function watchProgress(id) {
  if (activeSSE) activeSSE.close();
  const wrap = document.getElementById('progress-wrap');
  const fill = document.getElementById('prog-fill');
  const lbl  = document.getElementById('prog-label');
  if (wrap) wrap.classList.add('visible');
  activeSSE = new EventSource(`/api/upload/progress/${id}`);
  activeSSE.onmessage = async (evt) => {
    const d = JSON.parse(evt.data);
    if (fill) fill.style.width = d.percent + '%';
    if (lbl)  lbl.textContent = d.status === 'authenticating' ? 'Opening Google sign-in…'
      : d.status === 'done' ? 'Upload complete ✓'
      : d.status === 'error' ? '✗ '+d.error
      : `Uploading… ${d.percent}%`;
    if (d.status === 'done')  { activeSSE.close(); toast('Uploaded ✓','ok'); await loadEntries(); }
    if (d.status === 'error') { activeSSE.close(); toast(d.error,'err'); }
  };
}

// ── Settings ──────────────────────────────────────────────────────────────
async function loadCredentialStatus() {
  const r = await fetch('/api/settings/status');
  const d = await r.json();
  const el   = document.getElementById('cred-status');
  const dot  = document.getElementById('cred-status-dot');
  const text = document.getElementById('cred-status-text');
  if (d.all_set) {
    el.className = 'cred-status ok';
    dot.textContent = '●';
    text.textContent = 'Configured';
    // Show placeholders indicating fields are set without revealing values
    const idField = document.getElementById('yt-client-id');
    const secField = document.getElementById('yt-client-secret');
    if (!idField.value)  { idField.placeholder = '(saved in Keychain)'; idField.classList.add('filled'); }
    if (!secField.value) { secField.placeholder = '(saved in Keychain)'; secField.classList.add('filled'); }
  } else {
    el.className = 'cred-status missing';
    text.textContent = d.keychain.yt_client_id ? 'Client Secret missing' : 'Not configured';
  }
}

async function saveCredentials() {
  const id  = document.getElementById('yt-client-id').value.trim();
  const sec = document.getElementById('yt-client-secret').value.trim();
  if (!id && !sec) { toast('Enter at least one value','err'); return; }
  const r = await fetch('/api/settings/save', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({yt_client_id:id, yt_client_secret:sec})});
  const d = await r.json();
  if (d.ok) { toast('Saved to Keychain ✓','ok'); loadCredentialStatus(); }
}

async function clearCredentials() {
  if (!confirm('Remove YouTube credentials from Keychain?')) return;
  await fetch('/api/settings/clear', {method:'POST'});
  document.getElementById('yt-client-id').value = '';
  document.getElementById('yt-client-secret').value = '';
  ['yt-client-id','yt-client-secret'].forEach(id => {
    const el = document.getElementById(id);
    el.placeholder = '';
    el.classList.remove('filled');
  });
  toast('Credentials cleared','ok');
  loadCredentialStatus();
}

function toggleSecret(fieldId, btn) {
  const f = document.getElementById(fieldId);
  f.type = f.type === 'password' ? 'text' : 'password';
  btn.textContent = f.type === 'password' ? '👁' : '🙈';
}

// ── Toast ─────────────────────────────────────────────────────────────────
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast ${type} show`;
  setTimeout(() => el.classList.remove('show'), 4000);
}

// Init
loadEntries();
setInterval(loadEntries, 15000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    for d in [MANIFEST_DIR, DONE_DIR, FRAMES_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    print("\n  HiFi Pipeline UI")
    print("  ─────────────────")
    print("  http://localhost:8080\n")
    app.run(host="127.0.0.1", port=8080, debug=False, threaded=True)
