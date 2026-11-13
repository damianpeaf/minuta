"""Long actions the server runs for the extension's page and for the terminal: transcribe, retry.

A job has an id and reports the same `on_progress(step, fraction, info)` the terminal shows. Jobs
marked `serial` (transcriptions) run one at a time, in order; the others run at once. A job can be
cancelled: a queued one never starts, a running one stops at its next progress report.
"""

from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from minuta.redact import redact

log = logging.getLogger(__name__)
QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"
FINISHED = (DONE, ERROR, CANCELLED)
KEEP = 50  # finished jobs remembered


class Cancelled(Exception):
    """Raised from a progress report once the job was cancelled."""


class Job:
    def __init__(self, kind: str, run: Callable, meeting: str | None, serial: bool):
        self.id = secrets.token_hex(8)
        self.kind, self.run, self.meeting, self.serial = kind, run, meeting, serial
        self.state = QUEUED
        self.step: str | None = None
        self.steps: list[str] = []  # every step reached, in order
        self.fraction: float | None = None
        self.info: dict = {}
        self.step_info: dict[str, dict] = {}  # the last numbers of every step, to say what each one did
        self.result = None
        self.error: str | None = None
        self.error_kind: str | None = None
        self.created, self.started, self.finished = time.time(), None, None
        self.cancel_requested = threading.Event()

    def progress(self, step: str, fraction, info: dict) -> None:
        if self.cancel_requested.is_set():
            raise Cancelled()
        if step != self.step:
            self.info = {}
            if step not in self.steps:
                self.steps.append(step)
        self.step, self.fraction = step, fraction
        self.info.update({k: v for k, v in (info or {}).items() if isinstance(v, (str, int, float, bool, type(None)))})
        self.step_info[step] = dict(self.info)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "meeting": self.meeting, "state": self.state, "step": self.step,
                "steps": list(self.steps), "fraction": self.fraction, "info": dict(self.info),
                "result": self.result, "error": self.error, "error_kind": self.error_kind,
                "created": self.created, "started": self.started, "finished": self.finished}


class Jobs:
    def __init__(self):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._queue: queue.Queue[Job] = queue.Queue()
        self._worker: threading.Thread | None = None
        # Held while a transcription runs; the server's automatic transcription takes it too.
        self.transcribing = threading.Lock()

    def submit(self, kind: str, run: Callable[[Callable], object], meeting: str | None = None,
               serial: bool = False) -> Job:
        """`run(progress)` does the work and returns a JSON-able result. A job of the same kind for the
        same meeting that has not finished is returned instead of starting another."""
        with self._lock:
            for j in self._jobs.values():
                if meeting and j.kind == kind and j.meeting == meeting and j.state not in FINISHED:
                    return j
            job = Job(kind, run, meeting, serial)
            self._jobs[job.id] = job
            self._prune()
            if serial:
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._serial_worker, daemon=True, name="minuta-jobs")
                    self._worker.start()
                self._queue.put(job)
        if not serial:
            threading.Thread(target=self._execute, args=(job,), daemon=True, name=f"minuta-{kind}").start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def position(self, job: Job) -> int:
        """How many serial jobs run or wait before this one."""
        with self._lock:
            ahead = [j for j in self._jobs.values() if j.serial and j.state in (QUEUED, RUNNING)]
        return ahead.index(job) if job in ahead else 0

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.cancel_requested.set()
        with self._lock:
            if job.state == QUEUED:
                job.state, job.finished = CANCELLED, time.time()
        return job

    def _prune(self) -> None:
        finished = [i for i, j in self._jobs.items() if j.state in FINISHED]
        for i in finished[: max(0, len(finished) - KEEP)]:
            del self._jobs[i]

    def _serial_worker(self) -> None:
        while True:
            job = self._queue.get()
            if job.state != QUEUED:
                continue
            with self.transcribing:
                self._execute(job)

    def _execute(self, job: Job) -> None:
        with self._lock:
            if job.state != QUEUED:
                return
            job.state, job.started = RUNNING, time.time()
        try:
            result = job.run(job.progress)
            job.result, job.state = result, DONE
        except Cancelled:
            job.state = CANCELLED
        except Exception as e:  # noqa: BLE001 - reported to whoever asked
            log.warning("%s job %s failed: %s", job.kind, job.id, redact(str(e)))
            job.error, job.state = redact(str(e))[:500], ERROR
            job.error_kind = type(e).__name__
        finally:
            job.finished = time.time()
