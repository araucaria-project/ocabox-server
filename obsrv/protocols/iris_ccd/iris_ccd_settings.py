"""``iris_ccd_config.yml`` of the IRIS CCD connector: read and checked (:class:`ProtocolSettings`); a missing or
wrong key raises :class:`IrisConfigError` naming the key."""
import logging
import os
import re
from typing import Any

import confuse

from obsrv.protocols.iris_ccd.iris_ccd_exceptions import IrisConfigError

logger = logging.getLogger('iris_ccd_connector')     # one logger for the whole IRIS CCD connector

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'iris_ccd_config.yml')

_CAMERA = 'camera'
_FILTERWHEEL = 'filterwheel'


def _path(cfg: dict, dotted: str):
    node: Any = cfg
    for key in dotted.split('.'):
        if not isinstance(node, dict) or key not in node:
            raise IrisConfigError(f"{CONFIG_PATH}: missing key {dotted!r}")
        node = node[key]
    return node


def load_protocol_config(path: str = CONFIG_PATH) -> dict:
    """The protocol settings (``iris_ccd_config.yml``) as a plain dict."""
    logger.info(f"Loading IRIS CCD protocol configuration from: {path}")
    try:
        config = confuse.Configuration('IrisCcdConnector', __name__, read=False)
        config.set_file(path)
        return config.get()
    except (confuse.ConfigError, OSError) as e:
        raise RuntimeError(f"IRIS CCD protocol configuration {path} is missing or corrupted: {e}") from e


class ProtocolSettings:
    """Validated settings of ``iris_ccd_config.yml``. Every value the connector needs comes from there."""

    def __init__(self, cfg: dict):
        try:
            self.packet_size = int(_path(cfg, 'transport.packet_size'))
            self.encoding = str(_path(cfg, 'transport.encoding'))
            self.timeouts = {k: float(_path(cfg, f'timeouts.{k}')) for k in
                             ('command', 'filter_move', 'filter_rehome', 'exposure_overhead', 'exposure_margin')}
            self.commands = {k: str(_path(cfg, f'commands.{k}')) for k in
                             ('sync', 'exposure_time', 'expose', 'clear', 'xfer', 'filter', 'filter_rehome')}
            self.forbidden = {str(c).lower() for c in _path(cfg, 'commands.forbidden')}
            self.reply_ok = str(_path(cfg, 'replies.ok'))
            self.error_re = re.compile(_path(cfg, 'replies.error_pattern'), re.DOTALL)
            self.exposure_time_re = re.compile(_path(cfg, 'replies.exposure_time_pattern'))
            self.exposure_time_tolerance = float(_path(cfg, 'replies.exposure_time_tolerance'))
            self.exposure_files_re = re.compile(_path(cfg, 'replies.exposure_files_pattern'))
            self.frame_number_re = re.compile(_path(cfg, 'replies.frame_number_pattern'))
            self.xfer_re = re.compile(_path(cfg, 'replies.xfer_pattern'))
            self.xfer_disabled_host = str(_path(cfg, 'replies.xfer_disabled_host'))
            self.images_dir = str(_path(cfg, 'images.dir'))
            self.images_suffix = str(_path(cfg, 'images.suffix'))
            self.frames = {k: str(_path(cfg, f'images.frames.{k}')) for k in ('reset', 'middle', 'data')}
            self.camera_states = {k: int(_path(cfg, f'camera.states.{k}')) for k in
                                  ('idle', 'exposing', 'reading', 'error')}
            self.camera_fixed = dict(_path(cfg, 'camera.fixed'))
            self.first_slot = int(_path(cfg, 'filterwheel.first_slot'))
            self.last_slot = int(_path(cfg, 'filterwheel.last_slot'))
            self.unknown_position = int(_path(cfg, 'filterwheel.unknown_position'))
            self.skip_same_position = bool(_path(cfg, 'filterwheel.skip_same_position'))
            self.rehome_enabled = bool(_path(cfg, 'filterwheel.rehome_enabled'))
            self.default_focus_offset = int(_path(cfg, 'filterwheel.default_focus_offset'))
            self.info = {kind: {key: str(_path(cfg, f'info.{kind}.{key}')) for key in ('name', 'description')}
                         for kind in (_CAMERA, _FILTERWHEEL)}
            self.deliver_policy = str(_path(cfg, 'nats.deliver_policy'))
            self.first_value_wait = float(_path(cfg, 'nats.first_value_wait'))
            self.resubscribe_interval = float(_path(cfg, 'nats.resubscribe_interval'))
        except (TypeError, ValueError, re.error) as e:
            raise IrisConfigError(f"{CONFIG_PATH}: wrong value: {e}") from e
        for name in ('binx', 'biny', 'startx', 'starty'):
            if name not in self.camera_fixed:
                raise IrisConfigError(f"{CONFIG_PATH}: missing key 'camera.fixed.{name}'")

    def encode(self, command: str) -> bytes:
        data = command.encode(self.encoding)
        if len(data) >= self.packet_size:
            raise ValueError(f"command longer than {self.packet_size - 1} bytes")
        return data.ljust(self.packet_size, b'\0')

    def decode(self, data: bytes) -> str:
        return data.split(b'\0', 1)[0].decode(self.encoding, errors='replace')
