import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ingest import Clip


SAMPLE_INTERVAL = 1.5  # seconds between scored frames


@dataclass
class ScoredWindow:
    start: float
    end: float
    score: float
    dominant_signal: str  # "motion" | "warmth" | "sharpness"
    hook_frame_ts: float  # timestamp of single best frame (for re-ordering hook)


def _extract_frames_opencv(video_path: Path, interval: float) -> list[tuple[float, np.ndarray]]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps

    # Seek directly to each sample timestamp — avoids decoding every frame
    timestamps = []
    t = 0.0
    while t < duration:
        timestamps.append(t)
        t += interval

    frames = []
    for ts in timestamps:
        cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000)
        ret, frame = cap.read()
        if ret:
            frames.append((ts, frame))

    cap.release()
    return frames


def _motion_score(prev: np.ndarray, curr: np.ndarray) -> float:
    """Frame differencing — measures movement (woofer, drivers)."""
    if prev is None:
        return 0.0
    prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    curr_gray = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(prev_gray, curr_gray)
    # Focus on center-bottom third — where woofers typically sit in 9:16
    h, w = diff.shape
    roi = diff[h // 2:, w // 4: 3 * w // 4]
    return float(np.mean(roi)) / 255.0


def _warmth_score(frame: np.ndarray) -> float:
    """Detect warm tones — tube glow, wood cabinets, warm show lighting."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    # Broader warm range: amber, orange, warm-red, golden-yellow
    lower = np.array([5, 60, 80])
    upper = np.array([35, 255, 255])
    mask = cv2.inRange(hsv, lower, upper)
    warm_ratio = np.sum(mask > 0) / mask.size
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    blob_bonus = min(len(contours) * 0.01, 0.15)
    # Calibrated for 4K: raw ratios are ~0.01-0.03, scale up to 0-1 range
    return min(warm_ratio * 15.0 + blob_bonus, 1.0)


def _sharpness_score(frame: np.ndarray) -> float:
    """Laplacian variance — high = lots of fine detail in focus (driver texture, grille)."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    roi = gray[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
    lap_var = cv2.Laplacian(roi, cv2.CV_64F).var()
    # Calibrated for 4K MOV: typical indoor range 3–15, scale to 0–1
    return min(lap_var / 12.0, 1.0)


def score_clip(clip: Clip, weights: dict) -> list[tuple[float, float, str]]:
    """Returns list of (timestamp, composite_score, dominant_signal) per sampled frame."""
    frames = _extract_frames_opencv(clip.path, SAMPLE_INTERVAL)
    if not frames:
        return []

    scored = []
    prev_frame = None

    for ts, frame in frames:
        m = _motion_score(prev_frame, frame)
        w = _warmth_score(frame)
        s = _sharpness_score(frame)

        composite = (
            m * weights["motion"] +
            w * weights["warmth"] +
            s * weights["sharpness"]
        )

        signals = {"motion": m, "warmth": w, "sharpness": s}
        dominant = max(signals, key=signals.get)

        scored.append((ts, composite, dominant))
        prev_frame = frame

    return scored


def _sliding_window_score(scored: list[tuple], start_ts: float, end_ts: float) -> float:
    window = [s for ts, s, _ in scored if start_ts <= ts <= end_ts]
    return float(np.mean(window)) if window else 0.0


def find_best_windows(
    scored: list[tuple],
    min_s: int,
    max_s: int,
    max_windows: int,
    clip_duration: float,
) -> list[ScoredWindow]:
    if not scored:
        return []

    best_windows = []
    used_ranges = []
    global_best = None  # guaranteed fallback — best single window regardless of threshold

    # Try window lengths from max to min — prefer longer if score holds
    for target_len in range(max_s, min_s - 1, -1):
        step = SAMPLE_INTERVAL
        t = 0.0
        candidates = []

        while t + target_len <= clip_duration:
            window_end = t + target_len
            score = _sliding_window_score(scored, t, window_end)
            # Find dominant signal and best frame in this window
            window_frames = [(ts, s, d) for ts, s, d in scored if t <= ts <= window_end]
            if window_frames:
                best_ts, _, dominant = max(window_frames, key=lambda x: x[1])
                candidates.append((t, window_end, score, dominant, best_ts))
            t += step

        if not candidates:
            continue

        candidates.sort(key=lambda x: x[2], reverse=True)
        top = candidates[0]
        start, end, score, dominant, hook_ts = top

        # Track absolute best for fallback
        if global_best is None or score > global_best[2]:
            global_best = (start, end, score, dominant, hook_ts)

        # Check overlap with already-selected windows
        overlaps = any(
            not (end <= u_start or start >= u_end)
            for u_start, u_end in used_ranges
        )
        if overlaps:
            continue

        if score < 0.04:
            continue

        best_windows.append(ScoredWindow(
            start=start,
            end=end,
            score=score,
            dominant_signal=dominant,
            hook_frame_ts=hook_ts,
        ))
        used_ranges.append((start, end))

        if len(best_windows) >= max_windows:
            break

    # Always return at least one window — use best found even if below threshold
    if not best_windows and global_best:
        start, end, score, dominant, hook_ts = global_best
        best_windows.append(ScoredWindow(
            start=start, end=end, score=score,
            dominant_signal=dominant, hook_frame_ts=hook_ts,
        ))

    return sorted(best_windows, key=lambda w: w.score, reverse=True)
