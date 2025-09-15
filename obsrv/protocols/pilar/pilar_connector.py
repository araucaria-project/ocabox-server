import asyncio
import logging
import ssl
import os
from typing import Iterable, Callable, Tuple, Dict
import confuse

from obsrv.protocols.alpaca.alpaca_connector import Connector
from obsrv.telescope_devices.standard_components import StandardTelescopeComponents

logger = logging.getLogger(__name__.rsplit('.')[-1])

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'pilar_config.yaml')

class PilarConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    async def execute(self, cmd_id: int, command_str: str, timeout: float) -> str:
        full_command = f"{cmd_id} {command_str}\n"
        peername = self.writer.get_extra_info('peername')
        logger.debug(f"Pilar OUT on {peername} [ID:{cmd_id}] >>> {command_str}")
        self.writer.write(full_command.encode('utf-8'))
        await self.writer.drain()
        value_to_return = None
        while True:
            line_bytes = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
            if not line_bytes:
                raise ConnectionAbortedError("Pilar connection closed unexpectedly.")
            response_line = line_bytes.decode('utf-8').strip()
            logger.debug(f"Pilar IN on {peername} [ID:{cmd_id}] <<< {response_line}")
            if response_line.startswith(f"{cmd_id} ") and "=" in response_line:
                value_to_return = response_line.split("=", 1)[1].strip()
            if response_line.startswith(f"{cmd_id} COMMAND COMPLETE"):
                return value_to_return if value_to_return is not None else "OK"
            if response_line.startswith(f"{cmd_id} COMMAND FAILED"):
                raise RuntimeError(f"Pilar command failed: {command_str}")


