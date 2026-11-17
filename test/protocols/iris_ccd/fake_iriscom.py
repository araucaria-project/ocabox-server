"""A stand-in for iriscom (IRIS CCD server, iriscom.c 3.5) for tests: same packets, same replies, one command at
a time. Only what the connector uses is emulated."""
import asyncio
import copy
import time
from typing import List, Optional

from obsrv.protocols.iris_ccd.iris_ccd_connector import load_protocol_config

PACKET_SIZE = 1024
# iriscom.c command table, matched like iriscom: any prefix of a command word (strncasecmp)
COMMANDS = ['quit', 'sync', 'dl  ', 'go  ', 'et  ', 'clear ', 'xfer ', 'filter ']


def fast_settings(**timeouts) -> dict:
    """Protocol settings of iris_ccd_config.yml with timeouts short enough for tests."""
    cfg = copy.deepcopy(load_protocol_config())
    cfg['timeouts'].update({'command': 1.0, 'filter_move': 2.0, 'filter_rehome': 2.0,
                            'exposure_overhead': 0.2, 'exposure_margin': 1.0})
    cfg['timeouts'].update(timeouts)
    cfg['nats'].update({'first_value_wait': 0.2, 'resubscribe_interval': 0.0})
    return cfg


class FakeIriscom(asyncio.DatagramProtocol):

    def __init__(self):
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.received: List[str] = []         # every command processed, in order
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker: Optional[asyncio.Task] = None
        self.etime = 0.0
        self.frameno = 447182
        self.remotehost = ''
        self.remoteport = 4980
        self.go_overhead = 0.1                 # [s] emulated readout
        self.filter_delay = 0.2                # [s] emulated wheel move
        self.full_turn_delay = 0.4             # [s] move to the current filter (a full turn in iriscom)
        self.current_filter = 0                # 0: home
        self.electronics_ok = True
        self.go_error: Optional[str] = None    # e.g. "E300 Reset read failed.\n"
        self.et_ignores_value = False          # answer with the old etime (as for "et go")
        self.stray_reply_before: Optional[str] = None  # a reply to another command sent before the real one

    @classmethod
    async def start(cls) -> 'FakeIriscom':
        loop = asyncio.get_running_loop()
        fake = cls()
        await loop.create_datagram_endpoint(lambda: fake, local_addr=('127.0.0.1', 0))
        fake.worker = loop.create_task(fake._serve())
        return fake

    @property
    def address(self) -> str:
        host, port = self.transport.get_extra_info('sockname')[:2]
        return f'{host}:{port}'

    def close(self):
        if self.worker is not None:
            self.worker.cancel()
        if self.transport is not None:
            self.transport.close()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) != PACKET_SIZE:   # iriscom: "Invalid packet", no answer
            return
        self.queue.put_nowait((data.split(b'\0', 1)[0].decode('ascii'), addr))

    def _reply(self, inl: str, status: str, addr):
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        packet = f"{stamp} {inl}\n{status}".encode('ascii')[:PACKET_SIZE - 1].ljust(PACKET_SIZE, b'\0')
        self.transport.sendto(packet, addr)

    async def _serve(self):
        while True:
            inl, addr = await self.queue.get()
            self.received.append(inl)
            if self.stray_reply_before:
                self._reply(self.stray_reply_before, 'OKAY stray', addr)
                self.stray_reply_before = None
            status = await self._execute(inl)
            self._reply(inl, status, addr)

    async def _execute(self, inl: str) -> str:
        words = inl.split()
        s1 = words[0] if words else ''
        matches = [c.strip() for c in COMMANDS if c.lower().startswith(s1.lower())] if s1 else []
        if len(matches) != 1:
            return 'E100 Invalid or ambiguous command.'
        cmd, args = matches[0], words[1:]
        if cmd == 'sync':
            return 'OKAY Camera electronics communications OK.' if self.electronics_ok \
                else 'E300 Camera electronics not responding.'
        if cmd == 'et':
            if args and not self.et_ignores_value:
                try:
                    self.etime = float(args[0])
                except ValueError:
                    pass
            return f'OKAY etime = {self.etime:.1f}'
        if cmd == 'go':
            await asyncio.sleep(max(self.etime, 0.0) + self.go_overhead)
            if self.go_error:
                return self.go_error
            name = f'iris{self.frameno:08d}'
            self.frameno += 1
            return f'OKAY {name}r {name}d'
        if cmd == 'clear':
            return 'OKAY Reset successful.'
        if cmd == 'xfer':
            if args:
                self.remotehost = '' if args[0] == '-' else args[0]
                if len(args) > 1:
                    self.remoteport = int(args[1]) or 4980
            return f"OKAY {self.remotehost or '-'} {self.remoteport}"
        if cmd == 'filter':
            if not args:
                return 'E100 Missing Filter-wheel command'
            arg = args[0]
            if arg == 'status':
                return 'OKAY Success'
            if arg == 'rehome':
                await asyncio.sleep(self.filter_delay)
                self.current_filter = 0
                return 'OKAY Success'
            if len(arg) == 1 and arg.isdigit() and 1 <= int(arg) <= 8:
                await asyncio.sleep(self.full_turn_delay if int(arg) == self.current_filter else self.filter_delay)
                self.current_filter = int(arg)
                return 'OKAY Success'
            return 'E301 Filter-wheel error'
        # quit / dl: must never arrive; answer like iriscom would (quit would end it)
        return 'OKAY Normal exit.' if cmd == 'quit' else 'OKAY Download successful.'


class StubComponent:
    """The part of a tree component the connector uses."""

    def __init__(self, kind: str, options: dict, sys_id: Optional[str] = None):
        self.kind = kind
        self.sys_id = sys_id or f'obs.{kind}'
        self.options = options

    def get_option_recursive(self, option):
        return self.options.get(option)


def camera_options(address: str, **extra) -> dict:
    options = {'protocol': 'iris_ccd', 'address': address, 'sensorname': 'QUIRC', 'cameraxsize': 1024,
               'cameraysize': 1024, 'exposuremin': 0.0, 'exposuremax': 3600.0,
               'ccdtemperature': {'subject': 'tic.telemetry.iris.ccdtemperature',
                                  'value_path': ['measurements', 'temperature'], 'timestamp_path': ['ts'],
                                  'max_age': 60.0}}
    options.update(extra)
    return options


def filterwheel_options(address: str, **extra) -> dict:
    options = {'protocol': 'iris_ccd', 'address': address, 'filters': {'J': 8, 'H': 7, 'K': 5}}
    options.update(extra)
    return options
