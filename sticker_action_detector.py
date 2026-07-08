#!/usr/bin/env python3
"""Detect sticker-editing moments for short-form process videos.

The detector follows a simple editing grammar:

1. Keep about one second around the sticker peel/lift moment.
2. Keep about 0.8 seconds after the tweezer leaves the placed sticker.

It uses OpenCV visual motion/edge scores plus optional audio scratch transients,
then writes a segments.csv file that can be cut by FFmpeg.
"""

from __future__ import annotations

import argparse
import csv
import math
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Segment:
    kind: str
    start: float
    end: float
    anchor: float
    score: float
    note: str
    cx: float = 0.5
    cy: float = 0.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-detect sticker peel/release edit segments.")
    parser.add_argument("--input", required=True, help="Input video.")
    parser.add_argument("--output-dir", default="sticker_action_output")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--roi", default="0,0,1,1", help="x,y,w,h. 0..1 values are relative.")
    parser.add_argument("--analysis-fps", type=float, default=12.0)
    parser.add_argument("--max-actions", type=int, default=80)
    parser.add_argument("--min-action-gap", type=float, default=0.35)
    parser.add_argument("--peel-duration", type=float, default=1.0)
    parser.add_argument("--repeat-peel-duration", type=float, default=2.0)
    parser.add_argument("--peel-before", type=float, default=0.35)
    parser.add_argument("--release-duration", type=float, default=0.8)
    parser.add_argument("--release-search-min", type=float, default=0.45)
    parser.add_argument("--release-search-max", type=float, default=3.2)
    parser.add_argument(
        "--same-sticker-gap",
        type=float,
        default=1.75,
        help="Peel peaks within this gap are merged into the final action cluster.",
    )
    parser.add_argument(
        "--same-sticker-retry-gap",
        type=float,
        default=2.0,
        help="A non-extended clip followed within this gap is treated as a non-final retry.",
    )
    parser.add_argument("--same-sticker-distance", type=float, default=0.22)
    parser.add_argument("--final-cluster-max-span", type=float, default=2.2)
    parser.add_argument(
        "--repeat-peel-window",
        type=float,
        default=1.05,
        help="If a sticker is peeled again within this window, keep a longer final clip.",
    )
    parser.add_argument("--motion-threshold", type=float, default=1.4)
    parser.add_argument("--audio-threshold", type=float, default=2.4)
    parser.add_argument("--settle-threshold", type=float, default=0.25)
    parser.add_argument("--disable-audio", action="store_true")
    parser.add_argument("--keep-debug-audio", action="store_true")
    return parser.parse_args()


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        print(proc.stdout.decode("utf-8", errors="replace"))
        raise SystemExit(proc.returncode)


def parse_roi(text: str, width: int, height: int) -> tuple[int, int, int, int]:
    parts = [float(part.strip()) for part in text.split(",")]
    if len(parts) != 4:
        raise SystemExit("--roi must be x,y,w,h")
    x, y, w, h = parts
    if max(parts) <= 1.0:
        x, y, w, h = x * width, y * height, w * width, h * height
    left = max(0, min(width - 1, int(round(x))))
    top = max(0, min(height - 1, int(round(y))))
    right = max(left + 1, min(width, int(round(x + w))))
    bottom = max(top + 1, min(height, int(round(y + h))))
    return left, top, right, bottom


def robust_z(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) or 1e-6
    z = (values - median) / (1.4826 * mad)
    return np.clip(z, -6.0, 6.0)


