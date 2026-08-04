#!/usr/bin/env python3
"""Local web UI for reviewing detected sticker action clips."""

from __future__ import annotations

import csv
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DEFAULT_FFMPEG = Path(r"D:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe")
DEFAULT_PYTHON = Path(r"C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
FINAL_OUTPUT_ROOT = Path(r"D:\MellowScape") / "\u526a\u8f91\u540e\u89c6\u9891"
WORK_DIR = APP_DIR / "review_output"
PROCESS_ROOT = APP_DIR / "review_processes"
STATIC_DIR = APP_DIR / "web"
STATE_LOCK = threading.Lock()
TASKS_LOCK = threading.Lock()
TASKS: dict[str, dict[str, object]] = {}
STATE: dict[str, object] = {
    "busy": False,
    "message": "Ready.",
    "inputVideo": "",
    "outputDir": str(WORK_DIR),
    "segments": [],
    "finalVideo": "",
    "error": "",
    "progressPercent": 0,
    "progressText": "",
    "canCancel": False,
    "activeProcessId": "",
    "processes": [],
}


class CancelledError(RuntimeError):
    pass


@dataclass
class SegmentItem:
    index: int
    kind: str
    start: str
    end: str
    duration: str
    anchor: str
    score: str
    note: str
    clip: str
    thumb: str
    selected: bool = True


def set_state(**updates: object) -> None:
    with STATE_LOCK:
        STATE.update(updates)
        active_id = str(STATE.get("activeProcessId") or "")
        for process in STATE.get("processes", []):
            if process.get("id") == active_id:
                for key in ("inputVideo", "outputDir", "finalVideo", "message", "progressPercent", "progressText", "canCancel"):
                    if key in updates:
                        process[key] = updates[key]
                if "segments" in updates:
                    process["segmentCount"] = len(updates["segments"]) if isinstance(updates["segments"], list) else 0
                break


def set_process_state(process_id: str, **updates: object) -> None:
    with STATE_LOCK:
        active_id = str(STATE.get("activeProcessId") or "")
        for process in STATE.get("processes", []):
            if process.get("id") == process_id:
                process.update(updates)
                if "segments" in updates:
                    process["segmentCount"] = len(updates["segments"]) if isinstance(updates["segments"], list) else 0
                break
        if active_id == process_id:
            STATE.update(updates)
            if "segments" in updates and isinstance(updates["segments"], list):
                STATE["segments"] = updates["segments"]


def get_state() -> dict[str, object]:
    with STATE_LOCK:
        return json.loads(json.dumps(STATE))


def process_label(index: int) -> str:
    return f"Process {index}"


def create_process_record(name: str | None = None) -> dict[str, object]:
    PROCESS_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    existing = STATE.get("processes", [])
    process_id = f"process_{stamp}_{len(existing) + 1:02d}"
    out_dir = PROCESS_ROOT / process_id
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "id": process_id,
        "name": name or process_label(len(existing) + 1),
        "outputDir": str(out_dir),
        "inputVideo": "",
        "finalVideo": "",
        "message": "Ready.",
        "progressPercent": 0,
        "progressText": "",
        "canCancel": False,
        "busy": False,
        "segmentCount": 0,
    }


def ensure_processes() -> None:
    with STATE_LOCK:
        if STATE.get("processes"):
            return
        existing_dirs = sorted(path for path in PROCESS_ROOT.glob("process_*") if path.is_dir())
        if existing_dirs:
            processes: list[dict[str, object]] = []
            for index, out_dir in enumerate(existing_dirs, start=1):
                segment_count = len(read_segments(out_dir))
                processes.append(
                    {
                        "id": out_dir.name,
                        "name": process_label(index),
                        "outputDir": str(out_dir),
                        "inputVideo": "",
                        "finalVideo": "",
                        "message": "Ready.",
                        "progressPercent": 100 if segment_count else 0,
                        "progressText": "Loaded output" if segment_count else "",
                        "canCancel": False,
                        "busy": False,
                        "segmentCount": segment_count,
                    }
                )
            STATE["processes"] = processes
            STATE["activeProcessId"] = processes[0]["id"]
            STATE["outputDir"] = processes[0]["outputDir"]
            STATE["segments"] = read_segments(Path(str(processes[0]["outputDir"])))
            return
        process = create_process_record("Process 1")
        STATE["processes"] = [process]
        STATE["activeProcessId"] = process["id"]
        STATE["outputDir"] = process["outputDir"]


