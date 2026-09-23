"""The root tunnel daemon acts for the logged-in user, the way sudo would."""

from __future__ import annotations

import os
import pwd
import sys


def test_as_user_sets_the_sudo_variables(monkeypatch):
    import spoofr_app
    for k in ("SUDO_USER", "SUDO_UID", "SUDO_GID"):
        monkeypatch.delenv(k, raising=False)
    me = pwd.getpwuid(os.getuid())
    spoofr_app._act_for(me.pw_name)
    assert os.environ["SUDO_USER"] == me.pw_name
    assert os.environ["SUDO_UID"] == str(me.pw_uid)
    assert os.environ["SUDO_GID"] == str(me.pw_gid)
    # which is what pymobiledevice3 uses to pick the home folder under root
    from pymobiledevice3.osu.posix_util import Darwin
    assert str(Darwin().get_homedir()) == me.pw_dir


def test_the_flag_is_ours_and_never_reaches_pymobiledevice3(monkeypatch):
    import spoofr_app
    seen = {}
    import pymobiledevice3.__main__ as pmd
    monkeypatch.setattr(pmd, "main", lambda: seen.setdefault("argv", list(sys.argv)))
    monkeypatch.setattr(spoofr_app, "_act_for", lambda user: seen.setdefault("user", user))
    monkeypatch.setattr(sys, "argv", ["Spoofr", "--tunneld", "--as-user", "alice",
                                      "--protocol", "tcp"])
    spoofr_app.main()
    assert seen["user"] == "alice"
    assert seen["argv"] == ["pymobiledevice3", "remote", "tunneld", "--protocol", "tcp"]