def smooth(values: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0 or values.size < radius * 2 + 1:
        return values
    kernel = np.ones(radius * 2 + 1, dtype=np.float32)
    kernel /= kernel.sum()
    return np.convolve(values, kernel, mode="same")


def read_visual_scores(input_video: Path, roi_text: str, target_fps: float) -> dict[str, np.ndarray]:
    cap = cv2.VideoCapture(str(input_video))
    if not cap.isOpened():
        raise SystemExit(f"Could not open video: {input_video}")

    source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    stride = max(1, int(round(source_fps / target_fps)))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    left, top, right, bottom = parse_roi(roi_text, width, height)

    prev_gray: np.ndarray | None = None
    prev_edges: np.ndarray | None = None
    times: list[float] = []
    motion_scores: list[float] = []
    edge_scores: list[float] = []
    activity_scores: list[float] = []
    center_xs: list[float] = []
    center_ys: list[float] = []
    index = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % stride:
            index += 1
            continue

        crop = frame[top:bottom, left:right]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (360, 640), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(gray, 45, 135)

        if prev_gray is not None and prev_edges is not None:
            flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, 0.5, 3, 19, 3, 5, 1.2, 0)
            mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
            edge_delta = cv2.absdiff(prev_edges, edges)
            motion = float(np.percentile(mag, 94))
            edge_change = float(np.mean(edge_delta > 0))
            active_area = float(np.mean(mag > max(0.20, np.percentile(mag, 84))))
            mask = mag > max(0.20, np.percentile(mag, 90))
            if np.any(mask):
                ys, xs = np.where(mask)
                center_x = float(np.mean(xs) / max(1, mag.shape[1] - 1))
                center_y = float(np.mean(ys) / max(1, mag.shape[0] - 1))
            else:
                center_x = 0.5
                center_y = 0.5

            times.append(index / source_fps)
            motion_scores.append(motion)
            edge_scores.append(edge_change)
            activity_scores.append(active_area)
            center_xs.append(center_x)
            center_ys.append(center_y)

        prev_gray = gray
        prev_edges = edges
        index += 1

    cap.release()
    motion = smooth(np.asarray(motion_scores, dtype=np.float32), 1)
    edge = smooth(np.asarray(edge_scores, dtype=np.float32), 1)
    activity = smooth(np.asarray(activity_scores, dtype=np.float32), 1)
    combined = robust_z(motion) * 0.65 + robust_z(edge) * 0.25 + robust_z(activity) * 0.10
    return {
        "time": np.asarray(times, dtype=np.float32),
        "motion": motion,
        "edge": edge,
        "activity": activity,
        "center_x": np.asarray(center_xs, dtype=np.float32),
        "center_y": np.asarray(center_ys, dtype=np.float32),
        "combined_z": smooth(combined.astype(np.float32), 1),
    }


def extract_audio(ffmpeg: str, input_video: Path, wav_path: Path, sample_rate: int = 22050) -> bool:
    try:
        run(
            [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(input_video),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-acodec",
                "pcm_s16le",
                str(wav_path),
            ]
        )
        return True
    except SystemExit:
        return False


def read_audio_scores(wav_path: Path, hop_ms: float = 8.0) -> dict[str, np.ndarray]:
    with wave.open(str(wav_path), "rb") as wav:
        sample_rate = wav.getframerate()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32)
    if samples.size < 10:
        empty = np.asarray([], dtype=np.float32)
        return {"time": empty, "z": empty}

    samples /= 32768.0
    detail = np.abs(np.diff(samples, prepend=samples[0]))
    hop = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    frame = max(hop * 3, int(round(sample_rate * 0.035)))

    times: list[float] = []
    values: list[float] = []
    for start in range(0, max(1, detail.size - frame), hop):
        chunk = detail[start : start + frame]
        values.append(math.sqrt(float(np.mean(chunk * chunk))))
        times.append((start + chunk.size / 2) / sample_rate)
    z = smooth(robust_z(np.asarray(values, dtype=np.float32)), 2)
    return {"time": np.asarray(times, dtype=np.float32), "z": z.astype(np.float32)}


def audio_near(audio: dict[str, np.ndarray], start: float, end: float) -> float:
    times = audio["time"]
    if times.size == 0:
        return 0.0
    mask = (times >= start) & (times <= end)
    if not np.any(mask):
        return 0.0
    return float(np.max(audio["z"][mask]))


def local_min_after(times: np.ndarray, score_z: np.ndarray, peak_t: float, args: argparse.Namespace) -> tuple[float, float]:
    start = peak_t + args.release_search_min
    end = peak_t + args.release_search_max
    mask = (times >= start) & (times <= end)
    if not np.any(mask):
        return peak_t + args.release_search_min, 0.0

    idxs = np.where(mask)[0]
    local_scores = score_z[idxs]
    quiet = np.where(local_scores <= args.settle_threshold)[0]
    if quiet.size:
        idx = idxs[int(quiet[0])]
    else:
        idx = idxs[int(np.argmin(local_scores))]
    return float(times[idx]), float(score_z[idx])


