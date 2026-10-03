"""Remove committed recording media without sacrificing durable retry inputs."""

from __future__ import annotations

import logging
import struct
import threading
import time
from contextlib import ExitStack
from pathlib import Path

logger = logging.getLogger("uvicorn.error")
MEDIA_SUFFIXES = (".speechmatics.json", ".speechmatics.tmp", ".deepgram.json", ".deepgram.json.tmp", ".deepgram-mono.wav")
SIDECAR_SUFFIXES = (*MEDIA_SUFFIXES, ".request.json")


def recording_paths(
    directory: Path, filename: str, *, include_request=False
) -> list[Path]:
    root = directory.resolve()
    # Never follow links or delete anything outside the shared recordings directory.
    if not filename or Path(filename).name != filename:
        raise ValueError("Recording filename must be inside recordings directory")
    suffixes = SIDECAR_SUFFIXES if include_request else MEDIA_SUFFIXES
    paths = [root / filename] + [root / (filename + suffix) for suffix in suffixes]
    for path in paths:
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError("Recording cleanup cannot follow symlinks")
    return paths


def remove_recording_files(
    directory: Path, filename: str, *, include_request=False
) -> None:
    """Idempotent removal; failures propagate to callers that require explicit erasure."""
    for path in recording_paths(directory, filename, include_request=include_request):
        path.unlink(missing_ok=True)


class RecordingCleanup:
    def __init__(self, *, repository, settings):
        self.repository = repository
        self.directory = settings.recordings_dir
        self.interval = max(
            1, getattr(settings, "recording_cleanup_interval_seconds", 60)
        )
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self.run, name="recording-cleanup", daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.sweep()
            except Exception:
                logger.exception("Recording cleanup unavailable; retrying next tick")
            self.stop.wait(self.interval)

    def cleanup_completed_file(
        self, filename: str, snapshot=None, *, held_recording_id=None
    ) -> bool:
        """Recheck ownership/status under the same locks used by transcription workers."""
        try:
            rows = snapshot or self.repository.get_recordings_for_cleanup(
                [filename]
            ).get(filename, [])
            if not rows or any(row["status"] not in {"completed", "discarded_no_credits"} for row in rows):
                return False
            with ExitStack() as stack:
                for row in sorted(rows, key=lambda value: value["id"]):
                    if row["id"] == held_recording_id:
                        continue
                    if not stack.enter_context(
                        self.repository.job_lock(101, row["id"])
                    ):
                        return False
                current = self.repository.get_recordings_for_cleanup([filename]).get(
                    filename, []
                )
                if not current or {row["id"] for row in current} != {
                    row["id"] for row in rows
                }:
                    return False
                if any(row["status"] not in {"completed", "discarded_no_credits"} for row in current):
                    return False
                remove_recording_files(self.directory, filename, include_request=any(row["status"] == "discarded_no_credits" for row in current))
                return True
        except Exception:
            # Transcription is already committed. Storage errors must never turn it into a failed job.
            logger.exception(
                "Recording cleanup failed filename=%s; will retry", filename
            )
            return False

    def sweep(self) -> None:
        if hasattr(self.repository, "discarded_cleanup_jobs"):
            for recording_id, filename in self.repository.discarded_cleanup_jobs():
                try:
                    remove_recording_files(self.directory, filename, include_request=True)
                    self.repository.acknowledge_discard_cleanup(recording_id)
                except Exception:
                    logger.warning("Discard cleanup pending unit=%s", recording_id)
        if not self.directory.exists():
            return
        candidates = set()
        for path in self.directory.iterdir():
            if self.stop.is_set():
                return
            if not path.is_file() or path.is_symlink():
                continue
            filename = path.name
            for suffix in SIDECAR_SUFFIXES:
                if filename.endswith(suffix):
                    filename = filename[: -len(suffix)]
                    break
            candidates.add(filename)
        ordered = sorted(candidates)
        for offset in range(0, len(ordered), 200):
            snapshot = self.repository.get_recordings_for_cleanup(
                ordered[offset : offset + 200]
            )
            for filename, rows in snapshot.items():
                if self.stop.is_set():
                    return
                self.cleanup_completed_file(filename, rows)


def recover_realtime_wavs(repository, directory: Path) -> None:
    """A stale fallback WAV has no live capture writer; repair crash-truncated headers."""
    for recording_id, filename in repository.recoverable_realtime_units():
        try:
            path = recording_paths(directory, filename)[0]
            if time.time() - path.stat().st_mtime < 15:
                continue
            with path.open("r+b") as wav:
                header = bytearray(wav.read(44))
                size = path.stat().st_size - 44
                if (len(header) != 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE"
                        or header[36:40] != b"data" or size < 0 or size % 4 or size > 2**32 - 37):
                    raise ValueError("Invalid safety WAV")
                struct.pack_into("<I", header, 4, size + 36)
                struct.pack_into("<I", header, 40, size)
                wav.seek(0)
                wav.write(header)
                wav.flush()
            repository.admit_realtime_recovery(recording_id)
            logger.info("Realtime WAV recovered unit=%s", recording_id)
        except FileNotFoundError:
            # Retain the unit for operator inspection/retry; never fake completion.
            pass
        except Exception:
            logger.warning("Realtime recovery deferred unit=%s", recording_id)
