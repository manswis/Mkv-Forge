# Real-World Quirks & Edge Cases (Folded Into Spec & Plan)

All 7 edge cases below (plus additional technical catches) have been folded directly into [spec.md](file:///Users/manojbiswas/Documents/Projects/cranbear/MkvEdit/spec.md), [ui spec.md](file:///Users/manojbiswas/Documents/Projects/cranbear/MkvEdit/ui%20spec.md), and [implementation plan.md](file:///Users/manojbiswas/Documents/Projects/cranbear/MkvEdit/implementation%20plan.md).

## Critical Bugs Prevented

1. **The `.mp4` Extension Trap (Silent Corruption)**
   * `mkvmerge` can read `.mp4` files, but it **only outputs Matroska (`.mkv`)**.
   * **Decision:** Support both `.mkv` and `.mp4`. In *Replace Original* mode for `.mp4`, write `<stem>.mkv.mkvforge.tmp`, verify `<stem>.mkv` does not already exist (fail safely if it does), rename to `<stem>.mkv`, delete the original `.mp4` only after `<stem>.mkv` is safely in place, and update SQLite. In *Keep Both* mode, write `<stem>.remux.mkv` (auto-incrementing on collision).

2. **Double "Default" Audio Tracks**
   * Adding `--default-track-flag 0:yes` to the new EAC3/AC3 track does **not** automatically clear the `default` flag on the original AAC 5.1 track.
   * **Fix:** In `mkv_builder.py`, whenever the new Dolby track is enabled, explicitly pass `--default-track-flag <id>:no` on existing audio tracks (unless explicitly overridden).

3. **`mkvmerge` Track ID ≠ FFmpeg Stream Index & Channel Preservation**
   * `mkvmerge -J` assigns global container IDs, while FFmpeg stream indices can shift with cover art or data streams.
   * **Fix:** Count only `type == "audio"` tracks in order from `mkvmerge -J` to get relative index `N`, pass `-map 0:a:N` to FFmpeg, verify the mapped stream's codec and channel count via `ffprobe` before transcoding, and set `-ac min(source_channels, 6)` so stereo tracks are never upmixed to fake 5.1.

4. **`mkvmerge` Nanoseconds vs. FFmpeg Microseconds**
   * `mkvmerge -J` reports `container.properties.duration` in **nanoseconds**, whereas FFmpeg `-progress pipe:1` emits `out_time_us` in **microseconds** (and occasionally `N/A`).
   * **Fix:** Store `duration_us = duration_ns // 1000` during scan and ignore `out_time_us=N/A`.

5. **Empty Track Exclusion & `--track-order` File Indices**
   * Passing `--subtitle-tracks ""` or `--audio-tracks ""` causes `mkvmerge` to fail with exit code `2`.
   * **Fix:** Emit `--no-video` / `--no-audio` / `--no-subtitles` when 0 tracks of a type are kept, and build `--track-order` strictly from kept tracks using dynamic `<file_id>:<track_id>` indices (`0` = temp audio, `1` = source when Stage 1 ran; `0` = source otherwise).

## Subtle Media, Storage & Proxy Gotchas

6. **Audio Sync / Delay Drift**
   * Some MKVs have a container-level audio delay (`properties.minimum_timestamp` in nanoseconds). Extracting to raw `.eac3`/`.ac3` strips that timestamp.
   * **Fix:** If `minimum_timestamp` is non-zero, convert ns → ms (`round(min_ts_ns / 1_000_000)`) and pass `--sync 0:<delay_ms>` when muxing the converted audio track back in.

7. **Duplicate Job Guard**
   * **Fix:** In `POST /api/jobs`, reject with `409 Conflict` if `filepath` already has a `queued` or `running` job.

8. **Temp Audio Space & Disk Space Guard**
   * **Fix:** Store Stage 1 temp audio beside the target file (`<filename>.mkvforge.<ext>`), sweep `*.mkvforge.*` on startup crash cleanup, and require `1.1 * input_file_size + estimated_audio_bytes + 100 MB` free space before starting.

9. **NPM SSE Timeout & Single-Worker Lock**
   * **Fix:** Configure `sse-starlette` with `X-Accel-Buffering: no` and `ping=15` keepalives to prevent Nginx Proxy Manager's 60s `proxy_read_timeout` from killing idle SSE connections, and run `uvicorn --workers 1` so queue state and log buffers stay coherent.

## UX Guardrails

10. **Accidental Video Uncheck & Virtual Preview Row**
    * Warn before submitting if all video tracks are unchecked, and display a live `[NEW] <Title> • Default` preview row at Audio Index 0 when Dolby conversion is toggled ON.
