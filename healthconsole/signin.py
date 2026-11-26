"""Local only: runs `az login` and `az logout` for the page, and reads which tenants the Azure CLI is signed in to, so
none of it needs a terminal. The console keeps using the Azure CLI's session. Never mounted in a hosted app (see
`create_local_app`)."""
import json
import os
import re
import shutil
import subprocess
import threading
import time

TIMEOUT_SECONDS = 300
OUTPUT_LINES = 40
# A tenant ID or a domain: the only input that reaches the command line, so it's checked strictly.
TENANT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                    r"|^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}\.)+[A-Za-z]{2,63}$")
# A sign-in name or an object ID: what `az logout --username` takes, checked as strictly as the tenant.
USERNAME = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$"
                      r"|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CLI_TIMEOUT_SECONDS = 60
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# A tenant `az login` couldn't finish (MFA, Conditional Access): it warns, skips it and still exits 0.
_SKIPPED = re.compile(r"Authentication failed against tenant\s+([0-9a-fA-F-]{36})(?:\s+'([^']*)')?:\s*(.*)")
_MFA = re.compile(r"AADSTS50076|AADSTS50079|multi-factor", re.IGNORECASE)
_DEVICE = re.compile(r"(https://\S+?)[\s,.]+and enter the code\s+([A-Z0-9-]{6,16})", re.IGNORECASE)
IDLE, RUNNING, DONE, FAILED, CANCELLED = "idle", "running", "done", "failed", "cancelled"


class SignInError(ValueError):
    pass


def _az(az=None):
    az = az or shutil.which("az")
    if not az:
        raise SignInError("The Azure CLI (az) isn't installed, or isn't on the PATH the console was started with.")
    return az


def _run(argv, run):
    result = run(argv, capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        lines = [line for line in _ANSI.sub("", result.stderr or "").splitlines() if line.strip()]
        raise SignInError(lines[-1][:300] if lines else f"{' '.join(argv[1:3])} failed.")
    return result.stdout


def accounts(az=None, run=subprocess.run):
    """[{id, name, domain, account}]: every tenant the Azure CLI is signed in to, across accounts, from its own local
    list (`az account list --all`; no network). The page adds them to the tenant picker."""
    rows = json.loads(_run([_az(az), "account", "list", "--all", "--output", "json"], run) or "[]")
    found = {}
    for row in rows:
        tenant = row.get("tenantId") or ""
        if tenant and tenant not in found:
            found[tenant] = {"id": tenant, "name": row.get("tenantDisplayName") or row.get("tenantDefaultDomain") or tenant,
                             "domain": row.get("tenantDefaultDomain") or "", "account": (row.get("user") or {}).get("name") or ""}
    return sorted(found.values(), key=lambda t: t["name"].lower())


def logout(username, az=None, run=subprocess.run):
    """`az logout --username X`: signs that account out of the Azure CLI on this machine (terminals too)."""
    if not USERNAME.match(username or ""):
        raise SignInError("That isn't a sign-in name or object ID.")
    _run([_az(az), "logout", "--username", username], run)


def command(tenant=None, device_code=False, az=None):
    """The fixed argv: `az login --allow-no-subscriptions [--tenant X] [--use-device-code] --output none`. Without
    --allow-no-subscriptions an account with no subscription it can use (a personal one, say) signs in but is never
    saved, and every token request then says "Please run 'az login' to set up an account"."""
    argv = [_az(az), "login", "--allow-no-subscriptions", "--output", "none"]
    if tenant:
        if not TENANT.match(tenant):
            raise SignInError("That isn't a tenant ID or domain.")
        argv += ["--tenant", tenant]
    if device_code:
        argv.append("--use-device-code")
    return argv


class AzLogin:
    """One `az login` at a time; the page polls `state()`. `on_success` drops cached tokens so the new session is used.
    `list_accounts` reads what the CLI saved, when it skipped a tenant."""

    def __init__(self, on_success=None, popen=subprocess.Popen, clock=time.monotonic, list_accounts=None):
        self._popen, self._clock, self._on_success = popen, clock, on_success
        self._list_accounts = list_accounts or accounts
        self._lock = threading.Lock()
        self._process = None
        self._state = {"status": IDLE}

    def refresh_clients(self):
        """After a sign-out: the same token drop as after a sign-in."""
        if self._on_success:
            self._on_success()

    def state(self):
        with self._lock:
            return dict(self._state, output=list(self._state.get("output", [])),
                        skipped=[dict(t) for t in self._state.get("skipped", [])])

    def start(self, tenant=None, device_code=False):
        argv = command(tenant, device_code)
        with self._lock:
            if self._state["status"] == RUNNING:
                raise SignInError("A sign-in is already running: finish it in the browser, or cancel it first.")
            env = {**os.environ, "AZURE_CORE_LOGIN_EXPERIENCE_V2": "off"}  # no subscription picker; config untouched
            self._process = self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, env=env)
            self._state = {"status": RUNNING, "tenant": tenant or "", "device_code": bool(device_code), "code": None,
                           "url": None, "output": [], "skipped": [], "started": self._clock(), "message": ""}
            process = self._process
        threading.Thread(target=self._read, args=(process,), daemon=True).start()
        threading.Thread(target=self._watchdog, args=(process,), daemon=True).start()
        return self.state()

    def _read(self, process):
        for raw in process.stdout:
            line = _ANSI.sub("", raw).rstrip()
            if not line:
                continue
            with self._lock:
                if self._process is not process:
                    return
                self._state["output"] = (self._state["output"] + [line])[-OUTPUT_LINES:]
                found = _DEVICE.search(line)
                if found:
                    self._state["url"], self._state["code"] = found.group(1), found.group(2)
                skipped = _SKIPPED.search(line)
                if skipped and all(t["id"] != skipped.group(1) for t in self._state["skipped"]):
                    self._state["skipped"].append({"id": skipped.group(1), "name": skipped.group(2) or "",
                                                   "reason": skipped.group(3)[:300], "mfa": bool(_MFA.search(line))})
        code = process.wait()
        with self._lock:
            skipped = list(self._state["skipped"])
        ok = code == 0
        if ok and skipped:
            try:
                ok = bool(self._list_accounts())
            except (SignInError, ValueError, OSError, subprocess.SubprocessError):
                pass  # can't tell: the page's own check finds out
        with self._lock:
            if self._process is not process or self._state["status"] != RUNNING:
                return
            self._state["status"] = DONE if ok else FAILED
            names = ", ".join(t["name"] or t["id"] for t in skipped)
            self._state["message"] = ("Signed in." if ok else
                                      f"az login finished, but no account was saved: {names} needs a sign-in of its own."
                                      if code == 0 else
                                      next((line for line in reversed(self._state["output"]) if "error" in line.lower()),
                                           f"az login stopped with exit code {code}."))
        if ok and self._on_success:
            self._on_success()

    def _watchdog(self, process):
        deadline = self._clock() + TIMEOUT_SECONDS
        while process.poll() is None:
            if self._clock() > deadline:
                self._stop(process, FAILED, f"No sign-in within {TIMEOUT_SECONDS // 60} minutes, so it was stopped.")
                return
            time.sleep(0.5)

    def cancel(self):
        with self._lock:
            process = self._process if self._state["status"] == RUNNING else None
        if process:
            self._stop(process, CANCELLED, "Cancelled.")
        return self.state()

    def _stop(self, process, status, message):
        with self._lock:
            if self._process is not process or self._state["status"] != RUNNING:
                return
            self._state["status"], self._state["message"] = status, message
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
