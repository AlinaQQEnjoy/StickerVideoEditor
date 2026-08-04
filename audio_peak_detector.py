#!/usr/bin/env python3
"""Detect short sticker actions from audio peaks in a video."""

from __future__ import annotations

import argparse
import csv
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np


INT16_MAX = 32768.0


@dataclass
class AudioEvent:
    index: int
    start: float
    end: float
    anchor: float
    peak_dbfs: float
    event_start: float
    event_end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def run_ffmpeg_audio(input_video: Path, ffmpeg: Path, sample_rate: int) -> np.ndarray:
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"FFmpeg audio extraction failed: {message}")
    if not proc.stdout:
        raise RuntimeError("No audio samples were extracted from the video.")
    return np.frombuffer(proc.stdout, dtype=np.int16)


def seconds_to_time(value: float) -> str:
    value = max(0.0, value)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = int(value % 60)
    millis = int(round((value - math.floor(value)) * 1000))
    if millis == 1000:
        seconds += 1
        millis = 0
    if seconds == 60:
        minutes += 1
        seconds = 0
    if minutes == 60:
        hours += 1
        minutes = 0
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def peak_dbfs(abs_peak: float) -> float:
    if abs_peak <= 0:
        return -120.0
    return 20.0 * math.log10(abs_peak / INT16_MAX)


def detect_events(
    samples: np.ndarray,
    sample_rate: int,
    threshold_dbfs: float,
    window_ms: float,
    min_gap: float,
    pre_roll: float,
    post_roll: float,
) -> list[AudioEvent]:
    window_size = max(1, int(round(sample_rate * window_ms / 1000.0)))
    total_samples = int(samples.size)
    if total_samples == 0:
        return []

    pad = (-total_samples) % window_size
    padded = np.pad(samples, (0, pad), mode="constant") if pad else samples
    frames = padded.reshape((-1, window_size)).astype(np.int32)
    window_peaks = np.max(np.abs(frames), axis=1)
    dbfs = np.array([peak_dbfs(float(x)) for x in window_peaks])
    active = dbfs >= threshold_dbfs

    raw_ranges: list[tuple[int, int]] = []
    start: int | None = None
    for idx, is_active in enumerate(active):
        if is_active and start is None:
            start = idx
        elif not is_active and start is not None:
            raw_ranges.append((start, idx - 1))
            start = None
    if start is not None:
        raw_ranges.append((start, len(active) - 1))

    merged: list[tuple[int, int]] = []
    gap_windows = max(0, int(round(min_gap * sample_rate / window_size)))
    for event_start, event_end in raw_ranges:
        if merged and event_start - merged[-1][1] <= gap_windows:
            merged[-1] = (merged[-1][0], event_end)
        else:
            merged.append((event_start, event_end))

    events: list[AudioEvent] = []
    audio_duration = total_samples / sample_rate
    for win_start, win_end in merged:
        sample_start = max(0, win_start * window_size)
        sample_end = min(total_samples, (win_end + 1) * window_size)
        if sample_end <= sample_start:
            continue

        event_samples = samples[sample_start:sample_end].astype(np.int32)
        local_peak = int(np.argmax(np.abs(event_samples)))
        peak_sample_index = sample_start + local_peak
        anchor = peak_sample_index / sample_rate
        peak = float(abs(int(samples[peak_sample_index])))
        segment_start = max(0.0, anchor - pre_roll)
        segment_end = min(audio_duration, anchor + post_roll)
        if segment_end <= segment_start:
            continue

        events.append(
            AudioEvent(
                index=len(events) + 1,
                start=segment_start,
                end=segment_end,
                anchor=anchor,
                peak_dbfs=peak_dbfs(peak),
                event_start=sample_start / sample_rate,
                event_end=sample_end / sample_rate,
            )
        )
    return events


def write_csvs(events: list[AudioEvent], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "segments.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["index", "kind", "start", "end", "duration", "anchor", "score", "note"],
        )
        writer.writeheader()
        for event in events:
            writer.writerow(
                {
                    "index": event.index,
                    "kind": "audio_peak",
                    "start": seconds_to_time(event.start),
                    "end": seconds_to_time(event.end),
                    "duration": f"{event.duration:.3f}",
                    "anchor": seconds_to_time(event.anchor),
                    "score": f"{event.peak_dbfs:.1f} dBFS",
                    "note": "audio peak > threshold",
                }
            )

    with (output_dir / "audio_peaks.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["index", "anchor", "peak_dbfs", "event_start", "event_end", "segment_start", "segment_end"],
        )
        writer.writeheader()
        for event in events:
            writer.writerow(
                {
                    "index": event.index,
                    "anchor": f"{event.anchor:.3f}",
                    "peak_dbfs": f"{event.peak_dbfs:.3f}",
                    "event_start": f"{event.event_start:.3f}",
                    "event_end": f"{event.event_end:.3f}",
                    "segment_start": f"{event.start:.3f}",
                    "segment_end": f"{event.end:.3f}",
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect sticker action clips from audio peaks.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--threshold-dbfs", type=float, default=-20.0)
    parser.add_argument("--pre-roll", type=float, default=0.4)
    parser.add_argument("--post-roll", type=float, default=0.4)
    parser.add_argument("--min-gap", type=float, default=0.3)
    parser.add_argument("--window-ms", type=float, default=10.0)
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--max-peaks", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.exists():
        raise SystemExit(f"Input video not found: {args.input}")
    if not args.ffmpeg.exists():
        raise SystemExit(f"FFmpeg not found: {args.ffmpeg}")

    samples = run_ffmpeg_audio(args.input, args.ffmpeg, args.sample_rate)
    events = detect_events(
        samples=samples,
        sample_rate=args.sample_rate,
        threshold_dbfs=args.threshold_dbfs,
        window_ms=args.window_ms,
        min_gap=args.min_gap,
        pre_roll=args.pre_roll,
        post_roll=args.post_roll,
    )
    if args.max_peaks > 0:
        events = events[: args.max_peaks]
        for idx, event in enumerate(events, start=1):
            event.index = idx
    write_csvs(events, args.output_dir)
    print(f"Detected {len(events)} audio peak segments.")
    print(f"Segments: {args.output_dir / 'segments.csv'}")
    print(f"Peaks: {args.output_dir / 'audio_peaks.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
