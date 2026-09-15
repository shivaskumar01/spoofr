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

    def test_no_kill_when_nothing_is_running(self):
        import portable
        assert "kill" not in portable.tunnel_start_cmd([])

    def test_stale_daemons_are_killed_hard_and_all_of_them(self):
        """SIGTERM is not enough: uvicorn tries a graceful shutdown that never
        finishes while its tunnel tasks are stuck retrying, so a TERMed daemon was
        still holding the port half an hour later with two replacements idling
        behind it."""
        import portable
        cmd = portable.tunnel_start_cmd([69863, 72448, 72526])
        assert "kill -9 69863 72448 72526" in cmd, cmd
        assert cmd.index("kill -9") < cmd.index("--protocol tcp"), "must die before we start"
        assert "pkill" not in cmd, "pkill -f would match the shell running this command"

    def test_more_than_one_daemon_is_never_healthy(self, monkeypatch):
        """Whoever is answering is the oldest, not the one we asked for."""
        import portable
        two = ("  100 /x/Spoofr --tunneld --protocol tcp\n"
               "  200 /x/Spoofr --tunneld --protocol tcp\n")
        monkeypatch.setattr(portable, "_ps_lines", lambda: two.splitlines())
        assert portable.tunnel_is_healthy() is False

    def test_a_single_current_daemon_is_left_alone(self, monkeypatch):
        import portable
        monkeypatch.setattr(portable, "_ps_lines",
                            lambda: ["  700 /x/Spoofr --tunneld --protocol tcp"])
        assert portable.tunnel_is_healthy() is True

    def test_a_daemon_without_the_flag_is_not_healthy(self, monkeypatch):
        import portable
        monkeypatch.setattr(portable, "_ps_lines", lambda: ["  700 /x/Spoofr --tunneld"])
        assert portable.tunnel_is_healthy() is False
        assert portable.tunneld_pids() == [700]


class TestWhatCountsAsATunnelDaemon:
    """These pids are SIGKILLed as root, so the match has to be exact.

    A substring test matches any process whose arguments merely mention
    --tunneld. While this was being written that included the shell evaluating a
    command containing the word, and a `python -c` whose inline source quoted an
    example command. Both would have been killed.
    """

    @pytest.mark.parametrize("cmd", [
        "/Users/x/dist/Spoofr.app/Contents/MacOS/Spoofr --tunneld",
        "/Users/x/dist/Spoofr.app/Contents/MacOS/Spoofr --tunneld --protocol tcp",
        "/x/.venv/bin/python /x/spoofr_app.py --tunneld --protocol tcp",
        "/x/.venv/bin/python3.11 /x/spoofr_app.py --tunneld",
    ])
    def test_our_daemons_are_matched(self, cmd):
        import portable
        assert portable.is_tunneld_cmd(cmd) is True

    @pytest.mark.parametrize("cmd", [
        "/bin/zsh -c source /x/snap.sh && echo --tunneld",
        "/bin/sh -c ( /x/Spoofr --tunneld --protocol tcp & )",
        "/x/.venv/bin/python -c import portable; '/x/spoofr_app.py --tunneld'",
        "/usr/bin/grep -- --tunneld /tmp/spoofr-tunneld.log",
        "/usr/bin/tail -f /tmp/spoofr-tunneld.log",
        "/x/Spoofr --tunnelder",
        "/x/Spoofr",
        "/x/.venv/bin/python /x/server.py --server 8765",
        "",
    ])
    def test_everything_else_survives(self, cmd):
        import portable
        assert portable.is_tunneld_cmd(cmd) is False


class TestTunnelDiagnostics:
    def test_routine_polling_is_filtered_out_of_the_error(self, tmp_path, monkeypatch):
        """The dialog quoted three "GET /" lines and nothing about the failure."""
        import core
        log = tmp_path / "tunneld.log"
        log.write_text(
            "2026-01-01 pymobiledevice3.tunneld WARNING QuicProtocolNotSupportedError: boom\n"
            + 'INFO:     127.0.0.1:50354 - "GET / HTTP/1.1" 200 OK\n' * 40)
        monkeypatch.setattr(core, "TUNNELD_LOG", log)
        out = core._tunneld_tail(4)
        assert "QuicProtocolNotSupportedError" in out
        assert "GET /" not in out

    def test_a_quiet_log_says_so_instead_of_looking_empty(self, tmp_path, monkeypatch):
        import core
        log = tmp_path / "tunneld.log"
        log.write_text('INFO:     127.0.0.1:1 - "GET / HTTP/1.1" 200 OK\n')
        monkeypatch.setattr(core, "TUNNELD_LOG", log)
        assert "routine polling" in core._tunneld_tail(4)

    def test_a_missing_log_does_not_raise(self, tmp_path, monkeypatch):
        import core
        monkeypatch.setattr(core, "TUNNELD_LOG", tmp_path / "nope.log")
        assert "not written a log" in core._tunneld_tail(4)