def detect_segments(visual: dict[str, np.ndarray], audio: dict[str, np.ndarray], args: argparse.Namespace) -> list[Segment]:
    times = visual["time"]
    score_z = visual["combined_z"]
    center_x = visual["center_x"]
    center_y = visual["center_y"]
    candidates: list[tuple[float, float, str, float, float]] = []

    active = score_z >= args.motion_threshold
    ranges: list[tuple[int, int]] = []
    start_idx: int | None = None
    for i, is_active in enumerate(active):
        if is_active and start_idx is None:
            start_idx = i
        elif not is_active and start_idx is not None:
            ranges.append((start_idx, max(start_idx, i - 1)))
            start_idx = None
    if start_idx is not None:
        ranges.append((start_idx, score_z.size - 1))

    for start_idx, end_idx in ranges:
        peak_offset = int(np.argmax(score_z[start_idx : end_idx + 1]))
        i = start_idx + peak_offset
        t = float(times[i])
        audio_score = audio_near(audio, t - 0.18, t + 0.35)
        score = float(score_z[i] + max(0.0, audio_score - args.audio_threshold) * 0.45)
        note = "motion_peak"
        if audio_score >= args.audio_threshold:
            note = "motion_peak+scratch_audio"
        candidates.append((t, score, note, float(center_x[i]), float(center_y[i])))

    candidates.sort(key=lambda item: item[1], reverse=True)
    selected: list[tuple[float, float, str]] = []
    for candidate in candidates:
        if all(abs(candidate[0] - existing[0]) >= args.min_action_gap for existing in selected):
            selected.append(candidate)
        if len(selected) >= args.max_actions:
            break
    selected.sort(key=lambda item: item[0])

    groups = group_same_sticker_actions(selected, args.same_sticker_gap)
    segments: list[Segment] = []
    for group in groups:
        final_cluster = final_action_cluster(group, args.same_sticker_gap, args.final_cluster_max_span)
        repeated = has_quick_repeel(final_cluster, args.repeat_peel_window)
        peel_duration = args.repeat_peel_duration if repeated else args.peel_duration
        first_peak_t = final_cluster[0][0]
        peak_t, score, note, cx, cy = max(final_cluster, key=lambda item: item[1])
        last_peak_t = final_cluster[-1][0]
        peel_start = max(0.0, first_peak_t - args.peel_before)
        release_t, quiet_score = local_min_after(times, score_z, last_peak_t, args)
        release_start = max(0.0, release_t - 0.12)
        release_end = release_start + args.release_duration
        kind = "last_piece_extended" if repeated else "last_piece"
        group_note = note
        if repeated:
            group_note += f";quick_repeel_kept_last_only;duration={peel_duration:.1f}s"
        if len(final_cluster) > 1:
            group_note += f";merged_final_cluster={len(final_cluster)}"
        segments.append(
            Segment(
                kind,
                peel_start,
                max(peel_start + peel_duration, release_end),
                release_t,
                score - quiet_score * 0.2,
                group_note,
                cx,
                cy,
            )
        )

    return drop_non_final_retries(
        keep_last_overlapping_segments(sorted(segments, key=lambda s: s.start)),
        args.same_sticker_retry_gap,
        args.same_sticker_distance,
    )


def group_same_sticker_actions(
    actions: list[tuple[float, float, str, float, float]],
    same_sticker_gap: float,
) -> list[list[tuple[float, float, str, float, float]]]:
    groups: list[list[tuple[float, float, str, float, float]]] = []
    for action in actions:
        if not groups or action[0] - groups[-1][-1][0] > same_sticker_gap:
            groups.append([action])
        else:
            groups[-1].append(action)
    return groups


def final_action_cluster(
    actions: list[tuple[float, float, str, float, float]],
    cluster_gap: float,
    max_span: float,
) -> list[tuple[float, float, str, float, float]]:
    cluster = [actions[-1]]
    for action in reversed(actions[:-1]):
        if cluster[0][0] - action[0] > cluster_gap:
            break
        if cluster[-1][0] - action[0] > max_span:
            break
        cluster.insert(0, action)
    return cluster


