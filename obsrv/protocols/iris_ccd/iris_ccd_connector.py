import asyncio
import logging
import os
from typing import Iterable, Callable, Tuple, Dict
import confuse

from obsrv.protocols.alpaca.alpaca_connector import Connector
from obsrv.telescope_devices.standard_components import StandardTelescopeComponents

logger = logging.getLogger(__name__.rsplit('.')[-1])

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'iris_ccd_config.yaml')

class IrisCcdProtocol(asyncio.DatagramProtocol):
    def __init__(self, response_future: asyncio.Future):
        super().__init__()
        self.response_future = response_future
        self.transport = None

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        if not self.response_future.done():
            self.response_future.set_result(data)

    def error_received(self, exc: Exception):
        if not self.response_future.done():
            self.response_future.set_exception(exc)

    def connection_lost(self, exc: Exception):
        if not self.response_future.done():
            self.response_future.set_exception(exc or ConnectionError("Connection lost"))

class IrisCcdConnector(Connector):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._load_config()
        self._lock = asyncio.Lock()
        self._transport = None
        self._protocol = None
        self._connected = False
        logger.info('IrisCcdConnector created')

    def _load_config(self):
        logger.info(f"Loading IRIS CCD configuration from: {CONFIG_PATH}")
        try:
            config = confuse.Configuration('IrisCcdConnector', __name__)
            config.set_file(CONFIG_PATH)
            self._packet_size = config['settings']['packet_size'].get(int)
            self._timeout = config['settings']['command_timeout'].get(float)
            self._command_map = config['mappings']['commands'].get(dict)
            self._actions_map = config['mappings']['actions'].get(dict)
            logger.info("IRIS CCD configuration loaded successfully.")
        except (confuse.ConfigReadError, FileNotFoundError) as e:
            logger.error(f"CRITICAL: Could not read IRIS CCD config file. Error: {e}")
            raise RuntimeError("IRIS CCD connector configuration is missing or corrupted.") from e

    async def connect(self, host: str, port: int):
        if self._connected:
            return True
        logger.info(f"Setting up UDP endpoint for IRIS CCD at {host}:{port}...")
        try:
            loop = asyncio.get_running_loop()
            self._transport, self._protocol = await loop.create_datagram_endpoint(
                lambda: IrisCcdProtocol(asyncio.Future()),
                remote_addr=(host, port)
            )
            self._connected = True
            logger.info("UDP endpoint for IRIS CCD is ready.")
            return True
        except (OSError, asyncio.TimeoutError) as e:
            logger.error(f"Failed to create UDP endpoint for IRIS CCD: {e}")
            return False

    async def disconnect(self):
        if not self._connected:
            return
        self._connected = False
        if self._transport:
            self._transport.close()
            self._transport = None
            self._protocol = None
        logger.info("IRIS CCD UDP endpoint closed.")

    async def _execute_command(self, command_str: str) -> str:
        async with self._lock:
            if not self._connected:
                raise ConnectionError("IRIS CCD connector is not connected.")
            
            try:
                command_bytes = command_str.encode('utf-8')
                packet_to_send = command_bytes.ljust(self._packet_size, b'\0')
                response_future = asyncio.get_running_loop().create_future()
                self._protocol.response_future = response_future
                logger.debug(f"IRIS CCD OUT >>> {command_str}")
                self._transport.sendto(packet_to_send)
                data = await asyncio.wait_for(response_future, timeout=self._timeout)
                response = data.split(b'\0', 1)[0].decode('utf-8')
                logger.debug(f"IRIS CCD IN <<< {response}")

                if response.startswith("**** OKAY"):
                    return response[10:].strip()
                else:
                    raise RuntimeError(f"IRIS CCD returned an error or unexpected response: {response}")

            except asyncio.TimeoutError:
                logger.error(f"IRIS CCD command '{command_str}' timed out after {self._timeout}s.")
                raise TimeoutError("IRIS CCD did not respond in time.")
            except Exception as e:
                logger.error(f"An error occurred during IRIS CCD command execution: {e}")
                await self.disconnect()
                raise

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        try:
            command_def = self._command_map[component.kind][variable]
            command_base = command_def['command']
            get_arg = command_def.get('get_arg')
            if get_arg:
                command = f"{command_base} {get_arg}"
            else:
                command = command_base
            return await self._execute_command(command)
        except (KeyError, TimeoutError, ConnectionError, RuntimeError) as e:
            logger.error(f"IRIS CCD GET failed for {component.kind}.{variable}: {e}")
            return None

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        try:
            command_def = self._command_map[component.kind][variable]
            command_base = command_def['command']
            value = list(data.values())[0]
            command = f"{command_base} {value}"
            response = await self._execute_command(command)
            return {"status": "ok", "response": response}
        except (KeyError, TimeoutError, ConnectionError, RuntimeError) as e:
            logger.error(f"IRIS CCD PUT failed for {component.kind}.{variable}: {e}")
            return {"status": "failed", "error": str(e)}

    async def call(self, component: 'Component', function: str, **data):
        action_steps = self._actions_map.get(function)
        if not action_steps:
            return {"status": "unknown_function"}
        logger.info(f"Executing IRIS CCD action: {function} with data {data}")
        try:
            last_response = None
            for step in action_steps:
                command_base = step['command']
                value_template = step.get('value')
                
                if value_template is None:
                    command = command_base
                elif isinstance(value_template, str) and value_template.startswith('{'):
                    arg_name = value_template.strip('{}')
                    if arg_name not in data:
                        raise ValueError(f"Missing argument '{arg_name}' for action '{function}'")
                    command = f"{command_base} {data[arg_name]}"
                else:
                    command = f"{command_base} {value_template}"
                
                last_response = await self._execute_command(command)
            return {"status": f"action_{function}_completed", "response": last_response}
        except Exception as e:
            logger.error(f"IRIS CCD CALL failed for action {function}: {e}")
            return {"status": "failed", "error": str(e)}

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        logger.warning("IRIS CCD protocol does not support subscriptions.")
        pass
