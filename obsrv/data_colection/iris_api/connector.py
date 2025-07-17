import asyncio
import logging
from typing import Dict

from obsrv.data_colection.base_api.connector import Connector
from obsrv.data_colection.iris_api.exceptions import RequestConnectionError, PilarError

logger = logging.getLogger(__name__.rsplit('.', 1)[-1])


class IrisConnector(Connector):
    """
    TPL2 connector for communication with the IRIS telescope server (TCI/TSI).
    Implements asynchronous sending of commands and receiving of responses.
    """

    def __init__(self, host: str, port: int, **kwargs):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._transaction_id_counter = 0
        self._lock = asyncio.Lock()  # Protects the transaction_id counter

    async def connect(self):
        """Establishes a connection and starts the response reading loop."""
        if self.is_connected():
            logger.warning("Already connected.")
            return

        try:
            logger.info(f"Connecting to TPL2 server at {self.host}:{self.port}...")
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            # Start a background task for continuous response reading
            self._reader_task = asyncio.create_task(self._response_reader_loop())
            logger.info("Connection established and response reader started.")
        except (OSError, asyncio.TimeoutError) as e:
            raise RequestConnectionError(f"Failed to connect to {self.host}:{self.port}") from e

    async def close(self):
        """Closes the connection and stops the reading loop."""
        if not self.is_connected():
            return
        
        logger.info("Closing connection...")
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass  # Expected exception
        
        # Reject all pending requests
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(RequestConnectionError("Connection closed."))

        self._pending_requests.clear()
        self._writer = self._reader = self._reader_task = None
        logger.info("Connection closed.")

    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _get_next_transaction_id(self) -> int:
        """Gets the next, unique transaction ID."""
        async with self._lock:
            self._transaction_id_counter += 1
            if self._transaction_id_counter > 4294967295: # According to TPL2 [cite: 2146]
                self._transaction_id_counter = 1
            return self._transaction_id_counter

    async def _response_reader_loop(self):
        """A background loop that reads and processes all incoming lines from the server."""
        try:
            while self.is_connected():
                line_bytes = await self._reader.readline()
                if not line_bytes:
                    logger.warning("Connection closed by server.")
                    break
                
                line = line_bytes.decode('utf-8').strip()
                logger.debug(f"<<< RECV: {line}")
                
                parts = line.split(maxsplit=2)
                if not parts or not parts[0].isdigit():
                    logger.warning(f"Received non-standard line: {line}")
                    continue

                tx_id = int(parts[0])
                future = self._pending_requests.get(tx_id)

                if not future or future.done():
                    # Asynchronous message (EVENT) or response to a cancelled request
                    continue

                command = parts[1]
                payload = parts[2] if len(parts) > 2 else ""

                if command == "DATA":
                    # Simple implementation - we assume that DATA INLINE is the last piece of data
                    # In more complex cases, the Future could collect data
                    if "INLINE" in payload:
                        value = payload.split('=', 1)[-1]
                        if value == "NULL":
                            future.set_result(None)
                        elif value in ["BUSY", "DENIED", "UNKNOWN"]:
                            future.set_exception(PilarError(tx_id, f"DATA error: {value}"))
                        else:
                            future.set_result(value)
                elif command == "COMMAND" and payload == "COMPLETE":
                    if not future.done():
                        # If the SET command did not return DATA, we return success
                        future.set_result("OK")
                    # The request is complete, but we don't remove it, waiting for potential data
                elif command == "COMMAND" and "ERROR" in payload:
                    future.set_exception(PilarError(tx_id, f"COMMAND error: {payload}"))
                elif command == "DATA" and "ERROR" in payload:
                    future.set_exception(PilarError(tx_id, f"DATA error: {payload}"))
        except ConnectionResetError:
            logger.error("Connection was reset by the peer.")
        except Exception as e:
            logger.error(f"Error in response reader loop: {e}", exc_info=True)
        finally:
            asyncio.create_task(self.close())

    async def _execute_command(self, command_string: str) -> any:
        """Creates a Future, sends a TPL2 command, and waits for it to be resolved."""
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
            # Wait for the result from the reading loop
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending_requests.pop(tx_id, None)
            raise PilarError(-1, f"Command timeout for tx_id {tx_id}: {command_string}")
        finally:
            self._pending_requests.pop(tx_id, None)

    async def get(self, component: 'Component', variable: str, kind: str = None, **data) -> any:
        """Sends a GET command in TPL2 format."""
        object_path = f"{kind}.{variable}" if kind else f"{component.kind}.{variable}"
        command = f"GET {object_path}"
        return await self._execute_command(command)

    async def put(self, component: 'Component', variable: str, kind: str = None, **data) -> any:
        """Sends a SET command in TPL2 format."""
        object_path = f"{kind}.{variable}" if kind else f"{component.kind}.{variable}"
        value = data.get('value')

        if isinstance(value, str):
            # Strings must be in quotes according to TPL2
            value_str = f'"{value}"'
        else:
            value_str = str(value)

        command = f"SET {object_path}={value_str}"
        return await self._execute_command(command)
