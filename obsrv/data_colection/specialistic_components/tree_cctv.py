from __future__ import annotations

import inspect
import logging
import os
from logging.handlers import RotatingFileHandler

import time as time_module

from obcom.data_colection.address import AddressError
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obcom.data_colection.coded_error import TreeOtherError
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest

logger = logging.getLogger(__name__.rsplit('.')[-1])


class UnifiProtectApiSingleton:
    _instance = None
    _logger = None
    _states = {}
    _config = None
    _disconnect_socket = None
    _protect_client = None

    @classmethod
    def create_logger(cls):
        if cls._logger:
            return
        try:
            os.makedirs('/var/log/ocabox', exist_ok=True)
        except PermissionError:
            logger.warning('Cannot create /var/log/ocabox directory (no premissions). '
                           'Create it manually, and ensure write permissions for the user running the ocabox')
            cls._logger = logger
            return
        try:
            handler = RotatingFileHandler('/var/log/ocabox/unifiprotect.log', maxBytes=1000000, backupCount=5)
        except PermissionError:
            logger.warning('Cannot create /var/log/ocabox/unifiprotect.log file (no premissions). '
                           'Create it manually, and ensure write permissions for the user running the ocabox')
            cls._logger = logger
            return
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        handler.setLevel(logging.INFO)
        cls._logger = logging.getLogger('pyunifiprotect.api')
        cls._logger.setLevel(logging.INFO)
        cls._logger.addHandler(handler)
        cls._logger.propagate = False
        cls._logger.info(f'Created UnifiProtectApiSingleton logger')
        logger.addHandler(handler)
        logger.propagate = True
        logger.info(f'Added Handler for this log to write to {handler.baseFilename}')
        cls._logger.info(f'Log is maintenance-free rotating-log')
        # Redirecting pyunifiprotect.data.bootstrap logger to our logger
        logging.getLogger('pyunifiprotect.data.bootstrap').addHandler(handler)
        logging.getLogger('pyunifiprotect.data.bootstrap').propagate = False


    def __new__(cls):
        cls.create_logger()
        if not cls._instance:
            cls._instance = super().__new__(cls)
        cls._instance._config = None
        cls._instance._protect_client = None
        cls._logger.info(f'Returned UnifiProtectApiSingleton')
        return cls._instance

    def configure(self, host: str, port: int, username: str, password: str, verify_ssl: bool = False) -> None:
        parameters = inspect.signature(self.configure).parameters
        self._logger.info(f'Configuring UnifiProtectApiSingleton with host={host}, port={port}, username={username}, '
                          f'password=****, verify_ssl={verify_ssl}')
        cfg = {name: value for name, value in locals().items() if name in parameters.keys() and name != 'self'}
        empties = [k for k, v in cfg.items() if v == '']
        if empties:
            logger.warning(f'Ubiquity CCTV control disabled: '
                           f'TreeCCTV config misses: {",".join(empties)}, add it to your .secrets.yaml')
            self._logger.error(f'Config misses: {",".join(empties)}, add it to your .secrets.yaml')
            return
        self._config = cfg

    def is_configured(self) -> bool:
        return self._config is not None

    async def initialize(self):
        if not self.is_configured():
            self._logger.error('An attempt to initialize UnifiProtectApiSingleton which is not configured.')
            return False
        try:
            from pyunifiprotect import ProtectApiClient
            from pyunifiprotect.data import Camera, types
        except ImportError:
            logger.warning(f'Ubiquity CCTV control disabled: Can not import `pyunifiprotect` package')
            self._logger.error(f'Can not import `pyunifiprotect` package')
            return False

        self._states = {False: types.IRLEDMode.AUTO_NO_LED, True: types.IRLEDMode.AUTO}

        try:
            t1 = time_module.time()
            self._protect_client = ProtectApiClient(**self._config)
            t2 = time_module.time()
            await self._protect_client.update()
            t3 = time_module.time()
            self._disconnect_socket = self._protect_client.subscribe_websocket(
                lambda msg: self._handle_websocket_message(msg))
            t4 = time_module.time()
        except Exception as e:
            logger.warning(f'Ubiquity CCTV control disabled: initialization failed: {e}')
            self._logger.error(f'UnifiProtectApiSingleton initialization failed: {e}')
            if self._protect_client:
                await self._protect_client.close_session()
                self._protect_client = None
            self._protect_client = None
            return False
        self._logger.info(f'UnifiProtectApiSingleton initialized in {t4-t1:.2f}s (crate: {t2-t1:.2f}s, '
                          f'update: {t3-t2:.2f}s, subscribe: {t4-t3:.2f}s)')
        return True

    async def refresh_initialization(self):
        if self._protect_client is not None:
            t = time_module.time()
            await self._protect_client.update()
            self._logger.info(f'Refreshing UnifiProtectApiSingleton client in {time_module.time()-t:.2f}s')
            return True
        else:
            self._logger.error(f'An attempt to refresh UnifiProtectApiSingleton which is not initialized.')
            return False

    async def ensure_initialized(self):
        if self._protect_client is None:
            return await self.initialize()
        else:
            return await self.refresh_initialization()

    def _handle_websocket_message(self, msg):
        from pyunifiprotect.data import WSSubscriptionMessage
        msg: WSSubscriptionMessage = msg
        try:
            c = msg.changed_data['isp_settings']['ir_led_mode']
            self._logger.info(f'Websocket update, IR LED changed to: {c}')
        except LookupError:
            pass

    async def close(self):
        if self._protect_client is not None:
            t1 = time_module.time()
            self._disconnect_socket()
            self._logger.info(f'Websocket disconnected in {time_module.time()-t1:.2f}s')
            t2 = time_module.time()
            await self._protect_client.close_session()
            self._protect_client = None
            self._logger.info(f'UnifiProtectApiSingleton session closed in {time_module.time()-t2:.2f}s')
            logger.info('UnifiProtectApiSingleton closed')
        else:
            self._logger.info(f'UnifiProtectApiSingleton already closed')
            logger.info('UnifiProtectApiSingleton already closed')

    async def get_camera_by_name(self, camera_name: str):
        if not await self.ensure_initialized():
            self._logger.warning(f'get_camera_by_name: UnifiProtectApiSingleton is not initialized')
            return None

        t1 = time_module.time()
        cams = {cam.name: cam for cam in self._protect_client.bootstrap.cameras.values()}
        t2 = time_module.time()
        try:
            cam = cams[camera_name]
            self._logger.info(f'Camera found: {camera_name} ({cam.id}:{cam.name}) in {t2-t1:.2f}s')
        except LookupError:
            logger.warning(f'Ubiquity CCTV control disabled: '
                           f"Camera {camera_name} not fund (available cameras: {cams.keys()})")
            return None
        logger.info(f'Ubiquity CCTV for camera {camera_name} API initialized')

        return cam

    async def set_camera_ir(self, camera_name: str, on: bool):
        cam = await self.get_camera_by_name(camera_name)
        if cam is None:
            return False
        t = time_module.time()
        await cam.set_ir_led_model(self._states[on])
        self._logger.info(f'IR LED state set to {on} ({self._states[on]}) in {time_module.time()-t:.2f}s')
        return True