def active_process() -> dict[str, object]:
    ensure_processes()
    with STATE_LOCK:
        active_id = str(STATE.get("activeProcessId") or "")
        for process in STATE.get("processes", []):
            if process.get("id") == active_id:
                return json.loads(json.dumps(process))
        process = STATE["processes"][0]
        STATE["activeProcessId"] = process["id"]
        return json.loads(json.dumps(process))


def activate_process(process_id: str) -> dict[str, object]:
    ensure_processes()
    with STATE_LOCK:
        processes = STATE.get("processes", [])
        target = next((item for item in processes if item.get("id") == process_id), None)
        if target is None:
            raise RuntimeError(f"Process not found: {process_id}")
        out_dir = Path(str(target["outputDir"]))
        segments = read_segments(out_dir)
        STATE.update(
            activeProcessId=process_id,
            busy=target.get("busy", False),
            inputVideo=target.get("inputVideo", ""),
            outputDir=str(out_dir),
            finalVideo=target.get("finalVideo", ""),
            message=target.get("message", "Ready."),
            segments=segments,
            progressPercent=target.get("progressPercent", 0),
            progressText=target.get("progressText", ""),
            canCancel=False,
            error="",
        )
        STATE["canCancel"] = target.get("canCancel", False)
        target["segmentCount"] = len(segments)
    return get_state()


def add_process() -> dict[str, object]:
    ensure_processes()
    with STATE_LOCK:
        process = create_process_record()
        STATE["processes"].append(process)
    return activate_process(str(process["id"]))


def run(cmd: list[str], process_id: str) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    with TASKS_LOCK:
        task = TASKS.setdefault(process_id, {"cancel": threading.Event(), "process": None, "outputDir": None})
        task["process"] = proc
    output_parts: list[str] = []
    try:
        while proc.poll() is None:
            with TASKS_LOCK:
                cancel_event = TASKS.get(process_id, {}).get("cancel")
            if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                raise CancelledError("Task was cancelled.")
            time.sleep(0.2)
        if proc.stdout is not None:
            output_parts.append(proc.stdout.read())
    finally:
        with TASKS_LOCK:
            if TASKS.get(process_id, {}).get("process") is proc:
                TASKS[process_id]["process"] = None
        if proc.stdout is not None:
            proc.stdout.close()
    if proc.returncode != 0:
        raise RuntimeError("".join(output_parts))


def ffprobe_path(ffmpeg: Path) -> Path:
    candidate = ffmpeg.with_name("ffprobe.exe")
    if candidate.exists():
        return candidate
    return Path("ffprobe")


def source_video_size(input_video: Path, ffmpeg: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            str(ffprobe_path(ffmpeg)),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(input_video),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not read source video dimensions.")
    size_text = result.stdout.strip().splitlines()[0]
    width_text, height_text = size_text.split("x", 1)
    return int(width_text), int(height_text)


def four_k_scale_args(input_video: Path, ffmpeg: Path) -> list[str]:
    width, height = source_video_size(input_video, ffmpeg)
    target_width, target_height = (2160, 3840) if height >= width else (3840, 2160)
    if width >= target_width and height >= target_height:
        return []
    return ["-vf", f"scale={target_width}:{target_height}:flags=lanczos"]


def cleanup_task_output(path: Path | None) -> None:
    if path is None:
        return
    target = path.resolve()
    app_root = APP_DIR.resolve()
    if not str(target).lower().startswith(str(app_root).lower()):
        return
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def request_cancel(process_id: str | None = None) -> dict[str, object]:
    target_id = process_id or str(active_process()["id"])
    with TASKS_LOCK:
        task = TASKS.get(target_id)
    if not task:
        return {"ok": True, "cancelled": False, "message": "No running task."}
    cancel_event = task.get("cancel")
    if isinstance(cancel_event, threading.Event):
        cancel_event.set()
    proc = task.get("process")
    if isinstance(proc, subprocess.Popen):
        if proc is not None and proc.poll() is None:
            proc.terminate()
    set_process_state(target_id, message="Cancelling current task...", progressText="Cancelling", canCancel=False)
    return {"ok": True}


def parse_time_to_seconds(text: str) -> float:
    parts = text.replace(".", ":").split(":")
    if len(parts) < 4:
        return float(text)
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]) + int(parts[3]) / 1000.0


