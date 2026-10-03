from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from .recording_cleanup import recover_realtime_wavs

logger = logging.getLogger("uvicorn.error")


class RecordingWorkers:
    """Postgres is the durable queue. Worker locks are released on process death."""

    def __init__(
        self,
        *,
        repository,
        settings,
        transcriber_factory: Callable,
        process_recording: Callable,
    ):
        self.repository = repository
        self.settings = settings
        self.transcriber_factory = transcriber_factory
        self.process_recording = process_recording
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        # A single local model is shared and must not be loaded/transcribed concurrently.
        count = (
            1
            if self.settings.transcription_provider == "whisper"
            else self.settings.transcription_workers
        )
        for index in range(count):
            thread = threading.Thread(
                target=self.run, name=f"recording-{index}", daemon=True
            )
            self.threads.append(thread)
            thread.start()

    def run(self) -> None:
        while not self.stop.is_set():
            worked = False
            try:
                if hasattr(self.repository, "recoverable_realtime_units"):
                    recover_realtime_wavs(self.repository, self.settings.recordings_dir)
                for job in self.repository.get_recording_jobs():
                    if self.stop.is_set():
                        return
                    with self.repository.job_lock(101, job["id"]) as locked:
                        if not locked or not self.repository.begin_recording_job(
                            job["id"]
                        ):
                            continue
                        worked = True
                        metadata = job["metadata"]
                        try:
                            path = (
                                self.settings.recordings_dir / job["recording_filename"]
                            ).resolve()
                            if path.parent != self.settings.recordings_dir.resolve():
                                raise ValueError(
                                    "Recording must be inside the recordings directory"
                                )
                            self.process_recording(
                                recording_path=path,
                                recording_id=job["id"],
                                recording_lock_held=True,
                                session_id=job["session_id"],
                                discord_id=metadata["discord_id"],
                                username=metadata["username"],
                                display_name=metadata.get("display_name"),
                                channel_name=metadata["channel_name"],
                                recording_started_at=datetime.fromisoformat(
                                    metadata["recording_started_at"]
                                ),
                                provider_job_id=job["provider_job_id"],
                                provider_key_name=job["provider_key_name"],
                                settings=self.settings,
                                repository=self.repository,
                                transcriber=self.transcriber_factory(),
                            )
                        except Exception as exc:
                            self.repository.mark_recording_failed(
                                job["id"], type(exc).__name__
                            )
                            logger.exception("Recording worker failed id=%s", job["id"])
            except Exception:
                logger.exception(
                    "Recording queue unavailable; retrying on next worker tick"
                )
            if not worked:
                self.stop.wait(2)

    def close(self) -> None:
        self.stop.set()
        # Active jobs retain metadata and remote job IDs for the next startup.
        for thread in self.threads:
            thread.join(timeout=1)
