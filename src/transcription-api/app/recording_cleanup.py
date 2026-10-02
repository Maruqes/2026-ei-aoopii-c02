"""Remove committed recording media without sacrificing durable retry inputs."""

from __future__ import annotations

import logging
import threading
from contextlib import ExitStack
from pathlib import Path

logger = logging.getLogger("uvicorn.error")
SIDECAR_SUFFIXES = (".speechmatics.json", ".speechmatics.tmp", ".request.json")


def recording_paths(
    directory: Path, filename: str, *, include_request=False
) -> list[Path]:
    root = directory.resolve()
    # Never follow links or delete anything outside the shared recordings directory.
    if not filename or Path(filename).name != filename:
        raise ValueError("Recording filename must be inside recordings directory")
    suffixes = SIDECAR_SUFFIXES if include_request else SIDECAR_SUFFIXES[:2]
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
            if not rows or any(row["status"] != "completed" for row in rows):
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
                if any(row["status"] != "completed" for row in current):
                    return False
                remove_recording_files(self.directory, filename)
                return True
        except Exception:
            # Transcription is already committed. Storage errors must never turn it into a failed job.
            logger.exception(
                "Recording cleanup failed filename=%s; will retry", filename
            )
            return False

    def sweep(self) -> None:
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
