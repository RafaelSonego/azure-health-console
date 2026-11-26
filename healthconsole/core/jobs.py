"""Long work runs as a job (start, poll, result) so no request waits on a whole graph walk.
A job can be cancelled: its Azure client and its progress callback raise `Cancelled` from then on."""
import logging
import threading
import uuid
from collections import OrderedDict

from . import eventlog
from .arm import Cancelled
from .credentials import CredentialError

logger = logging.getLogger(__name__)

RUNNING, DONE, FAILED, CANCELLED = "running", "done", "failed", "cancelled"
MAX_RUNNING_PER_OWNER = 8  # a page starts at most a handful at once; a cancelled one counts until its thread ends
MAX_RUNNING = 24  # the whole process, when the blueprint serves several people


class TooManyJobs(RuntimeError):
    """A start refused because enough work is already running; nothing was started."""


class JobRunner:
    def __init__(self, max_jobs=20, max_running_per_owner=MAX_RUNNING_PER_OWNER, max_running=MAX_RUNNING):
        self._jobs = OrderedDict()
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self._per_owner, self._max_running = max_running_per_owner, max_running
        self._active = {}  # job ID -> owner, while its thread runs (cancelled or not)

    def start(self, owner, work, kind=None, replace=False, cancel=None):
        """`work(progress, publish)` returns the result (maybe publishing a partial one first); `owner` keeps
        jobs private. `replace` cancels the owner's running job of the same `kind` first. Raises TooManyJobs when
        the owner, or the process, already has its limit of threads running."""
        job_id = uuid.uuid4().hex
        cancel = cancel or threading.Event()
        job = {"owner": owner, "kind": kind, "status": RUNNING, "phase": "Starting", "result": None,
               "error": None, "credential": None, "partial": None, "cancel": cancel}
        with self._lock:
            mine = sum(1 for o in self._active.values() if o == owner)
            if mine >= self._per_owner or len(self._active) >= self._max_running:
                raise TooManyJobs(f"{mine if mine >= self._per_owner else len(self._active)} checks are already "
                                  "running. Wait for one to finish (or close a tab that started one), then try again.")
            if replace and kind:
                for other in self._jobs.values():
                    if other["owner"] == owner and other["kind"] == kind and other["status"] == RUNNING:
                        other["cancel"].set()
            self._jobs[job_id] = job
            self._active[job_id] = owner
            finished = [k for k, j in self._jobs.items() if j["status"] != RUNNING]
            while len(self._jobs) > self._max_jobs and finished:  # a running job is never dropped from view
                self._jobs.pop(finished.pop(0))

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
                job["error"] = str(error)
                if isinstance(error, CredentialError):  # signed out mid-job: the page shows its sign-in screen
                    job["credential"] = {"reason": error.reason, "command": error.command}
                else:
                    error_id = eventlog.record_error(str(error), source="job", error=error, user_id=owner,
                                                     context={"job": kind or ""})
                    logger.error("job failed", exc_info=error, extra={"recorded": True})
                    if error_id:
                        job["error"] += f" (error ID {error_id}: Settings → Logs has the details)"
                job["status"] = FAILED
            finally:
                with self._lock:
                    self._active.pop(job_id, None)

        try:
            threading.Thread(target=run, daemon=True).start()
        except RuntimeError:  # no thread could be started: the job never ran
            with self._lock:
                self._active.pop(job_id, None)
                self._jobs.pop(job_id, None)
            raise
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
