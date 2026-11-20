"""A stand-in for Pilar 3.3-3r21 (IRIS) for tests: TPL2 over TCP with the behaviour measured on the instrument
18-19.11.2026, in short times. Only what the Pilar connector uses is emulated:

* greeting ``TPL2 2.1 ...`` + ``AUTH OK``; at most ``max_clients`` clients, the next gets TOO MANY CLIENTS;
* one connection answers its commands in order; multi-GET with ``;``; ``X=UNKNOWN`` for a GET of an unknown
  variable, ``DATA ERROR X UNKNOWN`` for a SET of one, ``FAILED`` + ``EVENT WARN`` for a SET while the telescope
  is off;
* mount: a new target while moving is taken after ``react`` s (so a redirect overshoots and comes back);
  TRACK=2 starts after ``start_altaz`` s, TRACK=1 after ``start_track`` s; TELESCOPE.STOP does nothing;
  TRACK=0 stops established tracking only;
* freeze (:meth:`freeze`): no answer for the given time, motion goes on; at the end TRACK drops to 0 (tracking
  stops) and for ``reconnect`` s READY_STATE=0 and positions FAILED;
* focuser: SYNCMODE bit 16 switches its motor off on TRACK 1/2; it moves only on TRACK=4 with SYNCMODE bit 1;
* derotator: only DEROTATOR[2]; TARGETPOS answers when the move ends; field rotation while tracking with
  SYNCMODE 2/3; SYNCMODE 3 + TRACK=1 applies the change of OFFSET after the TRACK=1 start delay;
* covers: TARGETPOS answers when the move ends; READY=1 opens them, READY=0 parks, closes them and powers off.
"""
import asyncio
import math
import time
from typing import List, Optional, Set

LATITUDE = -24.598
D2 = 'POSITION.INSTRUMENTAL.DEROTATOR[2].'
FOCUS = 'POSITION.INSTRUMENTAL.FOCUS.'
PARK = (273.24, 5.28)


def hadec_to_azalt(ha_h: float, dec: float, lat: float = LATITUDE):
    h, d, p = math.radians(ha_h * 15.0), math.radians(dec), math.radians(lat)
    alt = math.asin(math.sin(p) * math.sin(d) + math.cos(p) * math.cos(d) * math.cos(h))
    x = -math.sin(h) * math.cos(d)
    y = math.sin(d) * math.cos(p) - math.cos(d) * math.cos(h) * math.sin(p)
    return math.degrees(math.atan2(x, y)) % 360.0, math.degrees(alt)


def azalt_to_hadec(az: float, alt: float, lat: float = LATITUDE):
    a, e, p = math.radians(az), math.radians(alt), math.radians(lat)
    dec = math.asin(math.sin(e) * math.sin(p) + math.cos(e) * math.cos(p) * math.cos(a))
    x = -math.sin(a) * math.cos(e)
    y = math.sin(e) * math.cos(p) - math.cos(e) * math.cos(a) * math.sin(p)
    return (math.degrees(math.atan2(x, y)) / 15.0) % 24.0, math.degrees(dec)