def parse_score_to_dbfs(text: str) -> float:
    import re

    match = re.search(r"[-+]?\d+(?:\.\d+)?", text or "")
    return float(match.group(0)) if match else 0.0


def safe_filename_part(text: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in text.strip())
    return cleaned.strip("_") or "video"


def default_final_video(process_id: str, input_video: str = "") -> Path:
    date_folder = time.strftime("%Y-%m-%d")
    source_name = safe_filename_part(Path(input_video).stem if input_video else "sticker_action")
    process_name = safe_filename_part(process_id)
    stamp = time.strftime("%H%M%S")
    return FINAL_OUTPUT_ROOT / date_folder / f"{source_name}_{process_name}_{stamp}.mp4"


def pick_video_file() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        initial_dir = Path(r"D:\MellowScape") / "\u539f\u89c6\u9891"
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="\u9009\u62e9\u539f\u89c6\u9891",
            initialdir=str(initial_dir) if initial_dir.exists() else str(Path.home()),
            filetypes=[
                ("\u89c6\u9891\u6587\u4ef6", "*.mov *.MOV *.mp4 *.MP4 *.m4v *.M4V"),
                ("\u6240\u6709\u6587\u4ef6", "*.*"),
            ],
        )
        root.destroy()
        return path or ""
    except Exception as exc:
        raise RuntimeError(f"Could not open file picker: {exc}") from exc


def rel_url(path: Path) -> str:
    rel = path.resolve().relative_to(APP_DIR)
    return "/file/" + urllib.parse.quote(rel.as_posix())


def send_cache_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("Pragma", "no-cache")
    origin = handler.headers.get("Origin", "")
    allowed_origins = {
        "https://alinaqqenjoy.github.io",
        "http://127.0.0.1:8787",
        "http://localhost:8787",
    }
    if origin in allowed_origins:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def find_segment_clip(out_dir: Path, index: int) -> Path:
    segment_clip = out_dir / "clips" / f"segment_{index:03d}.mp4"
    if segment_clip.exists():
        return segment_clip
    matches = sorted((out_dir / "clips").glob(f"clip_{index:03d}_*.mp4"))
    if matches:
        return matches[0]
    return segment_clip


def find_segment_thumb(out_dir: Path, index: int) -> Path | None:
    thumb = out_dir / "thumbs" / f"segment_{index:03d}.jpg"
    if thumb.exists():
        return thumb
    return None


def read_segments(out_dir: Path) -> list[dict[str, object]]:
    path = out_dir / "segments.csv"
    if not path.exists():
        return []
    items: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            index = int(row["index"])
            clip = find_segment_clip(out_dir, index)
            thumb = find_segment_thumb(out_dir, index)
            items.append(
                asdict(
                    SegmentItem(
                        index=index,
                        kind=row.get("kind", ""),
                        start=row.get("start", ""),
                        end=row.get("end", ""),
                        duration=row.get("duration", ""),
                        anchor=row.get("anchor", ""),
                        score=row.get("score", ""),
                        note=row.get("note", ""),
                        clip=rel_url(clip),
                        thumb=rel_url(thumb) if thumb is not None else "",
                    )
                )
            )
    return items


def process_cancelled(process_id: str) -> bool:
    with TASKS_LOCK:
        cancel_event = TASKS.get(process_id, {}).get("cancel")
    return isinstance(cancel_event, threading.Event) and cancel_event.is_set()


