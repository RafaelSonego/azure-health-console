"""The console's own files on disk (settings, the watchlist, the cost store, summaries): readable and writable only by
the user who runs it, whatever their umask."""
import os
import stat
from pathlib import Path


def private_dir(path):
    """A folder for the console's own data: created 0700; an existing one others can write to loses that write access
    (they could replace a file in it). A folder you chose keeps everything else as it is."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IWGRP | stat.S_IWOTH) and (not hasattr(os, "getuid") or path.stat().st_uid == os.getuid()):
            os.chmod(path, stat.S_IMODE(mode) & ~(stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass  # not ours, or a filesystem without modes: left as it is
    return path


def write_private_text(path, text):
    """Writes `text` to `path` as 0600 (new parent folders 0700). Refuses a symlink or anything that isn't a regular
    file, so a setting pointing the file somewhere else can't make the console overwrite it."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError(f"{path} isn't a regular file, so it wasn't written.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(path, 0o600)  # an existing file keeps its old mode through O_CREAT; make it private too
