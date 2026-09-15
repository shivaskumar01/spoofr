"""The phone-control server's session state.

status() used to report `connected: true` forever after one successful connect,
so the phone drove a session that had been dead for half an hour, and /connect
had no guard, so the page's retry timer stacked full connects behind each other.
"""

from __future__ import annotations

import threading
import time

import pytest

import core
import server
from tests.test_bridge import FakeDevice


@pytest.fixture
def state(monkeypatch):
    st = server.State()
    monkeypatch.setattr(st, "_start_monitor", lambda: None)   # no background polling
    return st


def connect_with(state, monkeypatch, device, home=(1.0, 2.0)):
    monkeypatch.setattr(core, "connect", lambda *a, **k: device)
    monkeypatch.setattr(server.geo, "current_location", lambda *a, **k: home)
    return state.connect()


def test_status_before_any_connect(state):
    st = state.status()
    assert st["connected"] is False and st["live"] is None and st["lost"] is False


def test_connect_reports_the_link(state, monkeypatch):
    r = connect_with(state, monkeypatch, FakeDevice())
    assert r == {"name": "iPhone", "ios": "18.0", "link": "USB"}
    assert state.status()["connected"] is True


def test_a_second_connect_reuses_the_open_session(state, monkeypatch):
    calls = []
    dev = FakeDevice()

    def fake_connect(*a, **k):
        calls.append(1)
        return dev

    monkeypatch.setattr(core, "connect", fake_connect)
    monkeypatch.setattr(server.geo, "current_location", lambda *a, **k: (1.0, 2.0))
    state.connect()
    state.connect()
    assert len(calls) == 1, "reconnected on top of a live session"


def test_a_connect_already_in_progress_is_reported_not_repeated(state, monkeypatch):
    """The page retries /connect on a timer; core.connect can take a minute."""
    started, release = threading.Event(), threading.Event()

    def slow_connect(*a, **k):
        started.set()
        release.wait(5)
        return FakeDevice()

    monkeypatch.setattr(core, "connect", slow_connect)
    monkeypatch.setattr(server.geo, "current_location", lambda *a, **k: (1.0, 2.0))
    t = threading.Thread(target=state.connect, daemon=True)
    t.start()
    started.wait(2)
    assert state.connect() == {"connecting": True}
    release.set()
    t.join(5)


def test_push_marks_the_session_lost_and_starts_one_reconnect(state, monkeypatch):
    connect_with(state, monkeypatch, FakeDevice(error=RuntimeError("Connection closed")))
    monkeypatch.setattr(server.State, "_reconnect", lambda self, gen: None)
    assert state.push(3.0, 4.0) is False
    st = state.status()
    assert st["connected"] is False and st["lost"] is True and st["link"] == ""


def test_push_records_what_the_phone_is_actually_set_to(state, monkeypatch):
    connect_with(state, monkeypatch, FakeDevice())
    assert state.spoof is None, "nothing is spoofed just because we connected"
    state.push(9.0, 8.0)
    assert state.spoof == (9.0, 8.0) and state.live == (9.0, 8.0)


def test_restore_forgets_the_spoof_so_the_heartbeat_cannot_re_apply_it(state, monkeypatch):
    dev = FakeDevice()
    connect_with(state, monkeypatch, dev, home=(5.0, 6.0))
    state.push(9.0, 8.0)
    state.restore()
    assert state.spoof is None
    assert state.live == (5.0, 6.0)


def test_route_stops_when_a_fix_does_not_land(state, monkeypatch):
    dev = FakeDevice()
    connect_with(state, monkeypatch, dev)
    monkeypatch.setattr(server.State, "_reconnect", lambda self, gen: None)
    state.start_route([(0.0, 0.0), (0.0, 0.05)], speed=200.0)
    time.sleep(0.2)
    dev.error = RuntimeError("Channel is closed")      # the tunnel dies mid-route
    state.route_thread.join(4)
    assert not state.route_thread.is_alive(), "route kept writing to a dead session"
    assert state.status()["connected"] is False


