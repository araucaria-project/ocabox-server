import asyncio
import logging
import os
from typing import Iterable, Callable, Tuple, Dict
import confuse

from obsrv.protocols.alpaca.alpaca_connector import Connector
from obcom.data_colection.coded_error import TreeOtherError, TreeStructureError
from obcom.data_colection.value import TreeValueError

logger = logging.getLogger(__name__.rsplit('.')[-1])

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'iris_ccd_config.yml')

# Socket-level failures that indicate the device is unreachable.
# Surfaced as TreeOtherError(4005, NORMAL) so cycle-query subscribers
# self-recover via ErrorPolicy.SERVICE staged-backoff retries when the
# device returns. NORMAL (not TEMPORARY) because ECONNREFUSED on TCP
# is a sustained device-offline state, not a single missed-poll blip
# — the operator should have visibility into a multi-day outage, and
# SERVICE preset's throttled logging gives that without the silent
# retry-forever pathology.
_TEMPORARY_IO_ERRORS = (ConnectionError, BrokenPipeError, OSError,
                        asyncio.TimeoutError, TimeoutError)

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
        self._endpoints = {} # Map: address -> (transport, protocol)
        self._locks = {}     # Map: address -> asyncio.Lock
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

    async def _get_endpoint(self, address: str):
        if address not in self._locks:
            self._locks[address] = asyncio.Lock()
        
        async with self._locks[address]:
            if address in self._endpoints:
                return self._endpoints[address]
            
            try:
                host, port_str = address.split(':')
                port = int(port_str)
                loop = asyncio.get_running_loop()
                # Create a protocol with a dummy future initially
                protocol = IrisCcdProtocol(asyncio.Future())
                transport, protocol = await loop.create_datagram_endpoint(
                    lambda: protocol,
                    remote_addr=(host, port)
                )
                self._endpoints[address] = (transport, protocol)
                logger.info(f"UDP endpoint created for {address}")
                return transport, protocol
            except Exception as e:
                logger.error(f"Failed to connect UDP to {address}: {e}")
                raise

    async def _execute_command(self, address: str, command_str: str) -> str:
        # Get endpoint first to ensure locks are created
        transport, protocol = await self._get_endpoint(address)
        
        # Lock per address to ensure sequential request-response on the same socket
        async with self._locks[address]:
            if not transport or transport.is_closing():
                 # Reconnect logic if needed, simplistically removing from cache
                 if address in self._endpoints: del self._endpoints[address]
                 transport, protocol = await self._get_endpoint(address)

            try:
                response_future = asyncio.get_running_loop().create_future()
                # Update future in the protocol instance
                protocol.response_future = response_future
                
                command_bytes = command_str.encode('utf-8')
                packet_to_send = command_bytes.ljust(self._packet_size, b'\0')
                
                logger.debug(f"IRIS CCD OUT ({address}) >>> {command_str}")
                transport.sendto(packet_to_send)
                
                data = await asyncio.wait_for(response_future, timeout=self._timeout)
                response = data.split(b'\0', 1)[0].decode('utf-8')
                logger.debug(f"IRIS CCD IN ({address}) <<< {response}")

                if "OKAY" in response:
                    # Znajdujemy pozycję słowa OKAY i zwracamy wszystko, co po nim występuje
                    index = response.find("OKAY")
                    return response[index + 4:].strip()
                else:
                    raise RuntimeError(f"IRIS CCD error: {response}")

            except asyncio.TimeoutError:
                logger.error(f"IRIS CCD command '{command_str}' timed out.")
                # Force reconnect on timeout to be safe
                if address in self._endpoints: del self._endpoints[address]
                raise TimeoutError("IRIS CCD did not respond in time.")
            except Exception as e:
                logger.error(f"Error during IRIS CCD command: {e}")
                raise

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        address = component.get_option_recursive('address')
        if not address:
             logger.error(f"No address for component {component.sys_id}")
             return None
             
        try:
            command_def = self._command_map[component.kind][variable]
        except KeyError:
            raise TreeStructureError(
                code=3002,
                message=f"Method {variable!r} is not implemented on {component.kind}",
                severity=TreeStructureError.SEVERITY_CRITICAL,
            ) from None

        try:
            command_base = command_def.get('command')
            if command_base is None:
                raise TreeStructureError(
                    code=3002,
                    message=f"Malformed command definition for {variable!r} on {component.kind}: missing 'command' key",
                    severity=TreeStructureError.SEVERITY_CRITICAL,
                )
            get_arg = command_def.get('get_arg')
            if get_arg:
                command = f"{command_base} {get_arg}"
            else:
                command = command_base
            
            # 1. Pobieramy surowy tekst z kamery
            raw_response = await self._execute_command(address, command)
            
            # 2. TŁUMACZENIE STANU KAMERY NA STANDARD ALPACA
            if component.kind == 'camera' and variable == 'camerastate':
                resp_upper = raw_response.upper()
                if "EXPOS" in resp_upper:
                    return 2  # CameraExposing
                elif "WAIT" in resp_upper:
                    return 1  # CameraWaiting
                elif "READ" in resp_upper:
                    return 3  # CameraReading
                elif "DOWNLOAD" in resp_upper:
                    return 4  # CameraDownload
                elif "ERROR" in resp_upper or "FAIL" in resp_upper:
                    return 5  # CameraError
                else:
                    # Dla "OK" i wszelkich innych statusów spoczynkowych
                    return 0  # CameraIdle
            
            # 3. Zwracamy odpowiedź (jeśli to nie jest camerastate, zwróci tekst)
            return raw_response

        except _TEMPORARY_IO_ERRORS as e:
            # Device unreachable — sustained external state. Surface as
            # 4005 NORMAL so cycle-query subscribers self-recover via
            # ErrorPolicy.SERVICE retries when the device returns.
            raise TreeOtherError(address=None, code=4005,
                                 message=f"IRIS CCD unreachable on GET {component.kind}.{variable}: {e}",
                                 severity=TreeOtherError.SEVERITY_NORMAL) from e
        except RuntimeError as e:
            # Device replied with a non-OKAY response (raised in _execute_command).
            # Real instrument-state error — surface as 2002 NORMAL so the client
            # sees it but can still retry per its ErrorPolicy.
            raise TreeValueError(address=None, code=2002,
                                 message=f"IRIS CCD device error on GET {component.kind}.{variable}: {e}",
                                 severity=TreeValueError.SEVERITY_NORMAL) from e

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        address = component.get_option_recursive('address')
        if not address:
             return {"status": "failed", "error": "No address"}

        try:
            command_def = self._command_map[component.kind][variable]
        except KeyError:
            raise TreeStructureError(
                code=3002,
                message=f"Method {variable!r} is not implemented on {component.kind}",
                severity=TreeStructureError.SEVERITY_CRITICAL,
            ) from None

        try:
            command_base = command_def.get('command')
            if command_base is None:
                raise TreeStructureError(
                    code=3002,
                    message=f"Malformed command definition for {variable!r} on {component.kind}: missing 'command' key",
                    severity=TreeStructureError.SEVERITY_CRITICAL,
                )
            if not data:
                return {"status": "failed", "error": "Missing input value."}
            value = list(data.values())[0]
            command = f"{command_base} {value}"
            response = await self._execute_command(address, command)
            return {"status": "ok", "response": response}
        except _TEMPORARY_IO_ERRORS as e:
            raise TreeOtherError(address=None, code=4005,
                                 message=f"IRIS CCD unreachable on PUT {component.kind}.{variable}: {e}",
                                 severity=TreeOtherError.SEVERITY_NORMAL) from e
        except RuntimeError as e:
            raise TreeValueError(address=None, code=2002,
                                 message=f"IRIS CCD device error on PUT {component.kind}.{variable}: {e}",
                                 severity=TreeValueError.SEVERITY_NORMAL) from e

    async def call(self, component: 'Component', function: str, **data):
        address = component.get_option_recursive('address')
        if not address:
             return {"status": "failed", "error": "No address"}

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
                
                last_response = await self._execute_command(address, command)
            return {"status": f"action_{function}_completed", "response": last_response}
        except _TEMPORARY_IO_ERRORS as e:
            raise TreeOtherError(address=None, code=4005,
                                 message=f"IRIS CCD unreachable on CALL {function}: {e}",
                                 severity=TreeOtherError.SEVERITY_NORMAL) from e
        except RuntimeError as e:
            raise TreeValueError(address=None, code=2002,
                                 message=f"IRIS CCD device error on CALL {function}: {e}",
                                 severity=TreeValueError.SEVERITY_NORMAL) from e

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        pass