def build_previews(input_video: Path, out_dir: Path, ffmpeg: Path, process_id: str) -> None:
    clips_dir = out_dir / "clips"
    thumbs_dir = out_dir / "thumbs"
    clips_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    for old in list(clips_dir.glob("segment_*.mp4")) + list(thumbs_dir.glob("segment_*.jpg")):
        old.unlink(missing_ok=True)

    with (out_dir / "segments.csv").open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    total = max(1, len(rows))
    scale_args = four_k_scale_args(input_video, ffmpeg)
    for row_number, row in enumerate(rows, start=1):
        if process_cancelled(process_id):
            raise CancelledError("Task was cancelled.")
        index = int(row["index"])
        start = parse_time_to_seconds(row["start"])
        duration = float(row["duration"])
        peak_dbfs = parse_score_to_dbfs(row.get("score", ""))
        clip = clips_dir / f"segment_{index:03d}.mp4"
        thumb = thumbs_dir / f"segment_{index:03d}.jpg"
        audio_args = ["-c:a", "aac", "-b:a", "160k"]
        if peak_dbfs < -4.0:
            audio_args = ["-af", "volume=20dB", "-c:a", "aac", "-b:a", "160k"]
        run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(input_video),
                "-t",
                f"{duration:.3f}",
                *scale_args,
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p5",
                "-cq:v",
                "16",
                "-b:v",
                "0",
                "-pix_fmt",
                "yuv420p",
                *audio_args,
                "-movflags",
                "+faststart",
                str(clip),
            ],
            process_id,
        )
        run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start + min(duration * 0.45, 0.4):.3f}",
                "-i",
                str(input_video),
                "-frames:v",
                "1",
                "-vf",
                "scale=360:-2",
                "-q:v",
                "3",
                str(thumb),
            ],
            process_id,
        )
        percent = 20 + round((row_number / total) * 75)
        set_process_state(
            process_id,
            progressPercent=min(95, percent),
            progressText=f"Building preview clips {row_number}/{total}",
            message=f"Building preview clips {row_number}/{total}...",
        )


def analyze_worker(payload: dict[str, object]) -> None:
    try:
        process_id = str(payload.get("processId") or active_process()["id"])
        process = active_process() if process_id == str(active_process()["id"]) else next(
            item for item in get_state().get("processes", []) if item.get("id") == process_id
        )
        with TASKS_LOCK:
            TASKS[process_id] = {"cancel": threading.Event(), "process": None, "outputDir": process["outputDir"]}
        input_video = Path(str(payload.get("inputVideo", "")).strip().strip('"'))
        if not input_video.exists():
            raise RuntimeError(f"Input video not found: {input_video}")
        ffmpeg = Path(str(payload.get("ffmpeg") or DEFAULT_FFMPEG))
        python = Path(str(payload.get("python") or DEFAULT_PYTHON))
        if not ffmpeg.exists():
            raise RuntimeError(f"FFmpeg not found: {ffmpeg}")
        if not python.exists():
            raise RuntimeError(f"Python not found: {python}")

        out_dir = Path(str(process["outputDir"]))
        out_dir.mkdir(parents=True, exist_ok=True)
        set_process_state(
            process_id,
            busy=True,
            message="Detecting audio peaks...",
            inputVideo=str(input_video),
            outputDir=str(out_dir),
            error="",
            progressPercent=5,
            progressText="Detecting audio peaks",
            canCancel=True,
        )

        detector = APP_DIR / "audio_peak_detector.py"
        cmd = [
            str(python),
            str(detector),
            "--input",
            str(input_video),
            "--output-dir",
            str(out_dir),
            "--ffmpeg",
            str(ffmpeg),
            "--threshold-dbfs",
            str(payload.get("thresholdDbfs") or -20),
            "--pre-roll",
            str(payload.get("preRoll") or 0.4),
            "--post-roll",
            str(payload.get("postRoll") or 0.4),
            "--min-gap",
            str(payload.get("minGap") or 0.3),
            "--window-ms",
            str(payload.get("windowMs") or 10),
            "--max-peaks",
            str(payload.get("maxPeaks") or 300),
        ]
        run(cmd, process_id)

        set_process_state(process_id, message="Building preview clips...", progressPercent=20, progressText="Building preview clips")
        build_previews(input_video, out_dir, ffmpeg, process_id)
        segments = read_segments(out_dir)
        set_process_state(
            process_id,
            busy=False,
            message=f"Detected {len(segments)} preview segments.",
            segments=segments,
            outputDir=str(out_dir),
            progressPercent=100,
            progressText="Analysis complete",
            canCancel=False,
        )
        with TASKS_LOCK:
            TASKS.pop(process_id, None)
    except CancelledError:
        cleanup_task_output(Path(str(process.get("outputDir"))))
        with TASKS_LOCK:
            TASKS.pop(process_id, None)
        set_process_state(
            process_id,
            busy=False,
            message="Recognition stopped. Temporary clips were deleted.",
            segments=[],
            error="",
            progressPercent=0,
            progressText="Stopped",
            canCancel=False,
        )
    except Exception as exc:
        with TASKS_LOCK:
            TASKS.pop(process_id, None)
        set_process_state(process_id, busy=False, message="Analyze failed.", error=str(exc), progressPercent=0, progressText="Analyze failed", canCancel=False)


