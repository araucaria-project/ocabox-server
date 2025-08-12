import asyncio
import ssl
import logging
from typing import Dict, List, Any

from obsrv.data_colection.base_api.connector import Connector
from obsrv.data_colection.iris_api.exceptions import RequestConnectionError, PilarError

logger = logging.getLogger(__name__.rsplit('.', 1)[-1])


class IrisConnector(Connector):
    def __init__(self, host: str, port: int, **kwargs):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._transaction_id_counter = 0
        self._lock = asyncio.Lock()
        self.event_queue = asyncio.Queue()

    async def connect(self):
        if self.is_connected():
            logger.warning("Already connected.")
            return

        try:
            logger.info(f"Connecting to TPL2 server at {self.host}:{self.port}...")
            
            open_connection = asyncio.open_connection(self.host, self.port)
            self._reader, self._writer = await asyncio.wait_for(open_connection, timeout=10.0)

            welcome_msg_bytes = await self._reader.readline()
            welcome_msg = welcome_msg_bytes.decode('utf-8').strip()
            logger.info(f"Received welcome message: {welcome_msg}")

            if "TLS" in welcome_msg:
                logger.info("Server supports TLS. Initiating encryption...")
                self._writer.write(b"ENC TLS\n")
                await self._writer.drain()
                
                enc_response_bytes = await self._reader.readline()
                enc_response = enc_response_bytes.decode('utf-8').strip()
                
                if "ENC OK" not in enc_response:
                    raise RequestConnectionError("TLS negotiation failed.")

                ssl_context = ssl.create_default_context()
                transport = self._writer.transport
                
                new_reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(new_reader)
                
                await asyncio.get_running_loop().start_tls(transport, protocol, ssl_context, server_side=False)
                
                self._reader = new_reader
                self._writer.set_transport(protocol.transport)

                logger.info("Connection is now encrypted.")

            self._reader_task = asyncio.create_task(self._response_reader_loop())
            logger.info("Connection established and response reader started.")

        except (OSError, asyncio.TimeoutError) as e:
            raise RequestConnectionError(f"Failed to connect to {self.host}:{self.port}") from e

    async def close(self):
        if not self.is_connected():
            return
        
        logger.info("Closing connection...")
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass 
        
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(RequestConnectionError("Connection closed."))

        self._pending_requests.clear()
        self._writer = self._reader = self._reader_task = None
        logger.info("Connection closed.")

    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _get_next_transaction_id(self) -> int:
        async with self._lock:
            self._transaction_id_counter += 1
            if self._transaction_id_counter > 4294967295:
                self._transaction_id_counter = 1
            return self._transaction_id_counter

    async def _response_reader_loop(self):
        try:
            while self.is_connected():
                line_bytes = await self._reader.readline()
                if not line_bytes:
                    logger.warning("Connection closed by server.")
                    break
                
                line = line_bytes.decode('utf-8').strip()
                if not line:
                    continue
                
                logger.debug(f"<<< RECV: {line}")
                
                parts = line.split(maxsplit=2)
                if not parts or not parts[0].isdigit():
                    if line.upper().startswith("0 EVENT"):
                        logger.info(f"Received asynchronous event: {line}")
                        await self.event_queue.put(line)
                    else:
                        logger.warning(f"Received non-standard line: {line}")
                    continue

                tx_id = int(parts[0])
                future = self._pending_requests.get(tx_id)

                if not future or future.done():
                    continue

                command = parts[1]
                payload = parts[2] if len(parts) > 2 else ""

                if not hasattr(future, 'collected_data'):
                    future.collected_data = []

                if command == "DATA":
                    if "INLINE" in payload:
                        future.collected_data.append(payload)
                elif command == "COMMAND":
                    if payload == "COMPLETE":
                        if not future.done():
                            result = future.collected_data if future.collected_data else "OK"
                            future.set_result(result)
                    elif "ERROR" in payload or "FAILED" in payload:
                        if not future.done():
                            future.set_exception(PilarError(tx_id, f"COMMAND error: {payload}"))
                elif command == "DATA" and "ERROR" in payload:
                     if not future.done():
                        future.set_exception(PilarError(tx_id, f"DATA error: {payload}"))

        except ConnectionResetError:
            logger.error("Connection was reset by the peer.")
        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.error(f"Error in response reader loop: {e}", exc_info=True)
        finally:
            if self.is_connected():
                asyncio.create_task(self.close())

    async def _execute_command(self, command_string: str) -> Any:
        if not self.is_connected():
            raise RequestConnectionError("Not connected.")

        tx_id = await self._get_next_transaction_id()
        future = asyncio.get_running_loop().create_future()
        self._pending_requests[tx_id] = future
        
        full_command = f"{tx_id} {command_string}\n"
        logger.debug(f">>> SEND: {full_command.strip()}")
        
        try:
            self._writer.write(full_command.encode('utf-8'))
            await self._writer.drain()
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            raise PilarError(-1, f"Command timeout for tx_id {tx_id}: {command_string}")
        finally:
            self._pending_requests.pop(tx_id, None)

    async def get(self, component: 'Component', variable: str, kind: str = None, **data) -> Any:
        object_path = f"{kind}.{variable}" if kind else f"{component.kind}.{variable}"
        command = f"GET {object_path}"
        response = await self._execute_command(command)
        
        if isinstance(response, list):
            parsed_data = {}
            for item in response:
                try:
                    key, value = item.split('=', 1)[0].split(' ')[-1], item.split('=', 1)[1]
                    if value in ["BUSY", "DENIED", "UNKNOWN"]:
                        parsed_data[key] = PilarError(0, f"DATA error: {value}")
                    else:
                        parsed_data[key] = value.strip('"') if value.startswith('"') and value.endswith('"') else value
                except IndexError:
                    continue # Ignore malformed data lines
            return parsed_data if len(parsed_data) > 1 else list(parsed_data.values())[0]

        return response


    async def put(self, component: 'Component', variable: str, kind: str = None, **data) -> Any:
        object_path = f"{kind}.{variable}" if kind else f"{component.kind}.{variable}"
        value = data.get('value')

        if isinstance(value, str):
            value_str = f'"{value}"'
        else:
            value_str = str(value)

        command = f"SET {object_path}={value_str}"
        return await self._execute_command(command)