class FakePilar:

    def __init__(self, *, ready: bool = True):
        # timing (short for tests)
        self.vmax = 40.0            # [deg/s]
        self.acc = 80.0             # [deg/s^2]
        self.react = 0.15           # [s] new target while moving
        self.start_altaz = 0.05     # [s]
        self.start_track = 0.3      # [s]
        self.ready_time = 0.2       # [s] READY=1
        self.reconnect = 0.3        # [s] after a freeze: READY_STATE 0, positions FAILED
        self.cover_speed = 5.0      # [1/s]
        self.focus_speed = 2.0      # [mm/s]
        self.derot_speed = 60.0     # [deg/s]
        self.field_rotation = 0.5   # [deg/s] while tracking
        self.max_clients = 10
        self.lst0 = 20.1
        self.t0 = time.monotonic()

        self.received: List[str] = []           # every command body, in order
        self.clients = 0
        self.server: Optional[asyncio.base_events.Server] = None
        self.address = ''
        self.awake = asyncio.Event()
        self.awake.set()
        self.link_down = False
        self.freezes = 0
        self.writers: Set[asyncio.StreamWriter] = set()
        self.tasks: Set[asyncio.Task] = set()

        self.powered = ready
        self.ready_frac = 1.0 if ready else 0.0
        self.goal = None                        # (az, alt) the mount drives to
        self.pending = None                     # (time, goal) taken after a delay
        self.vel = [0.0, 0.0]
        self.track = 0                          # 0, 1 (tracking) or 2 (az/alt)
        self.eq = None                          # (ra, dec) tracked
        self.derot_goal = None
        self.derot_pending = None               # (time, change) of the derotator after TRACK=1 with SYNCMODE 3
        self.focus_goal = None
        self.cover_goal = None
        self.applied_offset = 0.0
        self.S = {
            'TELESCOPE.STATUS.GLOBAL': 4,
            'TELESCOPE.STATUS.LIST': 'DRIVES|0::,SYSTEM|4:CABINET|4:ERR_USV_BatteryAlarm|USV\\\\| Alarm|4|CABINET',
            'TELESCOPE.STATUS.ERROR_LIST': 'ERR_USV_BatteryAlarm [W] (CABINET) USV\\\\| Alarm',
            'TELESCOPE.MOTION_STATE': 0,
            'POSITION.LOCAL.SIDEREAL_TIME': self.lst0,
            'POSITION.HORIZONTAL.AZ': 180.0, 'POSITION.HORIZONTAL.ALT': 45.0,
            'POINTING.TRACK': 0, 'OBJECT.TYPE': 'HORIZONTAL',
            'OBJECT.EQUATORIAL.RA': 0.0, 'OBJECT.EQUATORIAL.DEC': 0.0, 'OBJECT.EQUATORIAL.EPOCH': 2000.0,
            'OBJECT.EQUATORIAL.EQUINOX': 2000.0, 'OBJECT.EQUATORIAL.RA_PM': 0.0, 'OBJECT.EQUATORIAL.DEC_PM': 0.0,
            'OBJECT.HORIZONTAL.AZ': 0.0, 'OBJECT.HORIZONTAL.ALT': 0.0,
            'POSITION.INSTRUMENTAL.ZD.REALPOS!MAX': 85.0, 'POSITION.INSTRUMENTAL.ZD.REALPOS!MIN': -85.0,
            FOCUS + 'REALPOS': 27.0, FOCUS + 'TARGETPOS': 27.0, FOCUS + 'POWER_STATE': 1.0,
            FOCUS + 'REALPOS!MAX': 50.0, FOCUS + 'REALPOS!MIN': -0.2,
            'POINTING.SETUP.FOCUS.POSITION': 27.0, 'POINTING.SETUP.FOCUS.SYNCMODE': 16,
            D2 + 'REALPOS': 10.0, D2 + 'TARGETPOS': 10.0, D2 + 'REALPOS!MIN': -272.0, D2 + 'REALPOS!MAX': 272.0,
            'POINTING.SETUP.DEROTATOR.SYNCMODE': 2, 'POINTING.SETUP.DEROTATOR.OFFSET': 0.0,
            'AUXILIARY.COVER.REALPOS': 1.0 if ready else 0.0, 'AUXILIARY.COVER.TARGETPOS': 1.0 if ready else 0.0,
        }
        self.strings = {'TELESCOPE.STATUS.LIST', 'TELESCOPE.STATUS.ERROR_LIST', 'OBJECT.TYPE'}

    # ------------------------------------------------------------------ control

    @classmethod
    async def start(cls, **kwargs) -> 'FakePilar':
        fake = cls(**kwargs)
        fake.server = await asyncio.start_server(fake._handle, '127.0.0.1', 0)
        fake.address = '127.0.0.1:%d' % fake.server.sockets[0].getsockname()[1]
        fake._spawn(fake._physics())
        return fake

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def close(self):
        """Stop serving and wait until every connection and task of the fake is closed."""
        self.awake.set()
        if self.server is not None:
            self.server.close()
        writers = list(self.writers)
        for writer in writers:
            writer.close()
        for writer in writers:
            try:
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except Exception:
                pass
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server is not None:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 1.0)
            except Exception:
                pass

    def freeze(self, seconds: float, stop_tracking: bool = True):
        """Answer nothing for ``seconds``; then TRACK=0 and for ``reconnect`` s READY_STATE 0 / FAILED."""
        async def run():
            self.awake.clear()
            await asyncio.sleep(seconds)
            self.freezes += 1
            if stop_tracking and self.track == 1:
                self.track = 0
                self.eq = None
                self.goal = None
                self.pending = None
            self.S['POINTING.TRACK'] = 0
            self.link_down = True
            self.awake.set()
            await asyncio.sleep(self.reconnect)
            self.link_down = False
        return self._spawn(run())

    def count(self, prefix: str) -> int:
        return sum(1 for body in self.received if body.startswith(prefix))

    @property
    def az(self):
        return self.S['POSITION.HORIZONTAL.AZ']

    @property
    def alt(self):
        return self.S['POSITION.HORIZONTAL.ALT']

    def moving(self) -> bool:
        return math.hypot(*self.vel) > 0.05 or self.pending is not None

    # ------------------------------------------------------------------ values

    def lst(self) -> float:
        return (self.lst0 + (time.monotonic() - self.t0) * 1.0027379 / 3600.0) % 24.0

    def value(self, name: str) -> str:
        base = name.split('!')[0]
        if base.startswith('POSITION.INSTRUMENTAL.DEROTATOR['):
            idx = int(base.split('[')[1].split(']')[0])
            if idx > 2:
                return 'DIMENSION'
            if idx != 2:
                return 'NULL' if '!' in name else 'FAILED'
        if name == 'TELESCOPE.READY_STATE':
            return '0.0' if self.link_down else repr(round(self.ready_frac, 1))
        if name == 'TELESCOPE.READY':
            return 'FAILED'
        if name == 'POINTING.TRACK':
            return str(self.S['POINTING.TRACK'])
        if name in ('POSITION.EQUATORIAL.RA_J2000', 'POSITION.EQUATORIAL.DEC_J2000'):
            if self.link_down:
                return 'FAILED'
            ha, dec = azalt_to_hadec(self.az, self.alt)
            return repr((self.lst() - ha) % 24.0) if name.endswith('RA_J2000') else repr(dec)
        if name == 'POSITION.LOCAL.SIDEREAL_TIME':
            return repr(self.lst())
        if self.link_down and name.startswith(('POSITION.HORIZONTAL.', 'POSITION.INSTRUMENTAL.', 'AUXILIARY.')) \
                and '!' not in name:
            return 'FAILED'
        if name.startswith('AUXILIARY.COVER') and not self.powered:
            return 'FAILED'
        if name in ('POSITION.INSTRUMENTAL.AZ.POWER_STATE', 'POSITION.INSTRUMENTAL.ZD.POWER_STATE'):
            return '1.0' if self.powered else '0.0'
        if name in self.S:
            v = self.S[name]
            return '"%s"' % v if name in self.strings else repr(v)
        if any(known.startswith(name + '.') for known in self.S):
            return 'INVALID'                           # not a leaf, e.g. POSITION.HORIZONTAL
        return 'UNKNOWN'

    # ------------------------------------------------------------------ physics

    def _set_goal(self, goal, delay):
        self.pending = (time.monotonic() + (self.react if self.moving() else delay), goal)

    async def _physics(self):
        dt = 0.01
        while True:
            await asyncio.sleep(dt)
            now = time.monotonic()
            if self.track == 1 and self.eq is not None and self.pending is None and self.goal is not None:
                self.goal = hadec_to_azalt(self.lst() - self.eq[0], self.eq[1])
            if self.pending is not None and now >= self.pending[0]:
                self.goal = self.pending[1]
                self.pending = None
            az, alt = self.az, self.alt
            vx, vy = self.vel
            tx = ty = 0.0
            g = self.goal
            if g is not None:
                dx, dy = ((g[0] - az + 180) % 360) - 180, g[1] - alt
                dist = math.hypot(dx, dy)
                if dist > 1e-4:
                    sp = min(self.vmax, math.sqrt(2 * self.acc * dist) * 0.9)
                    tx, ty = dx / dist * sp, dy / dist * sp
            ex, ey = tx - vx, ty - vy
            e = math.hypot(ex, ey)
            if e > self.acc * dt:
                ex, ey = ex / e * self.acc * dt, ey / e * self.acc * dt
            vx, vy = vx + ex, vy + ey
            if g is not None and math.hypot(((g[0] - az + 180) % 360) - 180, g[1] - alt) < 0.003 \
                    and math.hypot(vx, vy) < 0.5:
                vx = vy = 0.0
                self.S['POSITION.HORIZONTAL.AZ'], self.S['POSITION.HORIZONTAL.ALT'] = g[0] % 360, g[1]
                if self.track != 1:
                    self.goal = None
            else:
                self.S['POSITION.HORIZONTAL.AZ'] = (az + vx * dt) % 360
                self.S['POSITION.HORIZONTAL.ALT'] = alt + vy * dt
            self.vel = [vx, vy]
            self.S['TELESCOPE.MOTION_STATE'] = 1 if self.moving() else 0
            # derotator: field rotation while tracking, moves to a goal (in tracking after the TRACK=1 delay)
            if self.derot_pending is not None and now >= self.derot_pending[0]:
                self.derot_goal = self.S[D2 + 'REALPOS'] + self.derot_pending[1]
                self.derot_pending = None
            if self.track == 1 and self.derot_goal is None \
                    and int(self.S['POINTING.SETUP.DEROTATOR.SYNCMODE']) in (2, 3):
                self.S[D2 + 'REALPOS'] += self.field_rotation * dt
            for key, var, speed in (('derot_goal', D2 + 'REALPOS', self.derot_speed),
                                    ('focus_goal', FOCUS + 'REALPOS', self.focus_speed),
                                    ('cover_goal', 'AUXILIARY.COVER.REALPOS', self.cover_speed)):
                goal = getattr(self, key)
                if goal is None:
                    continue
                if key == 'focus_goal' and self.S[FOCUS + 'POWER_STATE'] == 0.0:
                    setattr(self, key, None)     # motor off: the move stops
                    continue
                d = goal - self.S[var]
                step = speed * dt
                if abs(d) <= step:
                    self.S[var] = goal
                    setattr(self, key, None)
                else:
                    self.S[var] += step if d > 0 else -step

    # ------------------------------------------------------------------ commands

    async def _set(self, cid, name, val, out):
        try:
            v = float(val)
        except ValueError:
            v = val.strip('"')
        if name == 'TELESCOPE.READY':
            if v:
                if not self.powered:
                    for i in range(5):
                        await asyncio.sleep(self.ready_time / 5)
                        self.ready_frac = (i + 1) / 5
                    self.powered = True
                    self.cover_goal = 1.0
                    while self.cover_goal is not None:
                        await asyncio.sleep(0.01)
                return True
            self.track = 0
            self.eq = None
            self.S['POINTING.TRACK'] = 0
            self._set_goal(PARK, 0.0)
            while self.goal is not None or self.pending is not None or math.hypot(*self.vel) > 0.05:
                await asyncio.sleep(0.01)
            self.cover_goal = 0.0
            while self.cover_goal is not None:
                await asyncio.sleep(0.01)
            self.powered = False
            self.ready_frac = 0.0
            return True
        if name not in self.S and name not in ('TELESCOPE.STOP', 'TELESCOPE.STATUS.CLEAR') \
                and not name.endswith('.TARGETPOS'):
            out('%s DATA ERROR %s UNKNOWN' % (cid, name))
            return None
        if (not self.powered or self.link_down) and not name.startswith(('OBJECT.', 'POINTING.SETUP.')):
            out('%s EVENT WARN %s:0 Telescope not ready' % (cid, name))
            return False
        if name == 'POINTING.TRACK':
            t = int(v)
            if t == 0:
                self.S['POINTING.TRACK'] = 0
                if self.track == 1 and not self.moving():
                    self.goal = None
                self.track = 0 if not self.moving() else self.track
                return True
            if t in (1, 2):
                if self.S['OBJECT.TYPE'] == 'EQUATORIAL':
                    ra, dec = self.S['OBJECT.EQUATORIAL.RA'], self.S['OBJECT.EQUATORIAL.DEC']
                    goal = hadec_to_azalt(self.lst() - ra, dec)
                    self.eq = (ra, dec) if t == 1 else None
                else:
                    goal = (self.S['OBJECT.HORIZONTAL.AZ'], self.S['OBJECT.HORIZONTAL.ALT'])
                    self.eq = None
                self._set_goal(goal, self.start_track if t == 1 else self.start_altaz)
                if int(self.S['POINTING.SETUP.FOCUS.SYNCMODE']) & 16:
                    self.S[FOCUS + 'POWER_STATE'] = 0.0
                self.track = t
                self.S['POINTING.TRACK'] = 1 if t == 1 else 0
                if t == 1 and int(self.S['POINTING.SETUP.DEROTATOR.SYNCMODE']) == 3:
                    offset = self.S['POINTING.SETUP.DEROTATOR.OFFSET']
                    self.derot_pending = (time.monotonic() + self.start_track, offset - self.applied_offset)
                    self.applied_offset = offset
                return True
            if t == 4 and int(self.S['POINTING.SETUP.FOCUS.SYNCMODE']) & 1:
                self.S[FOCUS + 'POWER_STATE'] = 1.0
                self.S[FOCUS + 'TARGETPOS'] = self.S['POINTING.SETUP.FOCUS.POSITION']
                self.focus_goal = self.S['POINTING.SETUP.FOCUS.POSITION']
            return True
        if name == 'TELESCOPE.STOP':
            return True                                 # accepted, does nothing (as on the real Pilar)
        if name == D2 + 'TARGETPOS':
            self.S[name] = v
            self.derot_goal = v
            while self.derot_goal is not None:
                await asyncio.sleep(0.01)
            return True
        if name == 'AUXILIARY.COVER.TARGETPOS':
            self.S[name] = v
            self.cover_goal = v
            while self.cover_goal is not None:
                await asyncio.sleep(0.01)
            return True
        if name == FOCUS + 'TARGETPOS':
            self.S[name] = v
            if self.S[FOCUS + 'POWER_STATE'] == 1.0:
                self.focus_goal = v
            return True
        if name.endswith('.TARGETPOS'):
            out('%s DATA ERROR %s UNKNOWN' % (cid, name))
            return None
        self.S[name] = v
        if name == 'POINTING.SETUP.DEROTATOR.SYNCMODE' and int(v) != 3:
            self.applied_offset = self.S['POINTING.SETUP.DEROTATOR.OFFSET']
        if name.startswith('OBJECT.EQUATORIAL.'):
            self.S['OBJECT.TYPE'] = 'EQUATORIAL'
        elif name.startswith('OBJECT.HORIZONTAL.'):
            self.S['OBJECT.TYPE'] = 'HORIZONTAL'
        return True

    async def _handle(self, reader, writer):
        if self.clients >= self.max_clients:
            writer.write(b'TOO MANY CLIENTS CONNECTED')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        self.clients += 1
        self.writers.add(writer)
        await self.awake.wait()
        writer.write(('TPL2 2.1 CONN %d AUTH ENC MESSAGE This is Fake Pilar\nAUTH OK 0 0\n'
                      % self.clients).encode())

        def out(text):
            if not writer.is_closing():           # the client may have gone (e.g. a command it gave up on)
                writer.write((text + '\n').encode('latin-1'))

        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                await self._process(raw.decode('latin-1').strip(), out, writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.clients -= 1
            self.writers.discard(writer)
            writer.close()

    async def _process(self, line, out, writer):
        await self.awake.wait()
        if line == 'DISCONNECT':
            out('DISCONNECT OK')
            writer.close()
            return
        parts = line.split(' ', 2)
        if not parts[0].isdigit():
            out('0 COMMAND ERROR SYNTAX')
            out('0 COMMAND FAILED')
            return
        cid = parts[0]
        verb = parts[1] if len(parts) > 1 else ''
        arg = parts[2] if len(parts) > 2 else ''
        self.received.append(f'{verb} {arg}')
        if verb == 'GET' and arg:
            out('%s COMMAND OK' % cid)
            for name in arg.split(';'):
                out('%s DATA INLINE %s=%s' % (cid, name.upper(), self.value(name.upper())))
            out('%s COMMAND COMPLETE' % cid)
        elif verb == 'SET' and '=' in arg:
            name, _, val = arg.partition('=')
            out('%s COMMAND OK' % cid)
            await writer.drain()
            ok = await self._set(cid, name.upper(), val, out)
            await self.awake.wait()
            if ok:
                out('%s DATA OK %s' % (cid, name.upper()))
            elif ok is False:
                out('%s DATA ERROR %s FAILED' % (cid, name.upper()))
            out('%s COMMAND COMPLETE' % cid)
        else:
            out('%s COMMAND ERROR %s' % (cid, 'SYNTAX' if verb in ('GET', 'SET') else 'UNKNOWN'))
            out('%s COMMAND FAILED' % cid)
        try:
            await writer.drain()
        except Exception:
            pass