def load_output(payload: dict[str, object]) -> dict[str, object]:
    ensure_processes()
    out_dir = Path(str(payload.get("outputDir", "")).strip().strip('"'))
    if not out_dir.exists():
        raise RuntimeError(f"Output folder not found: {out_dir}")
    segments_path = out_dir / "segments.csv"
    if not segments_path.exists():
        raise RuntimeError(f"Segments file not found: {segments_path}")

    input_video = str(payload.get("inputVideo") or "")
    final_video = str(payload.get("finalVideo") or "")
    segments = read_segments(out_dir)
    active_id = active_process()["id"]
    with STATE_LOCK:
        for process in STATE.get("processes", []):
            if process.get("id") == active_id:
                process.update(
                    inputVideo=input_video,
                    outputDir=str(out_dir),
                    finalVideo=final_video,
                    segmentCount=len(segments),
                    message=f"Loaded {len(segments)} clips from PowerShell run.",
                    progressPercent=100 if segments else 0,
                    progressText="Loaded output" if segments else "",
                )
                break
    set_state(
        busy=False,
        message=f"Loaded {len(segments)} clips from PowerShell run.",
        inputVideo=input_video,
        outputDir=str(out_dir),
        segments=segments,
        finalVideo=final_video,
        error="",
        progressPercent=100 if segments else 0,
        progressText="Loaded output" if segments else "",
    )
    return {"ok": True, "segments": len(segments)}


def export_worker(payload: dict[str, object]) -> None:
    try:
        process_id = str(payload.get("processId") or active_process()["id"])
        process = active_process() if process_id == str(active_process()["id"]) else next(
            item for item in get_state().get("processes", []) if item.get("id") == process_id
        )
        with TASKS_LOCK:
            TASKS[process_id] = {"cancel": threading.Event(), "process": None, "outputDir": process["outputDir"]}
        out_dir = Path(str(process["outputDir"]))
        ffmpeg = Path(str(payload.get("ffmpeg") or DEFAULT_FFMPEG))
        selected = [int(x) for x in payload.get("selected", [])]
        if not selected:
            raise RuntimeError("No segments selected.")
        final_video = Path(str(payload.get("finalVideo") or default_final_video(process_id, str(process.get("inputVideo") or ""))))
        if not final_video.is_absolute():
            final_video = out_dir / final_video
        final_video.parent.mkdir(parents=True, exist_ok=True)

        set_process_state(
            process_id,
            busy=True,
            message="Exporting selected clips...",
            error="",
            progressPercent=15,
            progressText=f"Preparing {len(selected)} selected clips",
            canCancel=True,
        )
        concat_path = out_dir / "selected_concat.txt"
        lines: list[str] = []
        total = max(1, len(selected))
        for row_number, index in enumerate(selected, start=1):
            clip = find_segment_clip(out_dir, index).resolve()
            if clip.exists():
                safe = str(clip).replace("\\", "/").replace("'", "'\\''")
                lines.append(f"file '{safe}'")
            if process_cancelled(process_id):
                raise CancelledError("Task was cancelled.")
            set_process_state(
                process_id,
                progressPercent=15 + round((row_number / total) * 35),
                progressText=f"Preparing selected clips {row_number}/{total}",
            )
        concat_path.write_text("\n".join(lines), encoding="utf-8")
        set_process_state(process_id, message="Joining selected clips...", progressPercent=60, progressText="Joining selected clips")
        run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(final_video),
            ],
            process_id,
        )
        set_process_state(
            process_id,
            busy=False,
            message="Export complete.",
            finalVideo=str(final_video),
            progressPercent=100,
            progressText="Export complete",
            canCancel=False,
        )
        with TASKS_LOCK:
            TASKS.pop(process_id, None)
    except CancelledError:
        with TASKS_LOCK:
            TASKS.pop(process_id, None)
        set_process_state(
            process_id,
            busy=False,
            message="Export stopped.",
            error="",
            progressPercent=0,
            progressText="Stopped",
            canCancel=False,
        )
    except Exception as exc:
        with TASKS_LOCK:
            TASKS.pop(process_id, None)
        set_process_state(process_id, busy=False, message="Export failed.", error=str(exc), progressPercent=0, progressText="Export failed", canCancel=False)


