"""Remove detected pauses from a screen recording."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    fps: Fraction
    sample_rate: int
    video_codec: str


@dataclass(frozen=True)
class CutPlan:
    segments: list[tuple[Fraction, Fraction]]
    source_duration: float

    @property
    def kept(self) -> float:
        return float(sum((end - start for start, end in self.segments), Fraction(0)))

    @property
    def removed(self) -> float:
        return max(0.0, self.source_duration - self.kept)


def probe(source: Path) -> MediaInfo:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Media probing failed.")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(result.stderr.strip() or "Media probing returned invalid JSON.") from exc
    video = next((stream for stream in data["streams"] if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in data["streams"] if stream.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError(f"{source.name} has no video track.")
    if audio is None:
        raise ValueError(f"{source.name} has no audio track, so there are no pauses to find.")
    try:
        fps = Fraction(video["r_frame_rate"])
        if fps <= 0:
            raise ValueError("Frame rate must be positive.")
        return MediaInfo(
            duration=float(data["format"]["duration"]),
            fps=fps,
            sample_rate=int(audio["sample_rate"]),
            video_codec=video["codec_name"],
        )
    except (KeyError, ValueError, ZeroDivisionError, TypeError) as exc:
        raise RuntimeError(f"Could not read media properties for {source.name}.") from exc


def detect_silences(
    source: Path, threshold_db: float = -40, min_silence: float = 0.5,
) -> list[tuple[float, float]]:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(source), "-map", "0:a:0",
         "-af", f"silencedetect=n={threshold_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Silence detection failed.")
    starts = re.compile(r"silence_start:\s*([-+]?\d+(?:\.\d+)?)")
    ends = re.compile(r"silence_end:\s*([-+]?\d+(?:\.\d+)?)")
    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for line in result.stderr.splitlines():
        start = starts.search(line)
        if start:
            pending = max(0.0, float(start.group(1)))
        end = ends.search(line)
        if end and pending is not None:
            silences.append((pending, float(end.group(1))))
            pending = None
    if pending is not None:
        silences.append((pending, probe(source).duration))
    return sorted(silences, key=lambda interval: interval[0])


def plan_cuts(
    silences: list[tuple[float, float]], duration: float, fps: Fraction, keep_pause: float = 0.3,
) -> CutPlan:
    if not 0 <= keep_pause <= 2:
        raise ValueError("Keep pause must be between 0 and 2 seconds.")
    cuts: list[tuple[float, float]] = []
    for start, end in silences:
        if end - start <= keep_pause:
            continue
        if start <= 0.01 and end >= duration - 0.01:
            cut = (start, end)
        elif start <= 0.01:
            cut = (0.0, end - keep_pause / 2)
        elif end >= duration - 0.01:
            cut = (start + keep_pause / 2, duration)
        else:
            cut = (start + keep_pause / 2, end - keep_pause / 2)
        left, right = max(0.0, cut[0]), min(duration, cut[1])
        if left < right:
            cuts.append((left, right))
    if not cuts:
        raise ValueError(f"No pauses longer than {keep_pause:g} s were found.")  # plan() names min_silence instead

    merged_cuts: list[tuple[float, float]] = []
    for start, end in sorted(cuts):
        if merged_cuts and start <= merged_cuts[-1][1]:
            merged_cuts[-1] = (merged_cuts[-1][0], max(end, merged_cuts[-1][1]))
        else:
            merged_cuts.append((start, end))

    def snap(time: float) -> Fraction:
        return Fraction(round(Fraction(time) * fps), 1) / fps

    segments: list[tuple[Fraction, Fraction]] = []
    cursor = 0.0
    for start, end in merged_cuts:
        if cursor < start:
            segments.append((snap(cursor), snap(start)))
        cursor = end
    if cursor < duration:
        segments.append((snap(cursor), snap(duration)))

    kept: list[tuple[Fraction, Fraction]] = []
    for start, end in segments:
        if start >= end:
            continue
        if kept and start <= kept[-1][1]:
            kept[-1] = (kept[-1][0], max(kept[-1][1], end))
        else:
            kept.append((start, end))
    if not kept:
        raise ValueError("The whole recording is silent.")
    return CutPlan(kept, duration)


def plan(
    source: Path, threshold_db: float = -40, min_silence: float = 0.5, keep_pause: float = 0.3,
) -> CutPlan:
    info = probe(source)
    silences = detect_silences(source, threshold_db, min_silence)
    if not silences:
        raise ValueError(f"No pauses longer than {min_silence:g} s were found.")
    return plan_cuts(silences, info.duration, info.fps, keep_pause)


def select_expression(segments: list[tuple[Fraction, Fraction]], fps: Fraction) -> str:
    """An ffmpeg expression that is non-zero inside the kept segments."""
    half_frame = Fraction(1) / (2 * fps)
    # Shift bounds so ffmpeg's floating point comparison includes boundary frames.
    terms = [f"between(t,{float(start - half_frame):.6f},{float(end - half_frame):.6f})"
             for start, end in segments]

    # ffmpeg's parser nests once per "+", and gives up at about 100 levels, so a flat
    # a+b+c+… fails on long recordings. A balanced tree nests log2(len(terms)) deep.
    def tree(low: int, high: int) -> str:
        if high - low == 1:
            return terms[low]
        middle = (low + high) // 2
        return f"({tree(low, middle)}+{tree(middle, high)})"

    return tree(0, len(terms))


def render(
    source: Path, plan: CutPlan, output: Path, progress: Callable[[float], None] | None = None,
) -> Path:
    info = probe(source)
    fps = info.fps
    sr = info.sample_rate
    expression = select_expression(plan.segments, fps)
    # One audio selection unit should span a video frame when samples divide evenly.
    samples = sr * fps.denominator // fps.numerator if sr * fps.denominator % fps.numerator == 0 else round(sr / 1000)
    graph = (
        f"[0:v]fps={fps.numerator}/{fps.denominator},select='{expression}',setpts=N/FRAME_RATE/TB[v];"
        f"[0:a]asetnsamples=n={samples}:p=0,aselect='{expression}',asetpts=N/SR/TB[a]"
    )
    if info.video_codec == "av1":
        encoders = (["-c:v", "av1_nvenc", "-preset", "p5", "-cq", "28"],
                    ["-c:v", "libsvtav1", "-preset", "8", "-crf", "30"])
    elif info.video_codec == "hevc":
        encoders = (["-c:v", "hevc_nvenc", "-preset", "p5", "-cq", "26"],
                    ["-c:v", "libx264", "-preset", "medium", "-crf", "20"])
    else:
        encoders = (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23"],
                    ["-c:v", "libx264", "-preset", "medium", "-crf", "20"])
    output.parent.mkdir(parents=True, exist_ok=True)
    last_error = "Video rendering failed."
    for encoder in encoders:
        filter_fd, filter_name = tempfile.mkstemp(suffix=".filter", prefix=".cutter-", dir=output.parent)
        log_fd, log_name = tempfile.mkstemp(suffix=".log", prefix=".cutter-err-", dir=output.parent)
        video_fd, video_name = tempfile.mkstemp(suffix=".mp4", prefix=".cut-", dir=output.parent)
        filter_file, log_file, temporary = Path(filter_name), Path(log_name), Path(video_name)
        try:
            with os.fdopen(filter_fd, "w") as handle:
                handle.write(graph)
            os.close(video_fd)
            command = [
                "ffmpeg", "-hide_banner", "-y", "-i", str(source),
                "-/filter_complex", str(filter_file), "-map", "[v]", "-map", "[a]",
                *encoder, "-pix_fmt", "yuv420p", "-c:a", "flac", "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", "-loglevel", "error", str(temporary),
            ]
            with os.fdopen(log_fd, "w") as error_handle:
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_handle, text=True)
                assert proc.stdout is not None
                with proc.stdout:
                    for line in proc.stdout:
                        if progress is not None and plan.kept > 0 and line.startswith(("out_time_us=", "out_time_ms=")):
                            value = line.partition("=")[2].strip()
                            if value.isdecimal():
                                progress(min(1.0, (int(value) / 1_000_000) / plan.kept))
                proc.wait()
            if proc.returncode == 0:
                if progress is not None:
                    progress(1.0)
                temporary.chmod(0o644)  # mkstemp makes it 0600; match the recordings around it
                os.replace(temporary, output)
                return output
            last_error = "\n".join(log_file.read_text().splitlines()[-15:]) or "Video rendering failed."
        finally:
            filter_file.unlink(missing_ok=True)
            log_file.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
    raise RuntimeError(last_error)


def cut_silences(
    source: Path, output: Path, threshold_db: float = -40, min_silence: float = 0.5,
    keep_pause: float = 0.3, progress: Callable[[float], None] | None = None,
) -> CutPlan:
    result = plan(source, threshold_db, min_silence, keep_pause)
    render(source, result, output, progress)
    return result


if __name__ == "__main__":
    def clock_time(seconds: float) -> str:
        minutes = int(seconds // 60)
        return f"{minutes}:{seconds - 60 * minutes:04.1f}"

    def summary(result: CutPlan) -> str:
        return (
            f"{len(result.segments)} segments · kept {clock_time(result.kept)} "
            f"of {clock_time(result.source_duration)} (removed {clock_time(result.removed)})"
        )

    parser = argparse.ArgumentParser(description="Remove pauses from a screen recording.")
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--threshold-db", type=float, default=-40)
    parser.add_argument("--min-silence", type=float, default=0.5)
    parser.add_argument("--keep-pause", type=float, default=0.3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    output = args.output.expanduser() if args.output is not None else source.with_name(f"{source.stem}-cut.mp4")
    if output.exists():
        print(f"error: {output} already exists", file=sys.stderr)
        sys.exit(1)
    try:
        if args.dry_run:
            result = plan(source, args.threshold_db, args.min_silence, args.keep_pause)
            for start, end in result.segments:
                print(f"{float(start):.3f}–{float(end):.3f}")
        else:
            def show_progress(fraction: float) -> None:
                print(f"\rrendering: {fraction * 100:.0f}%", end="", flush=True, file=sys.stderr)

            result = cut_silences(
                source, output, args.threshold_db, args.min_silence, args.keep_pause, show_progress,
            )
            print(file=sys.stderr)
        print(summary(result))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
