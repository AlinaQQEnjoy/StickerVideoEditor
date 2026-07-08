# Video Editor

Local sticker-process video editor for detecting, reviewing, and exporting short
clips from long craft videos.

The current editing grammar keeps:

- about 1 second around the sticker peel/lift moment
- about 0.8 seconds when the sticker is placed and the tweezer leaves

## Run The Review UI

```powershell
python sticker_review_server.py
```

Open:

```text
http://127.0.0.1:8787
```

## Main Files

- `sticker_action_detector.py`: OpenCV/audio detection for peel and release events.
- `sticker_review_server.py`: local HTTP server and export API.
- `web/`: browser UI for reviewing, selecting, and exporting segments.
- `make_sticker_action_cut.ps1`: command-line batch cutter.

## Requirements

- Python 3
- OpenCV: `pip install opencv-python`
- FFmpeg
