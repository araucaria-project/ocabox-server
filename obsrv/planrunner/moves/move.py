import time

from obcom.comunication.comunication_error import CommunicationRuntimeError, CommunicationTimeoutError
from obsrv.data_colection.resource_manager.errors import ResourceCommandError
from obsrv.planrunner.base_command import BaseCommand


class Move(BaseCommand):
    _NAME = "MOVE"

    def __init__(self, plan_data, virtual_move=False):
        super().__init__(plan_data=plan_data)
        self.positions = []
        self._actn = None
        self._virtual_move = virtual_move

    async def watch(self):
        for p in self.positions:
            await p.run(api=self.api)

    @property
    def error(self):
        for p in self.positions:
            if p.done() and p.exception():
                return True
        return False

    @property
    def error_content(self):
        for p in self.positions:
            if p.done() and p.exception():
                return str(p.exception())
        return ""

    @property
    def progress(self):
        """
        Property representing summ progress of all positions

        :raise ValueError
        :return:
        """
        # virtual move is always done
        if self._virtual_move:
            return 1
        # todo dodać możliwośc subskrypcji na progres, przemyśleć dodanie Param klasy albo jakieś Eventy
        suma = 0
        for p in self.positions:
            suma += p.progress  # can raise ValueError
        return suma / len(self.positions)

    def done(self):
        for p in self.positions:
            if not p.done():
                return False
        return True

    async def wait(self):
        for p in self.positions:
            await p.wait()

    def add_position(self, position):
        # this should be before add given position to list
        self._set_in_order(position=position)
        self.positions.append(position)

    def _set_in_order(self, position):
        """This should be before add given position to list"""
        for p in self.positions:
            if p.order < position.order:
                position.add_more_important_position(p)
            elif p.order > position.order:
                p.add_more_important_position(position)

    def set_action(self, action):
        self._actn = action

    async def run(self):
        ts = time.time()
        # set start timestamp
        for p in self.positions:
            p.start_timestamp = ts
        if self._virtual_move:
            return
        try:
            if self._actn is not None:
                await self._actn
            else:
                await self._action()
        except NotImplementedError:
            raise ResourceCommandError(f"Method action is not implemented")
        except ResourceCommandError:
            raise

    async def _action(self):
        raise NotImplementedError

    async def a_init(self):
        pass

    async def _get_request_safe(self, address: str, timeout=None):
        """

        :param address: address
        :raise ResourceCommandError:
        :return: value
        """
        try:
            result = await self.api.get_async(address=address,
                                              parameters_dict={}, request_timeout=timeout)
        except (CommunicationRuntimeError, CommunicationTimeoutError) as e:
            raise ResourceCommandError(f"Move has error {str(e)}")
        if not result.status:
            raise ResourceCommandError(f"Move has error status in response {result.error}")
        if result.value is None:
            raise ResourceCommandError(f"Move try to get value from alpaca but no value was return {result.error}")
        return result.value.v

    async def reset(self):
        for p in self.positions:
            await p.reset()

    def cancel(self):
        for p in self.positions:
            p.cancel()