class PilarConnector(Connector):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._load_config()
        self._connection_pool: asyncio.Queue[PilarConnection] = asyncio.Queue()
        self._id_pool: asyncio.Queue[int] = asyncio.Queue()
        self._resource_locks: Dict[str, asyncio.Lock] = {
            resource_name: asyncio.Lock() for resource_name in set(self._resource_lock_map.values())
        }
        self._host = None
        self._port = None
        self._connected = False
        logger.info(f'Pilar advanced connector created')

    def _load_config(self):
        logger.info(f"Loading Pilar configuration from: {CONFIG_PATH}")
        try:
            config = confuse.Configuration('PilarConnector', __name__)
            config.set_file(CONFIG_PATH)
            self._pool_size = config['settings']['connection_pool_size'].get(int)
            id_range_list = config['settings']['id_pool_range'].get(list)
            self._id_range = (id_range_list[0], id_range_list[1])
            self._timeouts = {
                "connection": config['settings']['timeouts']['connection'].get(float),
                "get": config['settings']['timeouts']['get_command'].get(float),
                "set": config['settings']['timeouts']['set_command'].get(float),
                "pool_get": config['settings']['timeouts']['pool_get'].get(float),
            }
            self._command_map = config['mappings']['commands'].get(dict)
            self._resource_lock_map = config['mappings']['resource_locks'].get(dict)
            self._actions_map = config['actions'].get(dict)
            logger.info("Pilar configuration loaded successfully.")
        except (confuse.ConfigReadError, FileNotFoundError) as e:
            logger.error(f"CRITICAL: Could not read Pilar config file at {CONFIG_PATH}. Error: {e}")
            raise RuntimeError("Pilar connector configuration is missing or corrupted.") from e

    async def _create_pooled_connection(self) -> PilarConnection:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeouts['connection']
            )
            welcome_message = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            if b"TLS" in welcome_message:
                writer.write(b"ENC TLS\n")
                await writer.drain()
                enc_response = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if b"ENC OK" not in enc_response:
                    raise ConnectionError("Pilar TLS negotiation failed.")
                sc = ssl.create_default_context()
                await writer.start_tls(sc)
            return PilarConnection(reader, writer)
        except Exception as e:
            logger.error(f"Failed to create a pooled connection: {e}")
            return None

    async def connect(self, host: str, port: int):
        if self._connected: return True
        self._host, self._port = host, port
        for i in range(self._id_range[0], self._id_range[1] + 1):
            await self._id_pool.put(i)
        creation_tasks = [self._create_pooled_connection() for _ in range(self._pool_size)]
        connections = await asyncio.gather(*creation_tasks)
        for conn in connections:
            if conn: await self._connection_pool.put(conn)
        if self._connection_pool.full():
            self._connected = True
            logger.info(f"Pilar connector is now online.")
            return True
        else:
            await self.disconnect()
            return False

    async def disconnect(self):
        if not self._connected and self._connection_pool.empty(): return
        self._connected = False
        while not self._connection_pool.empty():
            conn = self._connection_pool.get_nowait()
            if conn.writer and not conn.writer.is_closing():
                conn.writer.close()
                await conn.writer.wait_closed()
        logger.info("Pilar connection pool closed.")

    async def _execute_command_safely(self, pilar_cmd_variable: str, command_str: str, timeout: float):
        resource_name = self._resource_lock_map.get(pilar_cmd_variable)
        lock = self._resource_locks.get(resource_name) if resource_name else None
        if lock:
            logger.debug(f"Waiting to acquire lock for resource: {resource_name}")
            async with lock:
                logger.debug(f"Lock acquired for resource: {resource_name}")
                return await self._execute_on_pooled_connection(command_str, timeout)
        else:
            return await self._execute_on_pooled_connection(command_str, timeout)

    async def _execute_on_pooled_connection(self, command_str: str, timeout: float):
        if not self._connected: raise ConnectionError("Connector not connected.")
        conn, cmd_id = None, None
        try:
            cmd_id = await asyncio.wait_for(self._id_pool.get(), timeout=self._timeouts['pool_get'])
            conn = await asyncio.wait_for(self._connection_pool.get(), timeout=self._timeouts['pool_get'])
            return await conn.execute(cmd_id, command_str, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("No available connection or ID in the pool.")
        finally:
            if conn: await self._connection_pool.put(conn)
            if cmd_id: await self._id_pool.put(cmd_id)

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        try:
            pilar_cmd = self._command_map[component.kind][variable]
            command = f"GET {pilar_cmd}"
            return await self._execute_on_pooled_connection(command, timeout=self._timeouts['get'])
        except Exception as e:
            logger.error(f"Pilar GET failed for {component.kind}.{variable}: {e}")
            return None

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        try:
            pilar_cmd = self._command_map[component.kind][variable]
            value = list(data.values())[0]
            command = f"SET {pilar_cmd}={value}"
            await self._execute_command_safely(pilar_cmd, command, timeout=self._timeouts['set'])
            return {"status": "ok", "value_set": value}
        except Exception as e:
            logger.error(f"Pilar PUT failed for {component.kind}.{variable}: {e}")
            return {"status": "failed", "error": str(e)}

    async def call(self, component: 'Component', function: str, **data):
        action_steps = self._actions_map.get(function)
        if not action_steps:
            logger.warning(f"Unknown Pilar action called: {function}")
            return {"status": "unknown_function"}
        logger.info(f"Executing Pilar action: {function} with data {data}")
        try:
            for step in action_steps:
                step_component_str = step['component']
                step_variable = step['variable']
                step_value = step['value']
                if isinstance(step_value, str) and step_value.startswith('{') and step_value.endswith('}'):
                    arg_name = step_value.strip('{}')
                    if arg_name not in data:
                        raise ValueError(f"Missing argument '{arg_name}' for action '{function}'")
                    final_value = data[arg_name]
                else:
                    final_value = step_value
                await self.put(component, step_variable, **{step_variable: final_value})
            return {"status": f"action_{function}_completed"}
        except Exception as e:
            logger.error(f"Pilar CALL failed for action {function}: {e}")
            return {"status": "failed", "error": str(e)}

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        logger.warning("Pilar protocol does not support subscriptions.")
        pass