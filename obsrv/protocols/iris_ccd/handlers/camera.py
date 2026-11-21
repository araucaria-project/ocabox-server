"""Handlers of the camera: state of the exposure (kept by TIC, iriscom reports nothing), exposure, clear, xfer, the
frame (fixed: iriscom always reads the full detector without binning) and the detector temperature from NATS.

An exposure is a long operation of the session: ``et <s>`` + ``go``; it runs to the end also when the client stops
waiting, and TIC keeps its result (``lastimage``).
"""
import asyncio
import logging
import posixpath
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import TreeOtherError

from obsrv.protocols.iris_ccd.iris_ccd_exceptions import IrisConfigError, IrisDeviceError, IrisProtocolError
from obsrv.protocols.iris_ccd.iris_ccd_session import IrisReply, IrisSession
from obsrv.protocols.iris_ccd.iris_ccd_telemetry import TelemetryValue
from obsrv.protocols.iris_ccd.handlers.base import HandlerBase

logger = logging.getLogger('iris_ccd_connector')     # one logger for the whole IRIS CCD connector


class CameraHandlers(HandlerBase):
    """Mixin of :class:`IrisCcdConnector`: handlers of the ``camera`` component."""

    #: given by :class:`IrisCcdConnector`
    _telemetry: Dict[str, TelemetryValue]
    subscriber_factory: Callable

    async def _get_camerastate(self, component, data) -> int:
        return self._session(component).camera.state()

    async def _get_imageready(self, component, data) -> bool:
        return self._session(component).camera.image_ready

    async def _get_percentcompleted(self, component, data) -> int:
        return self._session(component).camera.percent_completed()

    async def _get_lastexposureduration(self, component, data) -> Optional[float]:
        return self._session(component).camera.last_duration

    async def _get_lastexposurestarttime(self, component, data) -> Optional[str]:
        return self._session(component).camera.last_start

    async def _get_lastimage(self, component, data) -> Optional[dict]:
        image = self._session(component).camera.last_image
        return dict(image) if image is not None else None

    async def _get_numx(self, component, data) -> int:
        return self._full_frame(component, 'x')

    async def _get_numy(self, component, data) -> int:
        return self._full_frame(component, 'y')

    async def _put_startexposure(self, component, data) -> dict:
        duration = self._as_float(self._param(data, 'Duration'), 'Duration')
        light_param = self._param(data, 'Light', required=False)
        light = True if light_param is None else self._as_bool(light_param, 'Light')
        low = float(self._option(component, 'exposuremin'))
        high = float(self._option(component, 'exposuremax'))
        if not low <= duration <= high:
            raise AddressError(code=1003, message=f"Duration {duration} s outside {low}..{high} s")
        if not light:
            logger.warning(f"{component.sys_id}: Light=False requested, but iriscom has no shutter; "
                           f"the exposure is taken as usual")
        session = self._session(component)
        return await session.operation('exposure', lambda: self._expose(session, duration, light))

    async def _expose(self, session: IrisSession, duration: float, light: bool) -> dict:
        s = self.settings
        camera = session.camera
        try:
            reply = await session.exchange(f"{s.commands['exposure_time']} {duration!r}", s.timeouts['command'])
            reply.require_ok()
            match = s.exposure_time_re.match(reply.message)
            if not match:
                raise IrisProtocolError(f"unexpected reply to '{reply.command}': {reply.message!r}")
            set_to = float(match.group(1))
            if abs(set_to - duration) > s.exposure_time_tolerance:
                raise IrisDeviceError(None, f"iriscom set the exposure time to {set_to} s instead of {duration} s")
        except Exception as e:
            camera.fail(str(e))
            raise
        expected = duration + s.timeouts['exposure_overhead']
        started = camera.begin(duration, expected)
        logger.info(f"IRIS CCD ({session.address}): exposure {duration} s started")
        try:
            reply = await session.exchange(s.commands['expose'], expected + s.timeouts['exposure_margin'])
            reply.require_ok()
            match = s.exposure_files_re.match(reply.message)
            if not match:
                raise IrisDeviceError(None, f"exposure ended without image files: {reply.message!r}")
            image = self._image_info(match.group('name'), started, duration, light)
        except BaseException as e:
            camera.fail(str(e) or type(e).__name__)
            logger.error(f"IRIS CCD ({session.address}): exposure {duration} s failed: {e}")
            raise
        camera.finish(image)
        logger.info(f"IRIS CCD ({session.address}): exposure {duration} s done, {image['name']}")
        return dict(image)

    def _image_info(self, name: str, started: float, duration: float, light: bool) -> dict:
        s = self.settings
        number = s.frame_number_re.search(name)
        return {
            'name': name,
            'number': int(number.group(1)) if number else None,
            'dir': s.images_dir,
            'files': {frame: posixpath.join(s.images_dir, f'{name}{letter}{s.images_suffix}')
                      for frame, letter in s.frames.items()},
            'start': datetime.fromtimestamp(started, timezone.utc).isoformat(timespec='milliseconds')[:-6],
            'duration': duration,
            'light': light,
        }

    async def _put_clear(self, component, data) -> str:
        session = self._session(component)
        reply = await session.command(self.settings.commands['clear'], self.settings.timeouts['command'])
        return reply.require_ok().message

    def _parse_xfer(self, reply: IrisReply) -> dict:
        match = self.settings.xfer_re.match(reply.message)
        if not match:
            raise IrisProtocolError(f"unexpected reply to '{reply.command}': {reply.message!r}")
        host = match.group('host')
        return {'host': None if host == self.settings.xfer_disabled_host else host,
                'port': int(match.group('port'))}

    async def _get_xfer(self, component, data) -> dict:
        session = self._session(component)
        reply = await session.command(self.settings.commands['xfer'], self.settings.timeouts['command'])
        return self._parse_xfer(reply.require_ok())

    async def _put_xfer(self, component, data) -> dict:
        """``Host`` (``'-'`` or empty switches the transfer off) and optional ``Port``. With a host set, iriscom
        sends every frame there and an exposure fails when the host does not receive it."""
        host = self._param(data, 'Host')
        host = self.settings.xfer_disabled_host if host in (None, '') else str(host)
        if not host or any(c.isspace() or ord(c) < 32 for c in host):
            raise AddressError(code=1003, message=f"Parameter 'Host' must be one word, got {host!r}")
        command = f"{self.settings.commands['xfer']} {host}"
        port = self._param(data, 'Port', required=False)
        if port is not None:
            port = self._as_int(port, 'Port')
            if not 0 <= port <= 65535:
                raise AddressError(code=1003, message=f"Parameter 'Port' must be 0..65535, got {port}")
            command += f" {port}"
        session = self._session(component)
        reply = await session.command(command, self.settings.timeouts['command'])
        return self._parse_xfer(reply.require_ok())

    async def _get_ccdtemperature(self, component, data) -> float:
        cfg = self._option(component, 'ccdtemperature')
        if not isinstance(cfg, dict):
            raise IrisConfigError(f"{component.sys_id}: 'ccdtemperature' must be a mapping")
        for key in ('subject', 'value_path', 'max_age'):
            if key not in cfg:
                raise IrisConfigError(f"{component.sys_id}: no 'ccdtemperature.{key}' in the configuration")
        telemetry = await self._telemetry_for(cfg)
        if telemetry.value is None and telemetry.reader_alive():
            try:
                await asyncio.wait_for(telemetry.received.wait(), timeout=self.settings.first_value_wait)
            except asyncio.TimeoutError:
                pass
        if telemetry.value is None:
            why = f" (reader: {telemetry.reader_error})" if telemetry.reader_error else ''
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"No detector temperature received from {telemetry.subject}{why}")
        age = time.time() - telemetry.ts
        if age > float(cfg['max_age']):
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"Detector temperature from {telemetry.subject} is {age:.0f} s old "
                                         f"(limit {float(cfg['max_age']):.0f} s)")
        return telemetry.value

    async def _telemetry_for(self, cfg: dict) -> TelemetryValue:
        subject = str(cfg['subject'])
        telemetry = self._telemetry.get(subject)
        if telemetry is None:
            telemetry = TelemetryValue(subject, cfg['value_path'], cfg.get('timestamp_path'))
            self._telemetry[subject] = telemetry
        if telemetry.reader_alive() or time.monotonic() < telemetry.next_subscribe:
            return telemetry
        telemetry.next_subscribe = time.monotonic() + self.settings.resubscribe_interval
        try:
            if telemetry.subscriber is not None:
                await telemetry.subscriber.close()
        except Exception:
            pass
        try:
            subscriber = self.subscriber_factory(subject, deliver_policy=self.settings.deliver_policy)
            reading = await subscriber.subscribe(telemetry.on_message)
        except Exception as e:
            telemetry.subscriber = None
            telemetry.reading_task = None
            telemetry.reader_error = str(e) or type(e).__name__
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f"Cannot subscribe to {subject}: {telemetry.reader_error}") from e
        telemetry.subscriber = subscriber
        telemetry.reading_task = getattr(reading, 'task', reading)
        telemetry.reader_error = None

        def ended(task: asyncio.Task, telemetry=telemetry) -> None:
            if task.cancelled():
                telemetry.reader_error = 'reader cancelled'
            else:
                error = task.exception()
                telemetry.reader_error = str(error) if error else 'reader ended'
            logger.error(f"NATS reader of {telemetry.subject} stopped: {telemetry.reader_error}")

        if isinstance(telemetry.reading_task, asyncio.Future):
            telemetry.reading_task.add_done_callback(ended)
        logger.info(f"IRIS CCD: subscribed to {subject}")
        return telemetry

    # ------------------------------------------------------------------ frame and fixed values

    def _fixed_getter(self, name: str):
        async def getter(component, data):
            return self.settings.camera_fixed[name]
        return getter

    def _full_frame(self, component, axis: str) -> int:
        size = int(self._option(component, f'camera{axis}size'))
        return size // int(self.settings.camera_fixed[f'bin{axis}'])

    def _frame_setter(self, name: str):
        """binx, biny, startx, starty, numx, numy: iriscom always reads the full detector without binning, so the
        only value accepted is the one in force; it is not sent anywhere."""
        async def setter(component, data):
            value = self._as_int(self._param(data, name), name)
            if name in ('numx', 'numy'):
                allowed = self._full_frame(component, name[-1])
            else:
                allowed = int(self.settings.camera_fixed[name])
            if value != allowed:
                raise AddressError(code=1003, message=f"IRIS CCD supports only {name} = {allowed}, got {value}")
            return None
        return setter
