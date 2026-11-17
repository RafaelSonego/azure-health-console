"""Read-only Azure health console."""
from pathlib import Path


def _version():
    """The repo's VERSION when running from a checkout (an editable install's metadata goes stale), else the
    installed package's."""
    source = Path(__file__).resolve().parent.parent / "VERSION"
    if source.is_file():
        return source.read_text().strip()
    try:
        from importlib.metadata import version
        return version("azure-health-console")
    except Exception:  # e.g. a frozen executable without metadata
        return "unknown"


__version__ = _version()