class TreeCCTV(TreeProvider):
    """
    This module steers the Ubiquity CCTV camera.
    """
    COMPONENT_DEFAULT_NAME = 'TreeCCTV'

    def __init__(self, camera_id: str, source_name: str, **kwargs):
        self._api: UnifiProtectApiSingleton | None = None
        self.cam_name = None
        super().__init__(component_name=camera_id, source_name=source_name, subcontractor=None, **kwargs)
        logger.info(f'Created {self}')

    async def get_value(self, request: ValueRequest, **kwargs) -> Value or None:
        user = request.user
        request_type = request.request_type

        try:
            command = request.address[request.index]
        except IndexError:
            raise AddressError(code=1001, message='The address does not contain a command.')

        if command == 'ir':
            if request_type == 'PUT':
                try:
                    on: bool = request.request_data['state']
                except KeyError:
                    raise TreeOtherError(code=4007,
                                         message=f'{self.get_name()}: Missing "state" parameter in {command} request.')
                ret = await self._set_ir(on)
            else:
                # @Ernest: Is it going to be cached in current structure?
                ret = 'unknown'
            return Value(v=ret, ts=time_module.time())
        elif command == 'snapshot' and request_type == 'GET':
            ret = await self._get_snapshot()
            if ret is None:
                # @Ernest: Jak to zwrocic blad? Przez raise? Jak wybrać odpowiedni kod? (i severity?)
                raise TreeOtherError(code=4002, message='Snapshot not returned')
            return Value(v=ret, ts=time_module.time())

        raise AddressError(code=1002,
                           message=f'Unrecognised method {request_type}:{command} for module {self.get_name()}')

    async def run(self):
        await self._init_api()
        return await super().run()

    async def stop(self):
        await self._close_api()
        return await super().stop()

    async def _init_api(self):
        self._api = UnifiProtectApiSingleton()
        self.cam_name = self._get_cfg("udm_camera_id", None)
        if self.cam_name is None or self.cam_name == '':
            logger.warning(f'{self} Ubiquity CCTV control disabled: '
                           f'TreeCCTV config misses: udm_camera_id, add it to your configuration')
            return
        if not self._api.is_configured():
            self._api.configure(
                host=self._get_cfg("udm_host", ''),
                port=self._get_cfg("udm_port", 443),  # 7433
                username=self._get_cfg("udm_user", ''),
                password=self._get_cfg("udm_password", ''),
            )


    async def _close_api(self):
        if self._api is None:
            logger.info(f'{self} Ubiquity CCTV camera API not initialized, close API ignored')
            return
        await self._api.close()

        logger.info(f'{self} Ubiquity CCTV camera API closed')

    async def _get_snapshot(self):
        if self.cam_name is None:
            logger.info(f'{self} Unifi CCTV API not initialized, command get_snapshot ignored')
            return
        try:
            cam = await self._api.get_camera_by_name(self.cam_name)
        except Exception as e:
            logger.warning(f'{self} Unifi CCTV API get_camera_by_name error: {e}')
            return
        if cam is None:
            logger.info(f'{self} Unifi CCTV API not initialized, command get_snapshot() ignored')
            return None
        return await cam.get_snapshot()

    async def _set_ir(self, on: bool) -> None:
        if self.cam_name is None:
            logger.info(f'{self} Unifi CCTV API camera name not configured, command set_ir({on}) ignored')
            return
        if self._api is None:
            logger.info(f'{self} Unifi CCTV API not initialized, command set_ir({on}) ignored')
            return
        try:
            cam = await self._api.get_camera_by_name(self.cam_name)
            await self._api.set_camera_ir(self.cam_name, on)
        except Exception as e:
            logger.warning(f'{self} Unifi CCTV API error: {e}')
            return
