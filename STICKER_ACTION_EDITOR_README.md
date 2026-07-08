# Sticker Action Editor

This tool auto-cuts long sticker process videos using the edit logic:

- keep about 1 second around the sticker peel/lift moment
- keep about 0.8 seconds when the sticker is placed and the tweezer leaves

It uses OpenCV to score visual motion, edge changes, and activity area. Audio is
used as an optional helper because sticker peeling often creates short scratch
transients.

## Files

- `sticker_action_detector.py`: detects `peel` and `release` segments.
- `make_sticker_action_cut.ps1`: runs detection, cuts clips with FFmpeg, and
  joins them into one video.

## Quick Run

```powershell
cd D:\Codex\sticker-action-editor
.\make_sticker_action_cut.ps1 -InputVideo "D:\MellowScape\原视频\IMG_2038.MOV"
```

The final video is written to:

```text
D:\Codex\sticker-action-editor\sticker_action_output\sticker_action_cut.mp4
```

The review file is:

```text
D:\Codex\sticker-action-editor\sticker_action_output\segments.csv
```

## Useful Tuning

If it cuts too many actions:

```powershell
.\make_sticker_action_cut.ps1 -InputVideo "D:\MellowScape\原视频\IMG_2038.MOV" -MotionThreshold 2.6 -MinActionGap 1.6
```

If it misses actions:

```powershell
.\make_sticker_action_cut.ps1 -InputVideo "D:\MellowScape\原视频\IMG_2038.MOV" -MotionThreshold 1.5 -AudioThreshold 2.0
```

If the important action is only in part of the frame, restrict the ROI. Values
are `x,y,width,height`, where `0..1` means relative frame coordinates:

```powershell
.\make_sticker_action_cut.ps1 -InputVideo "D:\MellowScape\原视频\IMG_2038.MOV" -Roi "0.1,0.05,0.8,0.75"
```

If audio is music/noisy and hurts detection:

```powershell
.\make_sticker_action_cut.ps1 -InputVideo "D:\MellowScape\原视频\IMG_2038.MOV" -DisableAudio
```