def has_quick_repeel(
    actions: list[tuple[float, float, str, float, float]],
    repeat_window: float,
) -> bool:
    return any(
        actions[index + 1][0] - actions[index][0] <= repeat_window
        for index in range(len(actions) - 1)
    )


def keep_last_overlapping_segments(segments: list[Segment]) -> list[Segment]:
    kept: list[Segment] = []
    for segment in segments:
        if not kept or segment.start > kept[-1].end + 0.08:
            kept.append(segment)
            continue
        kept[-1] = Segment(
            kind=segment.kind,
            start=segment.start,
            end=segment.end,
            anchor=segment.anchor,
            score=segment.score,
            note=segment.note + ";overlap_kept_last_only",
            cx=segment.cx,
            cy=segment.cy,
        )
    return kept


def segment_distance(a: Segment, b: Segment) -> float:
    return math.hypot(a.cx - b.cx, a.cy - b.cy)


def drop_non_final_retries(segments: list[Segment], retry_gap: float, max_distance: float) -> list[Segment]:
    kept: list[Segment] = []
    for index, segment in enumerate(segments):
        next_segment = segments[index + 1] if index + 1 < len(segments) else None
        close_next_gap = next_segment.start - segment.end if next_segment is not None else float("inf")
        if (
            next_segment is not None
            and "extended" not in segment.kind
            and segment_distance(segment, next_segment) <= max_distance
            and (
                close_next_gap <= 0.5
                or ("merged_final_cluster" not in segment.note and close_next_gap <= retry_gap)
            )
        ):
            continue
        kept.append(segment)
    return kept


def fmt_time(seconds: float) -> str:
    ms_total = int(round(seconds * 1000))
    ms = ms_total % 1000
    total = ms_total // 1000
    sec = total % 60
    minute = (total // 60) % 60
    hour = total // 3600
    return f"{hour:02d}:{minute:02d}.{sec:02d}.{ms:03d}".replace(".", ":", 1)


def write_outputs(out_dir: Path, visual: dict[str, np.ndarray], audio: dict[str, np.ndarray], segments: list[Segment]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "segments.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["index", "kind", "start", "end", "duration", "anchor", "score", "note"])
        for index, segment in enumerate(segments, start=1):
            writer.writerow(
                [
                    index,
                    segment.kind,
                    fmt_time(segment.start),
                    fmt_time(segment.end),
                    f"{segment.end - segment.start:.3f}",
                    fmt_time(segment.anchor),
                    f"{segment.score:.3f}",
                    segment.note,
                ]
            )

    with (out_dir / "scores.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "motion", "edge", "activity", "center_x", "center_y", "combined_z", "audio_z"])
        for t, motion, edge, activity, cx, cy, combined in zip(
            visual["time"],
            visual["motion"],
            visual["edge"],
            visual["activity"],
            visual["center_x"],
            visual["center_y"],
            visual["combined_z"],
        ):
            writer.writerow(
                [
                    f"{float(t):.3f}",
                    f"{float(motion):.6f}",
                    f"{float(edge):.6f}",
                    f"{float(activity):.6f}",
                    f"{float(cx):.3f}",
                    f"{float(cy):.3f}",
                    f"{float(combined):.3f}",
                    f"{audio_near(audio, float(t) - 0.01, float(t) + 0.01):.3f}",
                ]
            )


def main() -> int:
    args = parse_args()
    input_video = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    visual = read_visual_scores(input_video, args.roi, args.analysis_fps)
    audio = {"time": np.asarray([], dtype=np.float32), "z": np.asarray([], dtype=np.float32)}
    wav_path = out_dir / "_analysis_audio.wav"
    if not args.disable_audio and extract_audio(args.ffmpeg, input_video, wav_path):
        audio = read_audio_scores(wav_path)
        if not args.keep_debug_audio:
            wav_path.unlink(missing_ok=True)

    segments = detect_segments(visual, audio, args)
    write_outputs(out_dir, visual, audio, segments)
    print(f"Detected {len(segments)} edit segments.")
    print(f"Segments: {out_dir / 'segments.csv'}")
    print(f"Scores: {out_dir / 'scores.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
