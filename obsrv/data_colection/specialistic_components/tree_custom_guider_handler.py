import logging
import time
from typing import Optional

from obcom.comunication.comunication_error import CommunicationRuntimeError, CommunicationTimeoutError
from obcom.data_colection.coded_error import TreeOtherError
from obcom.data_colection.address import AddressError
from obsrv.data_colection.base_components.tree_provider import TreeProvider
from obsrv.data_colection.resource_manager.resource_manager import TelescopeComponentManagerAlpaca
from obsrv.data_colection.specialistic_components.tree_alpaca import TreeAlpacaObservatory
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueRequest, ValueResponse
from typing import Dict, List
from serverish.base.datetime import dt_utcnow_array
import asyncio
import numpy as np
from serverish.base.exceptions import MessengerRequestNoResponders, MessengerRequestNoResponse, MessengerRequestTimeout
import math

logger = logging.getLogger(__name__.rsplit('.')[-1])


class GuiderHandler:

    STATUSES = ['idle', 'dark', 'calibration', 'guiding', 'preview', 'error']
    DIRECTIONS = [0, 1, 2, 3]

    def __init__(self, guider_source_name: str) -> None:
        self._guider_source_name: str = guider_source_name
        self.rs: Optional[TelescopeComponentManagerAlpaca] = None # Validate if rs not null
        self.api = None
        self.nats = None
        self.status: str = 'idle'
        self.exposure: float = 3.0 # in seconds
        self.period: float = 6.0 # in seconds
        self.calib_step: float = 1000.0 # in milliseconds
        self.calib_no_of_steps: int = 6
        self.calib_data: Optional[Dict] = None
        self.preview_single: bool = False
        self.sequence_id: str = ''
        self.loop_dark: int = 0
        self.nloops_dark: int = 0
        self.star_select: Optional[Dict] = None
        self.mount_follow: bool = True
        self.darks: Dict = {}
        self.rpc_req = None
        self.journal_publisher = None

    @property
    def _telescope_id(self) -> str:
        return self.rs.get_observatory_name()

    def _get_mount_comm(self, command: str) -> Optional[str]:
        res_mount = self.rs.get_resource(self.rs.MOUNT)
        if res_mount is not None:
            return f'{res_mount.adr}.{command}'
        else:
            return None

    def _get_camera_comm(self, command: str) -> Optional[str]:
        res_cam = self.rs.get_resource_by_source_name(self._guider_source_name, self.rs.CAMERA)
        if res_cam is not None:
            return f'{res_cam.adr}.{command}'
        else:
            return None

    def _get_access_comm(self, command: str) -> str:
        return f'{self._telescope_id}.access_grantor.{command}'

    async def _take_control(self, for_how_long: float):
        # TODO temporary for tests
        # await self._journal_pub(msg='Requesting for take control', level='DEBUG')
        timeout_reservation = time.time() + for_how_long
        try:
            await self.api.put_async(address=self._get_access_comm('take_control'),
                                     parameters_dict={'timeout_reservation': timeout_reservation},
                                     no_wait=False)
            await self._journal_pub(msg='Control taken', level='DEBUG')
            return True
        except (CommunicationRuntimeError, CommunicationTimeoutError):
            #await self._journal_pub(msg='Can not take control', level='DEBUG')
            return False

    async def _wait_for_exposure(self, wait_timeout: float = 30) -> bool:
        #await self._journal_pub(msg='Wait for exposure started.', level='DEBUG')
        cam_comm_imageready = self._get_camera_comm('imageready')
        if self.exposure >= 0.6:
            sub_t = self.exposure
        else:
            sub_t = 1.0
        await asyncio.sleep(float(sub_t))
        if cam_comm_imageready is not None:
            timou = time.time() + wait_timeout
            while True:
                if self.status in ['idle', 'error']:
                    return False
                if timou < time.time():
                    await self._journal_pub(msg=f'Wait for exposure timeout.', level="WARNING")
                    return False
                try:
                    res = await self.api.get_async(address=cam_comm_imageready,
                                                   time_of_data=0.5,
                                                   time_of_data_tolerance=0.5,
                                                   request_timeout=5)
                    if res is not None:
                        if res.value.v is True:
                            return True
                except CommunicationRuntimeError:
                    await self._journal_pub(msg=f'Wait for imageready failed.', level="WARNING")
                    return False
                except CommunicationTimeoutError:
                    pass
                await asyncio.sleep(0.1)
        else:
            return False

    async def _exec_exposure(self, light: bool) -> bool:
        ret = False
        if self.status not in ['idle', 'error']:
            cam_comm_start_exp = self._get_camera_comm('startexposure')
            if cam_comm_start_exp is not None:
                try:
                    #await self._journal_pub(msg=f'Exposure {self.exposure}s {light} sending....', level='DEBUG')
                    await self.api.put_async(address=cam_comm_start_exp,
                                             parameters_dict={'Duration': self.exposure, 'Light': light},
                                             no_wait=True)
                    #await self._journal_pub(msg=f'Exposure {self.exposure}s {light} send.', level='DEBUG')
                except (CommunicationRuntimeError, CommunicationTimeoutError):
                    await self._journal_pub(msg=f'Can not execute exposure.', level="WARNING")
                    return False
            ret = await self._wait_for_exposure()
        return ret

    async def _exec_pulseguide(self, direction: int, duration: float) -> bool:
        mo_comm_start_exp = self._get_mount_comm('pulseguide')
        if mo_comm_start_exp is not None:
            try:
                await self.api.put_async(address=mo_comm_start_exp,
                                         parameters_dict={'Direction': direction, 'Duration': duration},
                                         no_wait=True)
                return True
            except (CommunicationRuntimeError, CommunicationTimeoutError):
                return False
        else:
            return False

    @staticmethod
    def get_vector_angle_to_x(vector: np.ndarray) -> float:
        x_axis = np.array([10, 0])
        cos_alfa = np.dot(x_axis, vector) / (np.linalg.norm(x_axis) * np.linalg.norm(vector))
        if vector[1] < 0:
            a = -1
        else:
            a = 1
        return math.degrees(math.acos(cos_alfa)) * a

    @staticmethod
    def vector_lenght(vector: np.ndarray) -> float:
        return float(np.linalg.norm(vector))

    async def _calib_final_calc(self):
        # Calculate angles
        for n in self.DIRECTIONS:
            self.calib_data[n]['angle_to_x'] = self.get_vector_angle_to_x(self.calib_data[n]['guid_corr'].sum())
            vect_lenght = self.vector_lenght(self.calib_data[n]['guid_corr'].mean(axis=0))
            self.calib_data[n]['speed_px_per_millisec'] = vect_lenght / self.calib_step
            self.calib_data[n]['step_std'] = self.calib_data[n]['guid_corr'].std(axis=0)

        # self.calib_data['calib_status'] = 'ok'

    async def _build_calib_data(self, guid_corr) -> (bool, int):

        for n in self.DIRECTIONS:
            if n not in self.calib_data.keys():
                self.calib_data[n] = {'guid_corr': np.zeros((self.calib_no_of_steps, 2))}
                return True, n
            else:
                last_step = max(list(self.calib_data[n].keys()))
                if last_step != self.calib_no_of_steps:
                    step_no = last_step + 1
                    self.calib_data[n]['guid_corr'][step_no - 1] = guid_corr
                    if step_no == self.calib_no_of_steps:
                        return False, n
                    else:
                        return True, n
                else:
                    pass
        await self._calib_final_calc()
        # TODO TALK TO JOURNAL
        self.status = 'idle'

    async def _calib_corr(self, param: Dict,) -> None:
        try:
            guid_corr = param['guid_corr']
        except KeyError:
            # TODO TALK TO JOURNAL
            self.status = 'idle'
            return
        mount_pulse, direction = await self._build_calib_data(guid_corr=guid_corr)
        if mount_pulse:
            res = await self._exec_pulseguide(direction=direction, duration=self.calib_step)

    async def _guid_corr(self, param: Dict, t_start: float) -> None:
        try:
            guid_corr = param['guid_corr']
        except KeyError:
            # TODO TALK TO JOURNAL
            self.status = 'idle'
            return
        # TODO CALCULATE CORRECTION AND DIRECTION
        dec_dir = 0 # [0, 1]
        ra_dir = 2 # [2, 3]
        duration = 1
        res_1 = await self._exec_pulseguide(direction=dec_dir, duration=duration)
        res_2 = await self._exec_pulseguide(direction=ra_dir, duration=duration)
        # TODO here wait until the period ends, if time is longer just exit method

    def _get_seq_id(self) -> str:
        return f'{round((time.time())*10)}'[-7:]

    async def _send_rpc_request(self, req: str, loop: int, nloops: int):
        try:
            dat, met = await self.rpc_req.request(data=self._rpc_data(req=req, loop=loop, nloops=nloops),
                                                  meta=self._rpc_meta(),
                                                  timeout=5)
        except (MessengerRequestNoResponders, MessengerRequestNoResponse, MessengerRequestTimeout):
            logger.warning(f'Nats rpc guider msg can not reach responder')

            dat = None
            met = None
        return dat, met

    async def _journal_pub(self, msg: str, level: str = "INFO") :
        try:
           await self.journal_publisher.publish(data=self._journal_data(msg=msg, level=level),
                                                timeout=5)
        except (MessengerRequestTimeout):
            logger.warning(f'Nats rpc guider msg can not reach responder')

    def _journal_data(self, msg: str, level: str = "INFO") -> Dict:
        return {
            "ts": dt_utcnow_array(),
            "level": level,
            "message": msg,
            "op": "publish"
        }

    def _rpc_data(self, req: str, loop: int, nloops: int) -> Dict:
        return {
                "request": req,
                "star_select": [],
                "fits_id": f'{round((time.time())*10)}'[-7:],
                "sequence_id": self.sequence_id,
                "exp_time": self.exposure,
                "loop": loop,
                "nloops": nloops,
                #"tel_ra": 122.33, ?
                #"tel_dec": 60.0001, ?
                #"tel_alt": 44.5, ?
                #"tel_az": 22, ?
                "dtyp": "sideint16"
               }

    def _rpc_meta(self):
        return {
                "message_type": 'rpc',  # IMPORTANT type message, one of pre declared types
                "tags": [""],  # tags
                'sender': 'tic_guider',  # name who send message
                 }

    async def _preview(self):
        res = await self._exec_exposure(light=True)
        if res:
            if self.status not in ['idle', 'error']:
                #await self._journal_pub(msg=f'Rpc send.', level="DEBUG")
                d, m = await self._send_rpc_request(req="preview", loop=1, nloops=1)
                if d is not None:
                    try:
                        resp = d['response']
                        status = d['status']
                    except KeyError:
                        await self._journal_pub(msg=f'Wrong preview rpc response.', level="WARNING")
                        self.status = 'idle'
                        return
                    if resp in ['preview_done'] and status == 'ok':
                        await self._journal_pub(msg=f'Preview success.')
                        if self.preview_single:
                            await self._journal_pub(msg=f'Single preview done.')
                            self.status = 'idle'
                    else:
                        await self._journal_pub(msg=f'Wrong rpc response status.', level="WARNING")
                else:
                    await self._journal_pub(msg=f'Rpc response is None.', level="WARNING")
                    self.status = 'idle'
        else:
            await self._journal_pub(msg=f'Preview stopped.')
            self.status = 'idle'

    async def _dark(self):
        res = await self._exec_exposure(light=False)
        if res:
            if self.status not in ['idle', 'error']:
                self.loop_dark += 1
                d, m = await self._send_rpc_request(req="dark", loop=self.loop_dark, nloops=self.nloops_dark)
                if d is not None:
                    try:
                        resp = d['response']
                        status = d['status']
                    except KeyError:
                        await self._journal_pub(msg=f'Wrong dark rpc response.', level="WARNING")
                        self.status = 'idle'
                        return
                    if resp in ['dark_saved', 'master_dark_saved'] and status == 'ok':
                        await self._journal_pub(msg=f'Dark {self.loop_dark}/{self.nloops_dark} saved.')
                        if self.nloops_dark == self.loop_dark:
                            await self._journal_pub(msg=f'Master dark {self.exposure}s saved.')
                            self.status = 'idle'
                    elif status == 'error':
                        await self._journal_pub(msg=f'Master dark response error:{resp}.', level="WARNING")
                        self.status = 'idle'
                else:
                    await self._journal_pub(msg=f'Rpc response is None.', level="WARNING")
                    self.status = 'idle'
        else:
            await self._journal_pub(msg=f'Dark stopped.')
            self.status = 'idle'

    async def _calibration(self) -> None:
        await self._exec_exposure(light=True)
        d, m = await self._send_rpc_request(req="calib", loop=1, nloops=1)
        if d is not None:
            try:
                resp = d['response']
                status = d['status']
            except KeyError:
                # TODO TALK TO JOURNAL
                self.status = 'idle'
                return
            if resp in ['corr_calc'] and status == 'ok':
                # TODO TALK TO JOURNAL
                try:
                    param = d['param']
                except KeyError:
                    # TODO TALK TO JOURNAL
                    self.status = 'idle'
                    return
                await self._calib_corr(param=param)
            elif resp in ['star_selected'] and status == 'ok':
                # TODO TALK TO JOURNAL
                pass
            elif resp in ['star_lost'] and status == 'ok':
                # TODO TALK TO JOURNAL
                self.status = 'idle'
                pass
            else:
                pass
        else:
            # TODO TALK TO JOURNAL
            self.status = 'idle'

    async def _guiding(self) -> None:
        t_start = time.time()
        await self._exec_exposure(light=True)
        d, m = await self._send_rpc_request(req="guiding_simple", loop=1, nloops=1)
        if d is not None:
            try:
                resp = d['response']
                status = d['status']
            except KeyError:
                # TODO TALK TO JOURNAL
                self.status = 'idle'
                return
            if resp in ['corr_calc'] and status == 'ok':
                # TODO TALK TO JOURNAL
                try:
                    param = d['param']
                except KeyError:
                    # TODO TALK TO JOURNAL
                    self.status = 'idle'
                    return
                await self._guid_corr(param=param, t_start=t_start)
            elif resp in ['star_selected'] and status == 'ok':
                # TODO TALK TO JOURNAL
                pass
            else:
                # TODO TALK TO JOURNAL
                pass
        else:
            # TODO TALK TO JOURNAL
            self.status = 'idle'

    async def preview(self, single: bool = False):
        await self._journal_pub(msg=f'Preview started.')
        self.sequence_id = self._get_seq_id()
        self.preview_single = single
        await self._take_control(80000)
        self.status = 'preview'

    async def calibration(self) -> None:
        await self._journal_pub(msg=f'Calibration started.')
        self.sequence_id = self._get_seq_id()
        self.calib_data = {}
        self.status = 'calibration'

    async def dark(self, sub_no: int = 20) -> None:
        await self._journal_pub(msg=f'Dark {self.exposure}s subs:{sub_no} started.')
        self.sequence_id = self._get_seq_id()
        self.loop_dark = 0
        self.nloops_dark = sub_no
        await self._take_control(80000)
        self.status = 'dark'

    async def guiding(self, star_select: Optional[List] = None, mount_follow: bool = True) -> None:
        # TODO TALK TO JOURNAL
        if self.calib_data is not None:
            try:
                calib_status = self.calib_data['calib_status']
            except KeyError:
                # TODO TALK TO JOURNAL
                return
            if calib_status == 'ok':
                self.sequence_id = self._get_seq_id()
                self.star_select = star_select
                self.mount_follow = mount_follow
                self.status = 'guiding'
            else:
                # TODO TALK TO JOURNAL
                pass
        else:
            # TODO TALK TO JOURNAL
            pass

    async def stop(self) -> None:
        await self._journal_pub(msg=f'Guider stopped.')
        self.status = 'idle'

    async def run_body(self):
        self.rpc_req = self.nats.get_rpcrequester(f'tic.rpc.{self._telescope_id}.guiding')
        self.journal_publisher = self.nats.get_journalpublisher(f'tic.journal.{self._telescope_id}.guiding')
        while True:
            if self.status == 'preview':
                await self._preview()
            if self.status == 'dark':
                await self._dark()
            if self.status == 'calibration':
                await self._calibration()
            if self.status == 'guiding':
                await self._guiding()
            await asyncio.sleep(1)


