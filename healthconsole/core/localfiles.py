"""The console's own files on disk (settings, the watchlist, the cost store, summaries, the launch key): readable and
writable only by the user who runs it, whatever their umask, and trusted only when no other account can change them.
On Windows, per-user folders and their ACLs do this; the ownership checks below are POSIX only."""
import os
import stat
from pathlib import Path

MAX_TRUSTED_BYTES = 1024 * 1024  # settings, profiles, the watchlist: a bigger file is refused, not parsed


class UnsafePath(OSError):
    """Another account could change this file or folder, so the console won't trust or write it. Says how to fix it."""


def _mine():
    return os.getuid() if hasattr(os, "getuid") else None


def _others_can_change(st, sticky_ok=False):
    """Why an account other than you (or root) could change this entry, or None."""
    me = _mine()
    if me is None:
        return None
    if st.st_uid not in (me, 0):
        return "belongs to another user"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (sticky_ok and st.st_uid == 0 and st.st_mode & stat.S_ISVTX):
        return "can be changed by other users"
    return None


def _tighten(path, st):
    """Yours and writable by others: their write access goes. True when it's safe afterwards."""
    if _mine() is None or st.st_uid != _mine():
        return False
    try:
        os.chmod(path, stat.S_IMODE(st.st_mode) & ~(stat.S_IWGRP | stat.S_IWOTH))
        return True
    except OSError:
        return False


def _refuse(path, reason, what):
    fix = f"chmod go-w '{path}'" if reason.startswith("can") else "move it to a folder of your own"
    raise UnsafePath(f"{what}: {path} {reason}, so the console won't use it (it could be swapped for another one). "
                     f"Fix: {fix}, or point the setting somewhere only you can write.")


def check_folder(path, what="Folder"):
    """`path` (an existing folder) and every folder above it: changeable only by you or root. The folder itself, if
    yours, is tightened; a folder above it is only checked (root's sticky /tmp is fine). Raises UnsafePath."""
    path = Path(path).resolve()
    for index, folder in enumerate([path, *path.parents]):
        st = folder.stat()
        reason = _others_can_change(st, sticky_ok=index > 0)
        if reason and not (index == 0 and _tighten(folder, st)):
            _refuse(folder, reason, what)
    return path


def check_file(path, what="File"):
    """An existing file the console will trust (read or replace): a regular file changeable only by you or root, in a
    folder only you can change. A symlink is followed to its target, which must meet the same rules."""
    target = Path(path).resolve()
    check_folder(target.parent, what)
    st = target.stat()
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePath(f"{what}: {target} isn't a regular file, so the console won't use it.")
    reason = _others_can_change(st)
    if reason and not _tighten(target, st):
        _refuse(target, reason, what)
    return target


def read_trusted(path, what="File", limit=None):
    """The bytes of a file that steers the console (settings, a profile, the watchlist), after `check_file`; refuses
    one over `limit` (default MAX_TRUSTED_BYTES) bytes before reading it."""
    limit = limit or MAX_TRUSTED_BYTES
    target = check_file(path, what)
    size = target.stat().st_size
    if size > limit:
        raise UnsafePath(f"{what}: {target} is {size:,} bytes, over the {limit:,} the console reads; it wasn't loaded.")
    return target.read_bytes()


def read_private_text(path):
    """A secret file of the console's own (the launch key): never through a symlink, and only a regular file of yours
    that nobody else can read or write. Raises OSError otherwise, so the caller makes a new one."""
    handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(handle, "rb") as source:
        st = os.fstat(source.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > 4096:
            raise UnsafePath(f"{path} isn't a small regular file.")
        if _mine() is not None and (st.st_uid != _mine() or st.st_mode & 0o077):
            raise UnsafePath(f"{path} isn't private to you.")
        return source.read().decode("utf-8")


def private_dir(path):
    """A folder for the console's own data: created 0700; an existing one of yours that others can write to loses
    that write access (they could replace a file in it). One that isn't yours and others can change is refused
    (UnsafePath). A folder you chose keeps everything else as it is."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    check_folder(path, "Data folder")
    return path


def write_private_text(path, text):
    """Writes `text` to `path` as 0600 (new parent folders 0700). Refuses a symlink or anything that isn't a regular
    file, so a setting pointing the file somewhere else can't make the console overwrite it. The folder must be one
    only you can change (`private_dir`): the console reads these files back."""
    write_private_bytes(path, text.encode("utf-8"), own_folder=True)


def write_private_bytes(path, content, own_folder=False):
    """`write_private_text` for bytes. Reports (the default) go to the folder you picked as it is; `own_folder` is for
    files the console reads back, whose folder must be `private_dir`'s."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError(f"{path} isn't a regular file, so it wasn't written.")
    if own_folder:
        private_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(handle, "wb") as out:
        out.write(content)
    os.chmod(path, 0o600)  # an existing file keeps its old mode through O_CREAT; make it private too
