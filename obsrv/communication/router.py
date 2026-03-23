import asyncio
import logging
import zmq
from typing import List
from zmq.asyncio import Poller
from obcom.comunication.base_zmq_communication_object import BaseZmqCommunicationObject
from obsrv.communication.base_request_solver import BaseRequestSolver
from obcom.comunication.comunication_error import CommunicationTimeoutError
from obcom.comunication.message_serializer import MessageSerializer
from obcom.comunication.multipart_structure import MultipartStructure
from obsrv.communication.base_router_with_config import BaseRouterWithConfig
from obsrv.utils.asyncio_util_functions import wait_for_psce

logger = logging.getLogger(__name__.rsplit('.')[-1])


class Router(BaseRouterWithConfig):
    DEFAULT_NAME = 'DefaultRouter'
    TYPE = 'router'

    def __init__(self, request_solver: BaseRequestSolver or None, name: str = None, port: int = None, **kwargs):
        super().__init__(name=name, port=port, **kwargs)
        self._port = self._port if self._port is not None else self.get_cfg('port')  # rewrite port from config
        if not self._port or not isinstance(self._port, int):
            logger.error(f"Can not get correct port ({self._port}) for {self.TYPE}")
            raise RuntimeError
        # request solver
        self.request_solver: BaseRequestSolver or None = request_solver
        # OMQ
        self._front_socket = self.context.socket(zmq.ROUTER)
        # IMPORTANT Pending messages shall be discarded immediately when the socket is closed
        self._front_socket.setsockopt(zmq.LINGER, 0)
        address = f"{self._get_cfg('protocol', 'tcp')}://{self._get_cfg('url', '*')}:{self._port}"
        logger.info(f"Router start on: {address}")
        try:
            self._front_socket.bind(address)
        except zmq.error.ZMQError:
            logger.error(f"Can not start router because address {address} is already in use")
            raise RuntimeError(f"Can not start router because address {address} is already in use")
        # Tasks
        self._main_task_name = f'{self.name}_main_task'
        self._main_task = None
        self._echo_task_name = f'{self.name}_echo_task'
        self._echo_task = None
        self._message_task_name = f'{self.name}_message_task'
        self._message_tasks = []
        # self._stop_task_name = f'{self.name}_stop_task'
        self._stop_task = None
        # async loop
        self._current_loop = None  # remember async loop with router working

    async def _echo(self):
        enabled = self._get_cfg('echo-task-enabled', True)
        if not enabled:
            logger.info('Echo task disabled in config, stopping')
            return
        delay = self._get_cfg('echo-task-interval', 1.0)
        logger.info(f'Ping task interval: {delay:.2f}s')
        while True:
            await asyncio.sleep(delay)
            logger.info(f'{self.name}: Listening...')

    async def _solve_request(self, ms: MultipartStructure) -> List[bytes] or None:
        answer = None
        if ms.service_msg_bool:
            answer = await self._get_answer(ms)
        else:
            if self.request_solver:
                zmq_id_ = ms.prefix_data[0] if ms.prefix_size > 0 else b''
                answer = await self.request_solver.get_answer(ms.data, zmq_id_, timeout=ms.request_timeout_float)
            else:
                logger.error(f"Can not find request solver. The router can't send response to client.")
        return answer

    async def _get_answer(self, ms: MultipartStructure) -> List[bytes]:

        response = []
        # TODO In the future, when the number of commands increases, think about a class instead of a dictionary
        r = None
        if ms.data and isinstance(ms.data, list) and len(ms.data) > 0:
            message_dict = MessageSerializer.from_bytes(ms.data[0])
            order = message_dict.get("command")
            if order is not None and order == "is_alive":
                r = {"command": order, "response": True}
            if order is not None and order == "reload_config":
                result = await self.request_solver.reload_nats_config()
                r = {"command": order, "response": result}
        resp = MessageSerializer.pack_b(r)
        response.append(resp)

        return response

    @staticmethod
    def _open_envelope(multipart: List[bytes]) -> MultipartStructure:
        ms = MultipartStructure(multipart, 1)
        try:
            ms.validate()
        except ValueError as e:
            logger.error(e)
            raise
        target = ms.prefix_data
        if not target:
            logger.error(f'Wrong port address in multipart.')
            raise ValueError
        return ms

    @staticmethod
    def _pack_to_envelope(target: List[bytes], create_time: bytes, msg_id: bytes, request_timeout: bytes,
                          service_msg: bytes, data_b: list) -> MultipartStructure:
        if not target:
            logger.error(f'Wrong client address for multipart.')
            raise ValueError
        if not data_b:
            data_b = [b'']
            logger.warning(f'No data to send. Task send empty bit to client. ')
        multipart = MultipartStructure.from_parts(create_time=create_time, id_=msg_id, data=data_b,
                                                  request_timeout=request_timeout, service_msg=service_msg,
                                                  prefix_data=target)
        multipart.validate()
        return multipart

    async def _send_back(self, message):
        def remove_task_inner():
            try:
                task = asyncio.current_task()
                self._message_tasks.remove(task)
            except ValueError:
                logger.error('Unexpected ValueError. Can not remove task from task list.')

        try:
            ms = self._open_envelope(message)
        except ValueError:
            # Don't answer for incorrect requests. Close task.
            remove_task_inner()
            return
        try:
            time_to_expire = self._get_time_to_expire(ms=ms, use_default=True)
        except CommunicationTimeoutError as e:
            remove_task_inner()
            logger.error(e.message)
            return
        try:
            answer = await wait_for_psce(self._solve_request(ms), timeout=time_to_expire)
        except ValueError:
            # Obsolete and shouldn't have happened
            # Don't answer for incorrect requests. Close task.
            remove_task_inner()
            logger.error(f"Router encountered a ValueError when try to solve request. Solver handled the exception "
                         f"incorrectly")
            return
        except asyncio.CancelledError:
            # remove canceled task from list
            remove_task_inner()
            raise
        except asyncio.TimeoutError:
            # remove to slow task from list
            remove_task_inner()
            logger.error(f"Handling the request has timed out. Stop handling this task.")
            return
        except Exception as e:
            # shouldn't have happened
            remove_task_inner()
            logger.error(f"Router encountered an unexpected error while generating response. Task was closed and "
                         f"don't send response. Error message: {str(e)}")
            return
        try:
            answer_multipart = Router._pack_to_envelope(ms.prefix_data, ms.create_time, ms.id_, ms.request_timeout,
                                                        ms.service_msg, answer)
        except ValueError:
            # Don't answer for incorrect requests. Close task.
            remove_task_inner()
            return
        self._front_socket.send_multipart(answer_multipart.multipart)
        logger.info("Send response to client")
        remove_task_inner()

    async def _main(self):
        # Poller() is only necessary for multiple sockets. Has been added here with thoughts about the future.
        poller = Poller()
        poller.register(self._front_socket, zmq.POLLIN)
        i = 0
        while True:
            i += 1
            events = await poller.poll()
            if self._front_socket in dict(events):
                message = await self._front_socket.recv_multipart()
                task = self._current_loop.create_task(self._send_back(message), name=self._message_task_name)
                self._message_tasks.append(task)

    def _start_main_task(self):
        for task in asyncio.all_tasks(self._current_loop):
            if task.get_name().startswith(self.name):
                logger.error(f'Router name conflict. Could not start router {self.name} because a router with that '
                             f'name is already running in the async loop.')
                raise ValueError
        logger.info('start main task')
        self._main_task = self._current_loop.create_task(self._main(), name=self._main_task_name)
        logger.info('start echo task')
        self._echo_task = self._current_loop.create_task(self._echo(), name=self._echo_task_name)

    def start(self, loop=None):
        """
        This method starts the router's event loop on the asynchronous task and runs it in the current async loop.
        It is possible to pass a previously created loop as an argument of the method. Otherwise, the currently
        running loop is downloaded.

        :param loop: async loop
        :return:
        """
        if not loop:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # 'RuntimeError: There is no current event loop...'
                loop = None
        if loop and not loop.is_closed():
            if not self._current_loop:
                self._current_loop = loop  # remember async loop with router working
            if not self.is_stopped():
                logger.error('Can not start router because is already workng.')
                return
            self._current_loop = loop  # assign again after checking 'is_stopped()'
            self._start_main_task()
            logger.info('Router is running')
        else:
            logger.error('Can not start router because async loop not found.')

    async def main_coroutine(self):
        """
        This method is coroutine responsible for starting the server and waiting for it to finish running and then
        shutting down all tasks if they were not terminated earlier. Warning! this method catches error
        asyncio.CancelledError and shuts down the server securely. Canceling it turns off the entire server.
        :return:
        """
        if not self.is_stopped():
            logger.error('Can not start router because is already working.')
            return
        self._current_loop = asyncio.get_running_loop()  # this is called from loop, so it is not possible to loop was
        # closed and throw exception
        self._start_main_task()
        mt = self._main_task  # keep the instance when await because self param can be set to None during waiting
        pt = self._echo_task  # keep the instance when await because self param can be set to None during waiting
        try:
            await mt
        except asyncio.CancelledError:
            # catch canceled main task and cancel all server task safety
            pass
        finally:
            # Stop the router after finishing main tasks, tasks are endless loops so only in case of any errors you may
            # need this piece of code.
            self.stop()
            await self.wait_for_stop()
            logger.info(f'Main coroutine for router {self.name} was end')

    def _stop_router_tasks(self):
        logger.info('cancelling router tasks...')
        task_count = 0
        loop = self._current_loop
        for task in asyncio.all_tasks(loop):
            if task is self._main_task and not task.cancelled():
                logger.info('cancel main task')
                task.cancel()
                task_count += 1
            if task is self._echo_task and not task.cancelled():
                logger.info('cancel ping task')
                task.cancel()
                task_count += 1
            if task.get_name() is self._message_task_name:
                logger.info('cancel client answer task')
                task.cancel()
                task_count += 1
        if task_count > 0:
            logger.info(f'Cancel {task_count} tasks. They exiting in a minute...')
        else:
            logger.warning(f'Can not find any task for cancel. Tasks are already canceled or finished')

    def stop(self):
        """
        This method is responsible for canceling all router tasks and then creating a special task that is to wait
        for all canceled previously to close and the server to shut down properly. shutdown job should never be
        canceled as this may lead to incorrect shutdown of the router.

        :return:
        """
        if self.is_stopped():
            logger.info(f'The router {self.name} is already stop')
            return
        logger.info('Stopping router...')
        if self._current_loop and not self._current_loop.is_closed():
            loop = self._current_loop
        else:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # 'RuntimeError: There is no current event loop...'
                logger.warning('Can not stop router because the running loop not found.')
                loop = None
        if loop:
            if self._stop_task:
                logger.info(f'Router {self.name} is already stopping now, please wait a moment for stopped.')
            else:
                self._stop_router_tasks()
                self._stop_task = loop.create_task(self._wait_for_stop())
        else:
            # TODO It should never happened. However, if it does, need to think about restarting the loop and end tasks
            logger.error(f'Router {self.name} is not stopped and can not find running loop. Something go wrong')
            raise RuntimeError

    async def _wait_for_stop(self):
        # --------------------------------- stop main -----------------------------------------------------
        if self._main_task:
            try:
                await self._main_task
            except asyncio.CancelledError:
                # here is checking if stop task was canceled, in normal situation it don't and should waiting for rest
                # of router task
                if self._stop_task.cancelled():
                    logger.error(f'Stop task for router {self.name} was canceled before cancel all router tasks')
                    raise asyncio.CancelledError
            if self._main_task not in asyncio.all_tasks():
                self._main_task = None
                logger.info(f'Main task for router named {self.name} stopped.')

        # --------------------------------- stop ping ----------------------------------------------------
        if self._echo_task:
            try:
                await self._echo_task
            except asyncio.CancelledError:
                # here is checking if stop task was canceled, in normal situation it don't and should waiting for rest
                # of router task
                if self._stop_task.cancelled():
                    logger.error(f'Stop task for router {self.name} was canceled before cancel all router tasks')
                    raise asyncio.CancelledError
            if self._echo_task not in asyncio.all_tasks():
                self._echo_task = None
                logger.info(f'Ping task for router named {self.name} stopped.')
            else:
                logger.error(f'Stop task for router {self.name} was canceled before cancel all router tasks')

        # --------------------------------- stop message ---------------------------------------------------
        for task in self._message_tasks:
            try:
                await task
            except asyncio.CancelledError:
                # here is checking if stop task was canceled, in normal situation it don't and should waiting for rest
                # of router task
                if self._stop_task.cancelled():
                    logger.error(f'Stop task for router {self.name} was canceled before cancel message task')
                    raise asyncio.CancelledError
            if task in asyncio.all_tasks():
                logger.info(f'Task {task.get_name} for router named {self.name} stopped.')
        self._message_tasks = []

        self._stop_task = None
        logger.info(f'Router {self.name} was stopped.')

    async def wait_for_stop(self):
        """
        This method is a corutin that waits for the server to shutdown properly after calling the 'stop()' method.
        Additionally, it checks the server shutdown process. Instead, you can use the 'get_stop_task()' method which
        returns a job instance to shutdown the router and wait for it to complete but it is not recommended.

        :return:
        """
        if self.is_stopped():
            logger.info(f'The router {self.name} is already stopped')
            return
        stop_task = self.get_stop_task()
        if not stop_task:
            logger.warning(f'The router {self.name} is running and a stop has not been initiated try run first .stop() '
                           f'method')
            return
        try:
            await stop_task
        except asyncio.CancelledError:
            logger.error(f'the job waiting for shutting down the router {self.name} was not completed correctly. '
                         f'The router may be canceled but not terminated.')
        if not self.is_stopped():
            logger.error(f'An attempt to turn off the router has been unsuccessful')
            raise RuntimeError  # probably newer happen

    def is_stopped(self):
        """
        This method returns true if the router is stopped.

        :return: true if router is stopped or false if don't.
        """
        try:
            if self._current_loop:
                tasks = [self._main_task, self._echo_task, *self._message_tasks, self._stop_task]
                for t in tasks:
                    if t and t in asyncio.all_tasks(self._current_loop):
                        return False
            # else:
            # logger.info(f'Can not find async loop for this router. Probably this router was newer started.')
        except AttributeError: # Router may not have _current_loop attribute when called from _del_ method
            pass
        return True

    def get_stop_task(self):
        """
        This method returns an instance of the task that waits for the router to shut down correctly, provided the
        'stop()' method has been run beforehand. If the stop () method has not been run before or the router has
        already been shut down properly, None will be returned. It is recommended to use the 'wait_for_stop()' instead
        this method.

        :return: asyncio task
        """
        return self._stop_task

    def __del__(self):
        if not self.is_stopped():
            self.stop()
        if self._front_socket:
            self._front_socket.close()
        super().__del__()