def clear_clips(payload: dict[str, object]) -> dict[str, object]:
    process_id = str(payload.get("processId") or active_process()["id"])
    process = active_process() if process_id == str(active_process()["id"]) else next(
        item for item in get_state().get("processes", []) if item.get("id") == process_id
    )
    if process.get("busy"):
        raise RuntimeError("Stop this process before clearing its temporary folder.")
    out_dir = Path(str(process.get("outputDir") or "")).resolve()
    app_root = APP_DIR.resolve()
    if not str(out_dir).lower().startswith(str(app_root).lower()):
        raise RuntimeError(f"Refusing to clear process folder outside project folder: {out_dir}")
    if out_dir == app_root or out_dir.parent == app_root:
        raise RuntimeError(f"Refusing to clear unsafe folder: {out_dir}")

    deleted = 0
    deleted_bytes = 0
    if out_dir.exists():
        for target in out_dir.rglob("*"):
            if target.is_file():
                deleted += 1
                deleted_bytes += target.stat().st_size
        shutil.rmtree(out_dir, ignore_errors=True)

    set_process_state(
        process_id,
        message=f"Deleted current process cache folder with {deleted} files.",
        segments=[],
        outputDir=str(out_dir),
        progressPercent=0,
        progressText="Process cache folder deleted",
        error="",
    )
    return {"ok": True, "deleted": deleted, "bytes": deleted_bytes}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        send_cache_headers(self)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        send_cache_headers(self)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/state":
            self.send_json(get_state())
            return
        if parsed.path.startswith("/file/"):
            rel = urllib.parse.unquote(parsed.path.removeprefix("/file/"))
            target = (APP_DIR / rel).resolve()
            if not str(target).lower().startswith(str(APP_DIR).lower()) or not target.exists():
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            send_cache_headers(self)
            self.end_headers()
            self.wfile.write(data)
            return
        target = STATIC_DIR / ("index.html" if parsed.path == "/" else parsed.path.lstrip("/"))
        target = target.resolve()
        if not str(target).lower().startswith(str(STATIC_DIR).lower()) or not target.exists():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "text/plain")
        self.send_header("Content-Length", str(len(data)))
        send_cache_headers(self)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        payload = self.read_json()
        if parsed.path == "/api/cancel":
            self.send_json(request_cancel(str(payload.get("processId") or active_process()["id"])))
            return
        if parsed.path == "/api/add-process":
            try:
                self.send_json({"ok": True, "state": add_process()})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if parsed.path == "/api/select-process":
            try:
                self.send_json({"ok": True, "state": activate_process(str(payload.get("processId") or ""))})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if parsed.path == "/api/analyze":
            process_id = str(payload.get("processId") or active_process()["id"])
            process = next((item for item in get_state().get("processes", []) if item.get("id") == process_id), None)
            if process and process.get("busy"):
                self.send_json({"ok": False, "error": "This process is already running."}, 409)
                return
            threading.Thread(target=analyze_worker, args=(payload,), daemon=True).start()
            self.send_json({"ok": True})
            return
        if parsed.path == "/api/export":
            process_id = str(payload.get("processId") or active_process()["id"])
            process = next((item for item in get_state().get("processes", []) if item.get("id") == process_id), None)
            if process and process.get("busy"):
                self.send_json({"ok": False, "error": "This process is already running."}, 409)
                return
            threading.Thread(target=export_worker, args=(payload,), daemon=True).start()
            self.send_json({"ok": True})
            return
        if parsed.path == "/api/pick-video":
            try:
                self.send_json({"ok": True, "path": pick_video_file()})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if parsed.path == "/api/clear-clips":
            try:
                self.send_json(clear_clips(payload))
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if parsed.path == "/api/load-output":
            try:
                self.send_json(load_output(payload))
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        self.send_error(404)


def main() -> int:
    port = int(os.environ.get("STICKER_REVIEW_PORT", "8787"))
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    PROCESS_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_processes()
    if not STATIC_DIR.exists():
        raise SystemExit(f"Missing static folder: {STATIC_DIR}")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Sticker review server: http://127.0.0.1:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
