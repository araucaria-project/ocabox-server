import asyncio
import logging
import os
import json
from typing import Iterable, Callable, Tuple, Dict
import confuse
import nats

from obsrv.protocols.alpaca.alpaca_connector import Connector
from obcom.data_colection.coded_error import TreeOtherError, TreeStructureError

logger = logging.getLogger(__name__.rsplit('.')[-1])

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'iris_ccd_config.yml')

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
        self._endpoints = {} 
        self._locks = {}     
        
        self._nats_nc = None
        self._nats_connected = False
        self._nats_lock = asyncio.Lock()
        self._nats_data = {}
        
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
            
            self._nats_url = config['settings']['nats_url'].get(str)
            self._nats_subject = config['settings']['nats_subject'].get(str)
            self._nats_map = self._command_map.get('nats', {})
            
            logger.info("IRIS CCD configuration loaded successfully.")
        except (confuse.ConfigReadError, FileNotFoundError) as e:
            logger.error(f"CRITICAL: Could not read IRIS CCD config file. Error: {e}")
            raise RuntimeError("IRIS CCD connector configuration is missing or corrupted.") from e

    async def _nats_message_handler(self, msg):
        try:
            payload = json.loads(msg.data.decode('utf-8'))
            measurements = payload.get("data", {}).get("measurements", {})
            
            for var_name, var_conf in self._nats_map.items():
                expected_subject = var_conf.get('subject', self._nats_subject)
                
                if msg.subject == expected_subject:
                    key = var_conf.get('key')
                    if key and key in measurements:
                        self._nats_data[var_name] = float(measurements[key])
                        logger.debug(f"NATS [{msg.subject}]: Update '{var_name}' with key '{key}' -> {self._nats_data[var_name]}")
                        
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from NATS: {e}")
        except ValueError as e:
            logger.error(f"Error converting NATS value to float: {e}")
        except Exception as e:
            logger.error(f"Unexpected error while parsing NATS telemetry: {e}")

    async def _ensure_nats_connected(self):
        async with self._nats_lock:
            if not self._nats_connected:
                try:
                    self._nats_nc = await nats.connect(self._nats_url)
                    
                    subjects_to_subscribe = set()
                    if self._nats_subject:
                        subjects_to_subscribe.add(self._nats_subject)
                    for var_conf in self._nats_map.values():
                        sub = var_conf.get('subject', self._nats_subject)
                        if sub:
                            subjects_to_subscribe.add(sub)
                    
                    for sub in subjects_to_subscribe:
                        await self._nats_nc.subscribe(sub, cb=self._nats_message_handler)
                        logger.info(f"NATS: Subscribed to subject: {sub}")
                        
                    self._nats_connected = True
                    logger.info(f"Successfully connected to NATS server ({self._nats_url})")
                except Exception as e:
                    logger.error(f"Error connecting to NATS: {e}")
                    raise

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

    def _drop_endpoint(self, address: str) -> None:
        """Remove a cached UDP endpoint and close its transport.

        The transport owns the underlying socket FD; dropping the cache entry
        without closing it orphans the socket (asyncio keeps it registered with
        the event loop), leaking one FD per timeout/reconnect. With the device
        unreachable that leaks continuously until RLIMIT_NOFILE is exhausted.
        """
        entry = self._endpoints.pop(address, None)
        if entry is not None:
            transport, _protocol = entry
            try:
                transport.close()
            except Exception:
                pass

    async def _execute_command(self, address: str, command_str: str) -> str:
        transport, protocol = await self._get_endpoint(address)
        
        async with self._locks[address]:
            if not transport or transport.is_closing():
                 # Reconnect logic: close + drop the stale endpoint before recreating
                 self._drop_endpoint(address)
                 transport, protocol = await self._get_endpoint(address)

            try:
                response_future = asyncio.get_running_loop().create_future()
                protocol.response_future = response_future
                
                command_bytes = command_str.encode('utf-8')
                packet_to_send = command_bytes.ljust(self._packet_size, b'\0')
                
                logger.debug(f"IRIS CCD OUT ({address}) >>> {command_str}")
                transport.sendto(packet_to_send)
                
                data = await asyncio.wait_for(response_future, timeout=self._timeout)
                response = data.split(b'\0', 1)[0].decode('utf-8')
                logger.debug(f"IRIS CCD IN ({address}) <<< {response}")

                if "OKAY" in response:
                    index = response.find("OKAY")
                    return response[index + 4:].strip()
                else:
                    raise RuntimeError(f"IRIS CCD error: {response}")

            except asyncio.TimeoutError:
                logger.error(f"IRIS CCD command '{command_str}' timed out.")
                # Force reconnect on timeout to be safe — close the transport so the
                # UDP socket FD is released, not just dropped from the cache.
                self._drop_endpoint(address)
                raise TimeoutError("IRIS CCD did not respond in time.")
            except Exception as e:
                logger.error(f"Error during IRIS CCD command: {e}")
                raise

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        if variable in self._nats_map:
            try:
                await self._ensure_nats_connected()
            except Exception as e:
                raise TreeOtherError(address=None, code=4005,
                                     message=f"NATS connection failed: {e}",
                                     severity=TreeOtherError.SEVERITY_NORMAL) from e
                                     
            if variable not in self._nats_data:
                raise TreeOtherError(address=None, code=4005,
                                     message=f"NATS telemetry received no data for {variable!r} yet.",
                                     severity=TreeOtherError.SEVERITY_NORMAL)
                                     
            return self._nats_data[variable]

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
            
            raw_response = await self._execute_command(address, command)
            
            if component.kind == 'camera' and variable == 'camerastate':
                resp_upper = raw_response.upper()
                if "EXPOS" in resp_upper: return 2
                elif "WAIT" in resp_upper: return 1
                elif "READ" in resp_upper: return 3
                elif "DOWNLOAD" in resp_upper: return 4
                elif "ERROR" in resp_upper or "FAIL" in resp_upper: return 5
                else: return 0
            
            return raw_response

        except _TEMPORARY_IO_ERRORS as e:
            raise TreeOtherError(address=None, code=4005,
                                 message=f"IRIS CCD unreachable on GET {component.kind}.{variable}: {e}",
                                 severity=TreeOtherError.SEVERITY_NORMAL) from e
        except RuntimeError as e:
            # Device replied with a non-OKAY response (raised in _execute_command).
            # The connector worked; the instrument reported a fault — surface as
            # 4009 NORMAL so the client tells it apart from a TIC-internal failure.
            raise TreeOtherError(address=None, code=4009,
                                 message=f"IRIS CCD device error on GET {component.kind}.{variable}: {e}",
                                 severity=TreeOtherError.SEVERITY_NORMAL) from e

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
            raise TreeOtherError(address=None, code=4009,
                                 message=f"IRIS CCD device error on PUT {component.kind}.{variable}: {e}",
                                 severity=TreeOtherError.SEVERITY_NORMAL) from e

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
            raise TreeOtherError(address=None, code=4009,
                                 message=f"IRIS CCD device error on CALL {function}: {e}",
                                 severity=TreeOtherError.SEVERITY_NORMAL) from e

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        pass