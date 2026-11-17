import os
import stat

import pytest

from healthconsole.core import localfiles, settings_file
from healthconsole.core.watchlist import Watchlist


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_settings_and_their_backup_are_private_whatever_the_umask(tmp_path):
    old = os.umask(0o022)
    try:
        config = tmp_path / "config.yaml"
        settings_file.apply(config, values={"health.critical_max_severity": 1})
        settings_file.apply(config, values={"health.critical_max_severity": 2})
    finally:
        os.umask(old)
    assert mode(config) == 0o600 and mode(tmp_path / "config.yaml.bak") == 0o600


def test_an_existing_readable_file_is_made_private_when_written(tmp_path):
    path = tmp_path / "w.yaml"
    path.write_text("old")
    os.chmod(path, 0o644)
    localfiles.write_private_text(path, "new")
    assert path.read_text() == "new" and mode(path) == 0o600


def test_a_symlink_or_folder_is_never_written_through(tmp_path):
    target = tmp_path / "precious.txt"
    target.write_text("items: []\n")
    link = tmp_path / "watchlist.yaml"
    link.symlink_to(target)
    with pytest.raises(OSError, match="isn't a regular file"):
        Watchlist(str(link)).add("/subscriptions/s/resourceGroups/rg", "rg", "group", "t")
    with pytest.raises(OSError):
        localfiles.write_private_text(tmp_path, "x")
    assert target.read_text() == "items: []\n"  # untouched


def test_a_shared_cache_folder_loses_write_access_for_others_only(tmp_path):
    shared = tmp_path / "cache"
    shared.mkdir()
    os.chmod(shared, 0o777)
    localfiles.private_dir(shared)
    assert mode(shared) == 0o755  # others can no longer replace files in it; nothing else changed
    new = localfiles.private_dir(tmp_path / "fresh" / "cache")
    assert mode(new) & 0o077 == 0
