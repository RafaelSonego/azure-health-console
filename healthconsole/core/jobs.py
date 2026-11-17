"""Long work runs as a job (start, poll, result) so no request waits on a whole graph walk.
A job can be cancelled: its Azure client and its progress callback raise `Cancelled` from then on."""
import logging
import threading
import uuid
from collections import OrderedDict

from .arm import Cancelled
from .credentials import CredentialError

logger = logging.getLogger(__name__)

RUNNING, DONE, FAILED, CANCELLED = "running", "done", "failed", "cancelled"


class JobRunner:
    def __init__(self, max_jobs=20):
        self._jobs = OrderedDict()
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def start(self, owner, work, kind=None, replace=False, cancel=None):
        """`work(progress, publish)` returns the result (maybe publishing a partial one first); `owner` keeps
        jobs private. `replace` cancels the owner's running job of the same `kind` first."""
        job_id = uuid.uuid4().hex
        cancel = cancel or threading.Event()
        job = {"owner": owner, "kind": kind, "status": RUNNING, "phase": "Starting", "result": None,
               "error": None, "credential": None, "partial": None, "cancel": cancel}
        with self._lock:
            if replace and kind:
                for other in self._jobs.values():
                    if other["owner"] == owner and other["kind"] == kind and other["status"] == RUNNING:
                        other["cancel"].set()
            self._jobs[job_id] = job
            while len(self._jobs) > self._max_jobs:
                self._jobs.popitem(last=False)

        def progress(message):
            if cancel.is_set():
                raise Cancelled()
            job["phase"] = message

        def publish(partial):
            job["partial"] = partial

        def run():
            try:
                result = work(progress, publish)
                job["result"], job["status"] = result, CANCELLED if cancel.is_set() else DONE
            except Cancelled:
                job["status"], job["phase"] = CANCELLED, "Cancelled"
            except Exception as error:
                if cancel.is_set():
                    job["status"], job["phase"] = CANCELLED, "Cancelled"
                    return
                if isinstance(error, CredentialError):  # signed out mid-job: the page shows its sign-in screen
                    job["credential"] = {"reason": error.reason, "command": error.command}
                else:
                    logger.exception("job failed")
                job["error"] = str(error)
                job["status"] = FAILED

        threading.Thread(target=run, daemon=True).start()
        return job_id

    def cancel(self, job_id, owner):
        job = self.get(job_id, owner)
        if job and job["status"] == RUNNING:
            job["cancel"].set()
        return job

    def get(self, job_id, owner):
        with self._lock:
            job = self._jobs.get(job_id)
        if not job or job["owner"] != owner:
            return None
        return job
