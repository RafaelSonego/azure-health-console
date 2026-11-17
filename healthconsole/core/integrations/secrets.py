"""Integration secrets (passwords, API keys, OAuth tokens) live in the OS keychain through `keyring`, never in
config.yaml, logs or the page. An environment variable overrides the keychain where it's blocked."""
import os
import re

LEGACY_SERVICE = "azure-health-console"  # one name for every item, used before items were named; moved on first read
FIELD_LABELS = {"password": "password", "client_secret": "client secret", "refresh_token": "sign-in token",
                "api_key": "API key", "app_key": "application key", "token": "service account token"}


class SecretStoreError(RuntimeError):
    """The keychain couldn't be used; the message says what to do instead."""


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    return None if isinstance(keyring.get_keyring(), fail.Keyring) else keyring


def keychain():
    """{"available", "backend", "hint"} for the Settings page."""
    store = _keyring()
    if store is None:
        return {"available": False, "backend": "",
                "hint": "No OS keychain is usable on this machine, so secrets can only come from environment variables."}
    backend = store.get_keyring()
    return {"available": True, "backend": getattr(backend, "name", type(backend).__name__), "hint": ""}


def env_name(target, field):
    """`target` is "servicenow" or "monitor:<name>": AHC_SERVICENOW_PASSWORD, AHC_MONITOR_NEW_RELIC_API_KEY."""
    return "AHC_" + re.sub(r"[^A-Za-z0-9]+", "_", f"{target}_{field}").strip("_").upper()


def _account(target, scope, field):
    # `scope` keeps two instances (a developer and a production instance) from sharing one stored password.
    return f"{target}|{scope}|{field}"


def service_name(target, scope, field):
    """The item's name in the keychain and its prompts: which connection, and which secret."""
    what = target.split(":", 1)[1] if target.startswith("monitor:") else f"ServiceNow {scope}" if target == "servicenow" else target
    return f"Azure Health Console - {what} ({FIELD_LABELS.get(field, field)})"


def _from_keychain(target, scope, field):
    store = _keyring()
    if store is None:
        return None
    account, service = _account(target, scope, field), service_name(target, scope, field)
    try:
        value = store.get_password(service, account)
        if value is not None:
            return value
        value = store.get_password(LEGACY_SERVICE, account)
    except Exception:  # a locked or broken keychain reads as "not stored"
        return None
    if value is not None:
        try:  # give it its own name; if the keychain refuses, it keeps working under the old one
            store.set_password(service, account, value)
            store.delete_password(LEGACY_SERVICE, account)
        except Exception:
            pass
    return value


def get(target, scope, field):
    return os.environ.get(env_name(target, field)) or _from_keychain(target, scope, field)


def source(target, scope, field):
    """"environment", "keychain" or None: for the status shown in Settings."""
    if os.environ.get(env_name(target, field)):
        return "environment"
    return "keychain" if _from_keychain(target, scope, field) else None


def put(target, scope, field, value):
    store = _keyring()
    if store is None:
        raise SecretStoreError(f"{keychain()['hint']} Set {env_name(target, field)} instead.")
    account = _account(target, scope, field)
    try:
        store.set_password(service_name(target, scope, field), account, value)
    except Exception as error:
        raise SecretStoreError(f"The keychain refused to store it ({error}). "
                               f"Set {env_name(target, field)} instead.") from error
    try:
        store.delete_password(LEGACY_SERVICE, account)  # an old copy would only confuse
    except Exception:
        pass


def delete(target, scope, field):
    store = _keyring()
    if store is None:
        return
    for service in (service_name(target, scope, field), LEGACY_SERVICE):
        try:
            store.delete_password(service, _account(target, scope, field))
        except Exception:  # already gone
            pass
