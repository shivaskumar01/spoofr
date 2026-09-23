"""Route sessions: where the iPhone should be, as a function of the clock.

A route belongs to the session, not to the connection. The session knows when
the route started, every speed change, every pause and every stop dwell, so at
any moment it can say where the phone should be — whether or not the phone was
reachable for any of it, and across app quits, Mac sleep and Mac restarts.

Pure Python, no Qt, no device stack: RouteSession is the model, Runner is the
thread that pushes its position to the phone about once a second.
"""

from __future__ import annotations

import bisect
import copy
import math
import random
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

EARTH_R = 6_371_000.0
KMH = 1 / 3.6                     # m/s per km/h
SCHEMA = 1                        # bump when the saved JSON changes shape


def meters(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def bearing(a, b) -> float:
    """Initial compass bearing a → b, degrees clockwise from north."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


# ---- the clock -------------------------------------------------------------

def _mono() -> float:
    """Seconds that keep counting while the Mac sleeps.

    time.monotonic() is mach_absolute_time on macOS, which stops during sleep:
    close the lid for an hour mid-route and the route would come back an hour
    behind. CLOCK_MONOTONIC keeps counting through sleep and, unlike the wall
    clock, never jumps when the time zone or the clock itself is changed.
    """
    if sys.platform == "darwin":
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    return time.monotonic()


_BOOT: Optional[str] = None


def _boot_id() -> str:
    """Identifies this boot; CLOCK_MONOTONIC restarts from zero on the next one."""
    global _BOOT
    if _BOOT is None:
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                                 text=True, timeout=3).stdout
            _BOOT = out.split("sec =")[1].split(",")[0].strip() if "sec =" in out else out.strip()
        except Exception:
            _BOOT = "unknown"
    return _BOOT


class Clock:
    """Session time in seconds: counts through Mac sleep, ignores wall-clock and
    time-zone changes, and survives quits and Mac restarts.

    Within one boot it is CLOCK_MONOTONIC. Across a restart the monotonic clock
    starts again from zero, so the time that passed while the Mac was off is
    taken from the wall clock between the last save and now.
    """

    def __init__(self, mono=_mono, boot=_boot_id, wall=time.time):
        self._mono, self._boot, self._wall = mono, boot, wall
        self.boot = boot()
        self.base = mono()
        self.offset = 0.0

    def t(self) -> float:
        return self.offset + (self._mono() - self.base)

    def to_dict(self) -> dict:
        return {"boot": self.boot, "base": self.base, "offset": self.offset,
                "saved_t": self.t(), "saved_wall": self._wall()}

    @classmethod
    def from_dict(cls, d: dict, **kw) -> "Clock":
        c = cls(**kw)
        if d.get("boot") == c.boot:
            c.base, c.offset = d["base"], d["offset"]
        else:                                    # the Mac restarted since
            gone = max(0.0, c._wall() - d.get("saved_wall", c._wall()))
            c.offset = d.get("saved_t", 0.0) + gone
        return c


# ---- geometry ----------------------------------------------------------------

class Plan:
    """A polyline with cumulative distances, and where its stops sit along it."""

    def __init__(self, points, stops_d=()):
        pts = [(float(p[0]), float(p[1])) for p in points]
        # drop consecutive duplicates: a zero-length segment has no direction
        self.points = [p for i, p in enumerate(pts) if i == 0 or meters(pts[i - 1], p) > 0.01]
        if len(self.points) == 1 and len(pts) > 1:
            self.points.append(pts[-1])
        self.cum = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self.cum.append(self.cum[-1] + meters(a, b))
        self.total = self.cum[-1]
        self.stops_d = sorted(d for d in stops_d if 0.0 < d < self.total)

    def at(self, d: float) -> tuple[float, float, float]:
        """(lat, lon, heading) at distance d along the line (clamped)."""
        pts = self.points
        if len(pts) == 1:
            return pts[0][0], pts[0][1], 0.0
        d = min(max(d, 0.0), self.total)
        i = max(1, min(bisect.bisect_right(self.cum, d), len(pts) - 1))
        a, b = pts[i - 1], pts[i]
        seg = self.cum[i] - self.cum[i - 1]
        f = 0.0 if seg <= 0 else (d - self.cum[i - 1]) / seg
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, bearing(a, b))

    def between(self, d0: float, d1: float) -> list:
        """The polyline from distance d0 to d1 (d0 < d1), for drawing."""
        d0, d1 = max(0.0, min(d0, self.total)), max(0.0, min(d1, self.total))
        if d1 <= d0:
            return []
        out = [self.at(d0)[:2]]
        i0 = bisect.bisect_right(self.cum, d0)
        i1 = bisect.bisect_left(self.cum, d1)
        out += [self.points[i] for i in range(i0, i1)]
        out.append(self.at(d1)[:2])
        return out

    def locate(self, pts) -> list[float]:
        """Along-line distances of `pts`, matched in order (for stops on a
        road-snapped line, where the router moved them onto the street)."""
        out, j = [], 0
        for p in pts:
            best, bi = float("inf"), j
            for k in range(j, len(self.points)):
                dd = meters(p, self.points[k])
                if dd < best:
                    best, bi = dd, k
            out.append(self.cum[bi])
            j = bi
        return out

    def to_dict(self) -> dict:
        return {"points": [list(p) for p in self.points], "stops_d": list(self.stops_d)}

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(d["points"], d.get("stops_d", ()))


# ---- the session -------------------------------------------------------------

@dataclass
class Options:
    end: str = "stop"            # "stop" | "loop" | "pingpong"
    laps: Optional[int] = 1      # None = forever (loop / ping-pong only)
    dwell_s: float = 0.0         # pause at each stop
    variation: bool = False      # ±10% speed, so movement isn't robotic
    jitter: bool = False         # a metre or two of GPS noise on each fix


@dataclass
class Progress:
    t: float = 0.0               # session time this progress is valid at
    D: float = 0.0               # motion distance covered, all laps
    dwell_left: float = 0.0
    stop_mark: float = -1.0      # motion distance of the last stop dwelt at
    finished: bool = False
    t_finished: Optional[float] = None
    paused_total: float = 0.0    # time spent paused (and waiting to resume)


class RouteSession:
    """Where the phone should be at any session time.

    Motion distance D runs from 0 to laps × lap length. Speed is piecewise
    (every change is a new segment starting where the phone is, so there is
    never a jump), optionally varied ±10% by a smooth deterministic wobble, and
    time spent paused or dwelling at stops moves nothing.
    """

    def __init__(self, plan: Plan, speed_mps: float, options: Options | None = None,
                 clock: Clock | None = None, name: str = "", seed: int | None = None):
        self.plan = plan
        self.opts = options or Options()
        self.clock = clock or Clock()
        self.name = name
        self.seed = random.randrange(1 << 30) if seed is None else seed
        rnd = random.Random(self.seed)
        self._phase = (rnd.uniform(0, 2 * math.pi), rnd.uniform(0, 2 * math.pi))
        self.segments: list[list[float]] = [[self.clock.t(), max(0.1, float(speed_mps))]]
        self.t_start = self.segments[0][0]
        self.prog = Progress(t=self.t_start)
        self.paused = False
        self.hold = False            # don't push (waiting on a catch-up decision)
        self.delivered: Progress | None = None   # last state the phone actually got
        self.offline_since: Optional[float] = None
        self._build_lap()

    # -- lap geometry --

    def _build_lap(self):
        p = self.plan
        if self.opts.end == "loop" and len(p.points) > 1 and meters(p.points[-1], p.points[0]) > 1.0:
            self.lap = Plan(p.points + [p.points[0]], p.stops_d)   # closing leg home
        else:
            self.lap = p
        L = self.lap.total
        if self.opts.end == "stop":
            self.laps = 1
        else:
            self.laps = self.opts.laps if self.opts.laps and self.opts.laps > 0 else None
        self.motion_total = None if self.laps is None else self.laps * L

    def _reversed(self, lap_i: int) -> bool:
        return self.opts.end == "pingpong" and lap_i % 2 == 1

    def _lap_of(self, D: float) -> tuple[int, float]:
        L = self.lap.total
        if L <= 0:
            return 0, 0.0
        i = int(D // L)
        if self.motion_total is not None and D >= self.motion_total:
            i = self.laps - 1
        return i, D - i * L

    def _next_stop(self, D: float, after: float) -> Optional[float]:
        """Motion distance of the next stop at or after D and strictly after `after`."""
        if not self.plan.stops_d or self.opts.dwell_s <= 0:
            return None
        L = self.lap.total
        lap_i, _ = self._lap_of(D)
        for k in (lap_i, lap_i + 1):
            if self.laps is not None and k >= self.laps:
                break
            ev = [L - s for s in self.plan.stops_d] if self._reversed(k) else self.plan.stops_d
            for e in sorted(ev):
                m = k * L + e
                if m >= D - 1e-9 and m > after + 1e-9:
                    return m
        return None

    # -- speed --

    def base_speed(self, t: float | None = None) -> float:
        t = self.prog.t if t is None else t
        v = self.segments[0][1]
        for t0, s in self.segments:
            if t0 <= t:
                v = s
        return v

    def speed_at(self, t: float) -> float:
        v = self.base_speed(t)
        if self.opts.variation:
            p1, p2 = self._phase
            wob = 0.6 * math.sin(2 * math.pi * t / 41.0 + p1) + 0.4 * math.sin(2 * math.pi * t / 13.0 + p2)
            v *= 1.0 + 0.10 * wob
        return max(0.05, v)

    def set_speed(self, speed_mps: float, t: float | None = None):
        """Change speed from now on. The position carries straight on from where
        it is, so there is no jump; only what happens next changes."""
        t = self.clock.t() if t is None else t
        self.advance(t)
        self.segments.append([t, max(0.1, float(speed_mps))])

    # -- the state machine --

    def advance(self, t: float):
        """Bring the progress forward to session time t."""
        p = self.prog
        if t <= p.t:
            return
        cap = 1.0 if self.opts.variation else math.inf
        while p.t < t - 1e-9 and not p.finished:
            if self.paused:
                p.paused_total += t - p.t
                p.t = t
                break
            if p.dwell_left > 0:
                step = min(p.dwell_left, t - p.t)
                p.dwell_left -= step
                p.t += step
                continue
            nxt = self.motion_total if self.motion_total is not None else math.inf
            stop = self._next_stop(p.D, p.stop_mark)
            if stop is not None and stop < nxt:
                nxt = stop
            v = self.speed_at(p.t)
            step = min(t - p.t, cap, (nxt - p.D) / v if nxt < math.inf else math.inf)
            p.D += v * step
            p.t += step
            if p.D >= nxt - 1e-6:
                p.D = nxt
                if self.motion_total is not None and nxt >= self.motion_total:
                    p.finished = True
                    p.t_finished = p.t
                else:
                    p.stop_mark = nxt
                    p.dwell_left = self.opts.dwell_s
        if p.finished:
            if self.paused:
                p.paused_total += max(0.0, t - p.t)
            p.t = t

    def position(self) -> tuple[float, float, float]:
        """(lat, lon, heading) for the current progress."""
        lap_i, d = self._lap_of(self.prog.D)
        if self._reversed(lap_i):
            lat, lon, h = self.lap.at(self.lap.total - d)
            return lat, lon, (h + 180.0) % 360.0
        return self.lap.at(d)

    def position_now(self):
        self.advance(self.clock.t())
        return self.position()

    # -- pause / resume --

    def pause(self, t: float | None = None):
        t = self.clock.t() if t is None else t
        self.advance(t)
        self.paused = True

    def resume(self, t: float | None = None):
        t = self.clock.t() if t is None else t
        self.advance(t)               # while paused this only books the pause
        self.paused = False

    # -- delivery / offline --

    def mark_delivered(self):
        self.delivered = copy.copy(self.prog)
        self.offline_since = None

    def mark_offline(self):
        if self.offline_since is None:
            self.offline_since = self.prog.t

    def rewind_to_delivered(self, t: float | None = None):
        """'Continue from where it stopped': the time out of reach becomes a
        pause, and the route picks up from the last fix the phone received."""
        t = self.clock.t() if t is None else t
        if self.delivered is not None:
            gap = max(0.0, t - self.delivered.t)
            p = copy.copy(self.delivered)
            p.paused_total += gap
            p.t = t
            if p.finished and p.t_finished is not None:
                p.t_finished += gap
            self.prog = p
        self.offline_since = None

    def skipped(self) -> Optional[tuple[float, float]]:
        """(from, to) motion distances jumped over by a catch-up, if any."""
        if self.delivered is None or self.prog.D - self.delivered.D < 1.0:
            return None
        return self.delivered.D, self.prog.D

    # -- read-outs --

    def fraction(self) -> float:
        if self.motion_total:
            return min(1.0, self.prog.D / self.motion_total)
        L = self.lap.total
        return (self.prog.D % L) / L if L > 0 else 0.0

    def remaining_m(self) -> Optional[float]:
        return None if self.motion_total is None else max(0.0, self.motion_total - self.prog.D)

    def remaining_s(self) -> Optional[float]:
        rem = self.remaining_m()
        if rem is None:
            return None
        v = self.base_speed()
        dwells = 0.0
        if self.opts.dwell_s > 0 and self.plan.stops_d:
            m, mark = self.prog.D, self.prog.stop_mark
            while True:
                s = self._next_stop(m, mark)
                if s is None:
                    break
                dwells += self.opts.dwell_s
                m = mark = s
        return rem / v + dwells + self.prog.dwell_left

    def lap_number(self) -> int:
        return self._lap_of(self.prog.D)[0] + 1

    def summary(self) -> dict:
        p = self.prog
        end = p.t_finished if p.t_finished is not None else p.t
        return {"distance_m": p.D, "duration_s": max(0.0, end - self.t_start - p.paused_total),
                "finished_wall": time.time() - max(0.0, self.clock.t() - end)}

    # -- persistence --

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA, "name": self.name, "seed": self.seed,
            "plan": self.plan.to_dict(), "opts": asdict(self.opts),
            "segments": self.segments, "t_start": self.t_start,
            "prog": asdict(self.prog), "paused": self.paused,
            "delivered": asdict(self.delivered) if self.delivered else None,
            "offline_since": self.offline_since,
            "clock": self.clock.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict, clock_kw: dict | None = None) -> "RouteSession":
        clock = Clock.from_dict(d["clock"], **(clock_kw or {}))
        s = cls.__new__(cls)
        s.plan = Plan.from_dict(d["plan"])
        s.opts = Options(**d.get("opts", {}))
        s.clock = clock
        s.name = d.get("name", "")
        s.seed = d.get("seed", 0)
        rnd = random.Random(s.seed)
        s._phase = (rnd.uniform(0, 2 * math.pi), rnd.uniform(0, 2 * math.pi))
        s.segments = [list(x) for x in d["segments"]]
        s.t_start = d["t_start"]
        s.prog = Progress(**d["prog"])
        s.paused = d.get("paused", False)
        s.hold = False
        s.delivered = Progress(**d["delivered"]) if d.get("delivered") else None
        s.offline_since = d.get("offline_since")
        s._build_lap()
        return s


# ---- the runner ------------------------------------------------------------

def jittered(lat: float, lon: float, radius_m: float = 2.5, rnd=random) -> tuple[float, float]:
    r = radius_m * math.sqrt(rnd.random())
    th = rnd.uniform(0.0, 2.0 * math.pi)
    dlat = r * math.cos(th) / 111_320.0
    dlon = r * math.sin(th) / (111_320.0 * max(0.15, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


@dataclass
class Tick:
    """What the UI needs after each step, copied out of the session."""
    lat: float
    lon: float
    heading: float
    fraction: float
    remaining_m: Optional[float]
    remaining_s: Optional[float]
    speed: float
    lap: int
    delivered: bool
    offline: bool
    paused: bool
    finished: bool
    D: float = 0.0
    came_back: bool = False          # first delivered fix after time out of reach
    skipped: Optional[tuple] = field(default=None)
    done: bool = False               # finished, and the phone has the final fix


class Runner:
    """Pushes the session's position to the phone about once a second.

    `push(lat, lon) -> bool` is the one write path (the bridge). A failed push
    doesn't stop anything: the clock runs on, and the next fix that lands puts
    the phone wherever the route has got to. `lock` guards the session against
    the UI (speed changes, pause) editing it mid-step.
    """

    def __init__(self, session: RouteSession, push: Callable[[float, float], bool],
                 on_tick: Callable[[Tick], None] | None = None,
                 save: Callable[[RouteSession], None] | None = None,
                 interval: float | None = None, save_every: float = 5.0):
        self.session = session
        self.push = push
        self.on_tick = on_tick or (lambda _t: None)
        self.save = save or (lambda _s: None)
        self.interval = interval
        self.save_every = save_every
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sent: tuple | None = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="route-runner", daemon=True)
        self._thread.start()

    def stop(self, join: float = 0.0):
        self._stop.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(join)

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poke(self):
        """Forget what was last sent, so the next tick re-sends (after a reconnect)."""
        self._last_sent = None

    def _period(self) -> float:
        if self.interval is not None:
            return self.interval
        # quicker fixes at driving speeds keep corners and curves smooth
        return 0.5 if self.session.base_speed() > 8.0 else 1.0

    def step(self) -> Tick:
        """One tick: advance, push, report. Public so tests can drive it."""
        s = self.session
        with self.lock:
            s.advance(s.clock.t())
            lat, lon, heading = s.position()
            idle = s.paused or s.prog.finished
            want = (round(lat, 7), round(lon, 7))
            send = not s.hold and not (idle and self._last_sent == want)
            was_offline = s.offline_since is not None
        ok = False
        if send:
            fx, fy = (jittered(lat, lon) if s.opts.jitter and not idle else (lat, lon))
            ok = bool(self.push(fx, fy))
        with self.lock:
            skipped = None
            if send and ok:
                skipped = s.skipped() if was_offline else None
                s.mark_delivered()
                self._last_sent = want
            elif send or s.hold:
                s.mark_offline()
            return Tick(lat, lon, heading, s.fraction(), s.remaining_m(), s.remaining_s(),
                        s.base_speed(), s.lap_number(),
                        delivered=bool(send and ok), offline=s.offline_since is not None,
                        paused=s.paused, finished=s.prog.finished, D=s.prog.D,
                        came_back=bool(was_offline and send and ok), skipped=skipped,
                        done=s.prog.finished and self._last_sent == want)

    def _loop(self):
        last_save = 0.0
        while not self._stop.is_set():
            tick = self.step()
            self.on_tick(tick)
            now = time.monotonic()
            if now - last_save >= self.save_every or tick.came_back:
                last_save = now
                with self.lock:
                    self.save(self.session)
            if tick.done:
                with self.lock:
                    self.save(self.session)
                return
            self._stop.wait(self._period())