def test_shutdown_leaves_the_spoof_on_the_phone(state, monkeypatch):
    dev = FakeDevice()
    connect_with(state, monkeypatch, dev)
    state.push(1.0, 2.0)
    state.shutdown()
    assert state.device is None
    assert dev.closed_with_clear is False


class TestTunnelElevation:
    """How the app asks for root once and hands the tunnel to launchd."""

    def test_command_detaches_without_nohup(self):
        """nohup dies under `do shell script ... with administrator privileges`
        ("can't detach from console: Inappropriate ioctl for device") and never
        execs the command, so the password is accepted and the tunnel still
        never comes up. It works fine unprivileged, which hid the bug."""
        import portable
        sh = portable.detached_cmd(["/bin/echo", "hi"], "/tmp/x.log")
        assert "nohup" not in sh
        assert sh.startswith("(") and sh.endswith(")")       # detaching subshell
        assert "> /tmp/x.log" in sh and "2>&1" in sh and "< /dev/null" in sh
        assert sh.rstrip(") ").endswith("&")                 # backgrounded

    def test_arguments_with_spaces_survive_quoting(self):
        import portable
        sh = portable.detached_cmd(["/Applications/My App/bin/py", "--tunneld"], "/tmp/x.log")
        assert "'/Applications/My App/bin/py'" in sh

    def test_helper_points_at_the_ui_free_entry_point(self):
        """The root tunnel helper must not boot a GUI toolkit."""
        import portable
        argv = portable._helper_cmd("--tunneld")
        assert argv[-1] == "--tunneld"
        assert "gui.py" not in " ".join(argv)
        assert "spoofr_app.py" in " ".join(argv)


class TestTunnelProtocol:
    """iOS 18.2+ dropped QUIC, and pymobiledevice3 only defaults to TCP on Python
    3.13+ (remote/common.py). On 3.11 the daemon tries QUIC, fails every
    handshake, and publishes no tunnel — while still holding its port, so the app
    attaches happily and then reports that the phone must not be trusted."""

    def test_the_daemon_is_asked_for_tcp(self):
        import portable
        argv = portable._helper_cmd("--tunneld", "--protocol", portable.TUNNEL_PROTOCOL)
        assert argv[-3:] == ["--tunneld", "--protocol", "tcp"]
        assert "--protocol tcp" in portable.tunnel_start_cmd()

    def test_a_daemon_that_cannot_tunnel_is_retired_not_reused(self):
        import portable
        cmd = portable.tunnel_start_cmd(4242)
        assert cmd.startswith("kill 4242 ;"), cmd
        assert "--protocol tcp" in cmd
        assert "pkill" not in cmd, "pkill -f would match the shell running this command"

    def test_no_kill_when_nothing_is_running(self):
        import portable
        assert "kill" not in portable.tunnel_start_cmd(None)

    def test_detects_a_root_daemon_it_cannot_introspect(self, monkeypatch):
        """The daemon runs as root; psutil can't read its command line from a
        normal user, so detection goes through ps."""
        import portable

        class Result:
            stdout = ("  501 /usr/bin/something else\n"
                      " 69863 /Applications/Spoofr.app/Contents/MacOS/Spoofr --tunneld\n")

        monkeypatch.setattr(portable.subprocess, "run", lambda *a, **k: Result())
        assert portable.running_tunneld() == (
            69863, "/Applications/Spoofr.app/Contents/MacOS/Spoofr --tunneld")
        assert portable.tunneld_pid() == 69863
        assert portable.tunnel_speaks_tcp() is False      # no --protocol tcp -> retire it

    def test_a_current_daemon_is_left_alone(self, monkeypatch):
        import portable

        class Result:
            stdout = " 700 /Applications/Spoofr.app/Contents/MacOS/Spoofr --tunneld --protocol tcp\n"

        monkeypatch.setattr(portable.subprocess, "run", lambda *a, **k: Result())
        assert portable.tunnel_speaks_tcp() is True

    def test_no_daemon_reads_as_not_current(self, monkeypatch):
        import portable

        class Result:
            stdout = " 700 /usr/sbin/cupsd\n"

        monkeypatch.setattr(portable.subprocess, "run", lambda *a, **k: Result())
        assert portable.running_tunneld() is None
        assert portable.tunnel_speaks_tcp() is False