class TreeCustomGuiderHandler(TreeProvider):
    """
    This module is responsible for managing custom telescope guider.
    The module has several defined address commands:
        - method1 - cos ...
        - method2 - cos ...
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeCustomGuiderHandler'
    _CFG_PRP_GDR_SRC_NAME = "guider_source_name"

    def __init__(self, component_name: str, source_name: str, target_alpaca: TreeAlpacaObservatory, **kwargs):
        super().__init__(component_name=component_name, source_name=source_name, **kwargs)
        self._target_alpaca: TreeAlpacaObservatory = target_alpaca
        self.rs = None
        self.gh: GuiderHandler = GuiderHandler(self._get_cfg(TreeCustomGuiderHandler._CFG_PRP_GDR_SRC_NAME))

    async def get_value(self, request: ValueRequest, **kwargs) -> Optional[Value]:

        # todo poniżej są dane wiadomości: user, request_type (PUT/GET), command (np. method1, method2)
        user = request.user
        request_type = request.request_type
        if user is None:
            raise TreeOtherError(code=4001, message='No user in request')
        try:
            command = request.address[request.index]
        except IndexError:
            raise AddressError(code=1001, message='The address does not contain a command.')

        # ------------------------------------------------------------------------------------

        # todo jak się robi zapytania :
        #  można sobie to obudoawać w jakąś metodę lokalnie jeśli będzie wygodniej
        #  pamiętajmy że uzywamy starego rare api które udostępnia masę opcji i zapytania idą przez cache więc
        #  jak chcemy ominąc to trzeba odpowiednio ustawić parametry
        #address_to_alpaca = f"{self.rs.get_resource(self.rs.MOUNT).adr}.azimuth"  # <--- tak bierzemy adres do Mounta i doklejamy do niebo polecenie np azimuth
        #address_to_alpaca_camera = f"{self.rs.get_resource(self.rs.CAMERA, nr=1).adr}.azimuth"  # <--- tak bierzemy adres do Mounta i doklejamy do niebo polecenie np azimuth
        # result = self.api.get_async(address=address_to_alpaca, ...) # <--- tak używamy api
        # todo ważana sprawa to żeby nie wbijać na stałe jakichś danych, jeśli jest coś konkretnego potrzebne to
        #  zapytac ernesta bo bardzo prawdopodobne że jest na to sposób np: jakąś nazwę teleskopu albo
        #  coś z konfiguracj wziąść ( działamy po stronie servera więc konfiguracja jest tutaj najświeższa ni niema
        #  sensu pytać NATS - szkoda wydajność marnować)

        # todo ---- poniżej ifologia z metodami jeśli jest skompplikowana logika to należy ją wyrzucić do osobnej metody
        #  żeby poniższy kod był czytelny ----  + wszystkie metody wpisać do domumętacji na góże

        if command == 'status' and request_type == 'GET':
            return Value(v=self.gh.status, ts=time.time())

        if command == 'exposure' and request_type == 'GET':
            return Value(v=self.gh.exposure, ts=time.time())

        if command == 'exposure' and request_type == 'PUT':
            try:
                self.gh.exposure = request.request_data['exp']
            except KeyError:
                raise TreeOtherError(code=4007,
                                     message=f'{self.get_name()}: Missing "exp" parameter in {command} request.')
            return Value(v=self.gh.status, ts=time.time())

        if command == 'period' and request_type == 'GET':
            return Value(v=self.gh.period, ts=time.time())

        if command == 'period' and request_type == 'PUT':
            try:
                self.period = request.request_data['period']
            except KeyError:
                raise TreeOtherError(code=4007,
                                     message=f'{self.get_name()}: Missing "period" parameter in {command} request.')
            return Value(v=True, ts=time.time())

        if command == 'calib_step' and request_type == 'GET':
            return Value(v=self.gh.calib_step, ts=time.time())

        if command == 'calib_step' and request_type == 'PUT':
            try:
                self.calib_step = request.request_data['step']
            except KeyError:
                raise TreeOtherError(code=4007,
                                     message=f'{self.get_name()}: Missing "step" parameter in {command} request.')
            return Value(v=True, ts=time.time())

        if command == 'preview' and request_type == 'PUT':
            try:
                single = request.request_data['single']
            except KeyError:
                raise TreeOtherError(code=4007,
                                     message=f'{self.get_name()}: Missing "single" parameter in {command} request.')
            await self.gh.preview(single=single)
            return Value(v=True, ts=time.time())

        if command == 'stop' and request_type == 'PUT':
            await self.gh.stop()
            return Value(v=True, ts=time.time())

        if command == 'start_calib' and request_type == 'PUT':
            await self.gh.calibration()
            return Value(v=True, ts=time.time())

        if command == 'start_guiding' and request_type == 'PUT':
            try:
                star_select = request.request_data['star_select']
            except KeyError:
                raise TreeOtherError(code=4007,
                                     message=f'{self.get_name()}: Missing "star_select" parameter in {command} request.')
            try:
                mount_follow = request.request_data['mount_follow']
            except KeyError:
                raise TreeOtherError(code=4007,
                                     message=f'{self.get_name()}: Missing "mount_follow" parameter in {command} request.')
            await self.gh.guiding(star_select=star_select, mount_follow=mount_follow)
            return Value(v=True, ts=time.time())

        if command == 'dark' and request_type == 'PUT':
            try:
                sub_no = request.request_data['sub_no']
            except KeyError:
                raise TreeOtherError(code=4007,
                                     message=f'{self.get_name()}: Missing "sub_no" parameter in {command} request.')
            await self.gh.dark(sub_no=sub_no)
            return Value(v=True, ts=time.time())

        if command == 'calib_data' and request_type == 'GET':
            return Value(v=self.gh.calib_data, ts=time.time())

        raise AddressError(code=1002, message=f'Unrecognised method for module {self.get_name()}')

    async def run(self):
        self.rs = await self._target_alpaca.get_res_manager()
        self.gh.rs = self.rs
        self.gh.api = self.api
        self.gh.nats = self.target_nats
        self.task = asyncio.create_task(self.gh.run_body())
        await super().run()

    async def stop(self):
        self.task.cancel()
        await super().stop()
