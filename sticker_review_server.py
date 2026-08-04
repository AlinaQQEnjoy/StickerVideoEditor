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
WORK_DIR = APP_DIR / "review_output"
STATIC_DIR = APP_DIR / "web"
STATE_LOCK = threading.Lock()
STATE: dict[str, object] = {
    "busy": False,
    "message": "Ready.",
    "inputVideo": "",
    "outputDir": str(WORK_DIR),
    "segments": [],
    "finalVideo": "",
    "error": "",
}


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


def get_state() -> dict[str, object]:
    with STATE_LOCK:
        return json.loads(json.dumps(STATE))


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout)


def parse_time_to_seconds(text: str) -> float:
    parts = text.replace(".", ":").split(":")
    if len(parts) < 4:
        return float(text)
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]) + int(parts[3]) / 1000.0


def rel_url(path: Path) -> str:
    rel = path.resolve().relative_to(APP_DIR)
    return "/file/" + urllib.parse.quote(rel.as_posix())


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


def build_previews(input_video: Path, out_dir: Path, ffmpeg: Path) -> None:
    clips_dir = out_dir / "clips"
    thumbs_dir = out_dir / "thumbs"
    clips_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    for old in list(clips_dir.glob("segment_*.mp4")) + list(thumbs_dir.glob("segment_*.jpg")):
        old.unlink(missing_ok=True)

    with (out_dir / "segments.csv").open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        index = int(row["index"])
        start = parse_time_to_seconds(row["start"])
        duration = float(row["duration"])
        clip = clips_dir / f"segment_{index:03d}.mp4"
        thumb = thumbs_dir / f"segment_{index:03d}.jpg"
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
                "-vf",
                "scale=540:-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-an",
                "-movflags",
                "+faststart",
                str(clip),
            ]
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
            ]
        )


def analyze_worker(payload: dict[str, object]) -> None:
    try:
        input_video = Path(str(payload.get("inputVideo", "")).strip().strip('"'))
        if not input_video.exists():
            raise RuntimeError(f"Input video not found: {input_video}")
        ffmpeg = Path(str(payload.get("ffmpeg") or DEFAULT_FFMPEG))
        python = Path(str(payload.get("python") or DEFAULT_PYTHON))
        if not ffmpeg.exists():
            raise RuntimeError(f"FFmpeg not found: {ffmpeg}")
        if not python.exists():
            raise RuntimeError(f"Python not found: {python}")

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        out_dir = WORK_DIR / timestamp
        out_dir.mkdir(parents=True, exist_ok=True)
        set_state(busy=True, message="Detecting audio peaks...", inputVideo=str(input_video), outputDir=str(out_dir), error="")

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
        run(cmd)

        set_state(message="Building preview clips...")
        build_previews(input_video, out_dir, ffmpeg)
        segments = read_segments(out_dir)
        set_state(busy=False, message=f"Detected {len(segments)} preview segments.", segments=segments, outputDir=str(out_dir))
    except Exception as exc:
        set_state(busy=False, message="Analyze failed.", error=str(exc))


def load_output(payload: dict[str, object]) -> dict[str, object]:
    out_dir = Path(str(payload.get("outputDir", "")).strip().strip('"'))
    if not out_dir.exists():
        raise RuntimeError(f"Output folder not found: {out_dir}")
    segments_path = out_dir / "segments.csv"
    if not segments_path.exists():
        raise RuntimeError(f"Segments file not found: {segments_path}")

    input_video = str(payload.get("inputVideo") or "")
    final_video = str(payload.get("finalVideo") or "")
    segments = read_segments(out_dir)
    set_state(
        busy=False,
        message=f"Loaded {len(segments)} clips from PowerShell run.",
        inputVideo=input_video,
        outputDir=str(out_dir),
        segments=segments,
        finalVideo=final_video,
        error="",
    )
    return {"ok": True, "segments": len(segments)}


def export_worker(payload: dict[str, object]) -> None:
    try:
        state = get_state()
        out_dir = Path(str(state["outputDir"]))
        ffmpeg = Path(str(payload.get("ffmpeg") or DEFAULT_FFMPEG))
        selected = [int(x) for x in payload.get("selected", [])]
        if not selected:
            raise RuntimeError("No segments selected.")
        final_video = Path(str(payload.get("finalVideo") or (out_dir / "sticker_action_final.mp4")))
        if not final_video.is_absolute():
            final_video = out_dir / final_video
        final_video.parent.mkdir(parents=True, exist_ok=True)

        set_state(busy=True, message="Exporting selected clips...", error="")
        concat_path = out_dir / "selected_concat.txt"
        lines: list[str] = []
        for index in selected:
            clip = find_segment_clip(out_dir, index).resolve()
            if clip.exists():
                safe = str(clip).replace("\\", "/").replace("'", "'\\''")
                lines.append(f"file '{safe}'")
        concat_path.write_text("\n".join(lines), encoding="utf-8")
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
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-movflags",
                "+faststart",
                str(final_video),
            ]
        )
        set_state(busy=False, message="Export complete.", finalVideo=str(final_video))
    except Exception as exc:
        set_state(busy=False, message="Export failed.", error=str(exc))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

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
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if get_state().get("busy"):
            self.send_json({"ok": False, "error": "Server is busy."}, 409)
            return
        payload = self.read_json()
        if parsed.path == "/api/analyze":
            threading.Thread(target=analyze_worker, args=(payload,), daemon=True).start()
            self.send_json({"ok": True})
            return
        if parsed.path == "/api/export":
            threading.Thread(target=export_worker, args=(payload,), daemon=True).start()
            self.send_json({"ok": True})
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
    if not STATIC_DIR.exists():
        raise SystemExit(f"Missing static folder: {STATIC_DIR}")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Sticker review server: http://127.0.0.1:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
