import asyncio
import logging
import ssl
import os
from typing import Iterable, Callable, Tuple, Dict
import confuse

from obsrv.protocols.alpaca.alpaca_connector import Connector
from obcom.data_colection.address import AddressError
from obcom.data_colection.coded_error import TreeOtherError

logger = logging.getLogger(__name__.rsplit('.')[-1])

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'pilar_config.yml')

# Socket-level failures that indicate the Pilar link is down / stale.
# These are classified as SEVERITY_TEMPORARY so TreeConditionalFreezer can suppress them.
_TEMPORARY_IO_ERRORS = (ConnectionError, BrokenPipeError, OSError, asyncio.TimeoutError, TimeoutError)


class PilarConnection:
    """Reprezentuje pojedyncze, aktywne połączenie TCP z serwerem Pilar."""
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.broken = False

    async def execute(self, cmd_id: int, command_str: str, timeout: float) -> str:
        """Wysyła komendę i czeka na odpowiedź pasującą do cmd_id."""
        full_command = f"{cmd_id} {command_str}\n"
        try:
            self.writer.write(full_command.encode('utf-8'))
            await self.writer.drain()
        except _TEMPORARY_IO_ERRORS:
            self.broken = True
            raise

        value_to_return = None
        while True:
            try:
                line_bytes = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
            except _TEMPORARY_IO_ERRORS:
                self.broken = True
                raise
            if not line_bytes:
                self.broken = True
                raise ConnectionAbortedError("Pilar connection closed unexpectedly.")
            
            response_line = line_bytes.decode('utf-8').strip()
            
            # Sprawdzamy czy odpowiedź dotyczy naszego ID
            if response_line.startswith(f"{cmd_id} "):
                
                # Znak = oznacza, że dostaliśmy wartość
                if "=" in response_line:
                    value_str = response_line.split("=", 1)[1].strip()
                    # Próba konwersji tekstu na liczbę musi być pod if "="
                    try:
                        if '.' in value_str:
                            value_to_return = float(value_str)
                        else:
                            value_to_return = int(value_str)
                    except ValueError:
                        # Jeśli to nie liczba, zostaw jako tekst
                        value_to_return = value_str
                        
                # Rozpoznajemy status zakończenia komendy
                if "COMMAND COMPLETE" in response_line:
                    return value_to_return if value_to_return is not None else "OK"
                
                # Rozszerzone wyłapywanie asynchronicznych błędów i ostrzeżeń z serwera
                if any(error_flag in response_line for error_flag in ["COMMAND FAILED", "DATA ERROR", "FAILED", "EVENT WARN"]):
                    raise RuntimeError(f"Pilar command failed: {command_str}. Server said: {response_line}")


class PilarConnector(Connector):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._load_config()
        
        # Słowniki przechowujące pule dla poszczególnych adresów (host:port)
        # Klucz: "IP:PORT", Wartość: Kolejka aktywnych obiektów PilarConnection
        self._connection_pools: Dict[str, asyncio.Queue[PilarConnection]] = {}
        
        # Klucz: "IP:PORT", Wartość: Kolejka dostępnych ID transakcji
        self._id_pools: Dict[str, asyncio.Queue[int]] = {}
        
        # Blokady logiczne dla zasobów (np. żeby nie ruszać Focuserem gdy inny wątek nim rusza)
        self._resource_locks: Dict[str, asyncio.Lock] = {
            resource_name: asyncio.Lock() for resource_name in set(self._resource_lock_map.values())
        }
        
        # Blokada techniczna, aby nie tworzyć puli dla tego samego adresu wielokrotnie
        self._connection_locks: Dict[str, asyncio.Lock] = {} 
        
        logger.info(f'Pilar advanced connector created with pool size: {self._pool_size}')

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

            try:
                self._focuser_multiplier = config['settings']['focuser']['multiplier'].get(float)
            except confuse.NotFoundError:
                self._focuser_multiplier = 1000.0  # Wartość domyślna w razie braku wpisu w configu
                logger.warning("No settings.focuser.multiplier in configuration. Use default value: 1000.0")
            
            self._resource_lock_map = config['resource_locks'].get(dict)
            self._command_map = {}
            self._actions_map = {}

            if 'components' in config:
                for component_name, component_config in config['components'].get().items():
                    if 'mappings' in component_config:
                        self._command_map[component_name] = component_config['mappings']
                    if 'actions' in component_config:
                        self._actions_map[component_name] = component_config['actions']

            logger.info("Pilar configuration loaded successfully.")

        except (confuse.ConfigReadError, FileNotFoundError) as e:
            logger.error(f"CRITICAL: Could not read Pilar config file at {CONFIG_PATH}. Error: {e}")
            raise RuntimeError("Pilar connector configuration is missing or corrupted.") from e

    async def _create_single_connection(self, host, port) -> PilarConnection:
        """Tworzy jedno fizyczne połączenie TCP."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self._timeouts['connection']
            )
            # Obsługa powitania i ew. TLS
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
            logger.error(f"Failed to create a connection to {host}:{port}: {e}")
            return None

    async def _ensure_connected(self, address: str):
        """
        Sprawdza, czy dla danego adresu istnieje pula połączeń.
        Jeśli nie - tworzy 'pool_size' RÓWNOLEGŁYCH połączeń do tego adresu.
        """
        # Szybkie sprawdzenie bez blokady
        if address in self._connection_pools:
             return True 
        
        if address not in self._connection_locks:
            self._connection_locks[address] = asyncio.Lock()
        
        async with self._connection_locks[address]:
            # Ponowne sprawdzenie pod blokadą (double-check locking pattern)
            if address in self._connection_pools:
                 return True 
            
            try:
                host, port_str = address.split(':')
                port = int(port_str)
            except ValueError:
                logger.error(f"Invalid Pilar address format: {address}. Expected host:port")
                raise AddressError(address, 1003, "Invalid address format")

            logger.info(f"Initializing connection pool for {address} (Size: {self._pool_size})...")
            
            # 1. Przygotowanie puli ID
            id_pool = asyncio.Queue()
            for i in range(self._id_range[0], self._id_range[1] + 1):
                id_pool.put_nowait(i)
            self._id_pools[address] = id_pool

            # 2. Nawiązywanie wielu połączeń równolegle
            creation_tasks = [self._create_single_connection(host, port) for _ in range(self._pool_size)]
            connections = await asyncio.gather(*creation_tasks)
            
            # 3. Wrzucanie udanych połączeń do kolejki
            conn_pool = asyncio.Queue()
            active_count = 0
            for conn in connections:
                if conn:
                    conn_pool.put_nowait(conn)
                    active_count += 1
            
            if active_count > 0:
                self._connection_pools[address] = conn_pool
                logger.info(f"Pilar connector connected to {address}. Active connections: {active_count}")
                return True
            else:
                logger.error(f"Failed to connect to Pilar at {address} (0 connections established).")
                return False

    async def _get_connection_resources(self, address):
        """Pobiera jedno z wolnych połączeń i wolny ID z puli."""
        await self._ensure_connected(address)
        try:
            id_pool = self._id_pools[address]
            conn_pool = self._connection_pools[address]
            
            # Czekamy na dostępność ID i Połączenia
            # Dzięki temu wiele zapytań może działać równolegle, dopóki są wolne sockety
            cmd_id = await asyncio.wait_for(id_pool.get(), timeout=self._timeouts['pool_get'])
            conn = await asyncio.wait_for(conn_pool.get(), timeout=self._timeouts['pool_get'])
            return conn, cmd_id
        except (KeyError, asyncio.TimeoutError):
             raise TimeoutError(f"No available connection or ID in the pool for {address}.")

    async def _return_connection_resources(self, address, conn, cmd_id):
        """Zwraca zasoby do puli po zakończeniu komendy.
        Zepsute połączenia (broken pipe, reset) są zamykane i zastępowane świeżymi,
        żeby pula sama się leczyła po restartcie Pilara / idle-timeoucie sieci.
        """
        if address not in self._connection_pools:
            return
        if conn.broken:
            try:
                conn.writer.close()
            except Exception:
                pass
            try:
                host, port_str = address.split(':')
                replacement = await self._create_single_connection(host, int(port_str))
            except Exception:
                replacement = None
            if replacement is not None:
                await self._connection_pools[address].put(replacement)
        else:
            await self._connection_pools[address].put(conn)
        await self._id_pools[address].put(cmd_id)

    async def _get_address(self, component):
        return component.get_option_recursive('address')

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        address = await self._get_address(component)
        if not address:
             raise ValueError(f"No address configured for component {component.sys_id}")

        try:
            pilar_cmd = self._command_map[component.kind][variable]
            command = f"GET {pilar_cmd}"

            # Pobierz zasoby (to tu następuje zrównoleglenie - różne wątki dostają różne conn)
            conn, cmd_id = await self._get_connection_resources(address)
            try:
                result = await conn.execute(cmd_id, command, timeout=self._timeouts['get'])
                if component.kind == 'focuser' and variable == 'position':
                    if isinstance(result, (int, float)):
                        # Używamy round() przed int(), aby uniknąć błędów precyzji float
                        # np. 25.123 * 1000 = 25122.9999999 -> bez round wyszłoby 25122
                        result = int(round(result * self._focuser_multiplier))

                return result
            finally:
                await self._return_connection_resources(address, conn, cmd_id)
        except _TEMPORARY_IO_ERRORS as e:
            logger.warning(f"Pilar not responding at {address} ({component.kind}.{variable}): {e}")
            raise TreeOtherError(address=None, code=4005,
                                 message=f"Pilar not responding at {address}",
                                 severity=TreeOtherError.SEVERITY_TEMPORARY) from e
        except Exception as e:
            logger.error(f"Pilar GET failed for {component.kind}.{variable} at {address}: {e}")
            raise

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        address = await self._get_address(component)
        if not address:
             raise ValueError(f"No address configured for component {component.sys_id}")

        try:
            # --- DODANY KOD: Automatyczne przekierowanie akcji (np. slewtoaltaz) do metody call ---
            if component.kind in self._actions_map and variable in self._actions_map[component.kind]:
                logger.info(f"Przekierowuje PUT '{variable}' do CALL, poniewaz jest zdefiniowane jako akcja.")
                return await self.call(component, variable, **data)
            # --------------------------------------------------------------------------------------

            pilar_cmd = self._command_map[component.kind][variable]
            if not data:
                return {"status": "failed", "error": "Missing input value."}
            value = list(data.values())[0]
            if component.kind == 'focuser' and variable == 'position':
                try:
                    # Zamieniamy na float, dzielimy i zaokrąglamy dla bezpieczeństwa
                    value = round(float(value) / self._focuser_multiplier, 4)
                except (ValueError, TypeError):
                    pass # Jeśli klient wysłał bzdurę typu string "START", nie ruszamy, Pilar wyrzuci błąd
            command = f"SET {pilar_cmd}={value}"
            
            resource_name = self._resource_lock_map.get(pilar_cmd)
            lock = self._resource_locks.get(resource_name) if resource_name else None
            
            async def _do_put():
                conn, cmd_id = await self._get_connection_resources(address)
                try:
                    await conn.execute(cmd_id, command, timeout=self._timeouts['set'])
                finally:
                    await self._return_connection_resources(address, conn, cmd_id)

            # Jeśli komenda wymaga wyłączności (np. ruch teleskopu), używamy logicznej blokady
            # Inne komendy (np. zapalenie lampy) mogą iść równolegle
            if lock:
                async with lock:
                    await _do_put()
            else:
                await _do_put()
                
            return {"status": "ok", "value_set": value}
        except _TEMPORARY_IO_ERRORS as e:
            logger.warning(f"Pilar not responding at {address} ({component.kind}.{variable}): {e}")
            raise TreeOtherError(address=None, code=4005,
                                 message=f"Pilar not responding at {address}",
                                 severity=TreeOtherError.SEVERITY_TEMPORARY) from e
        except Exception as e:
            logger.error(f"Pilar PUT failed for {component.kind}.{variable}: {e}")
            return {"status": "failed", "error": str(e)}

    async def call(self, component: 'Component', function: str, **data):
        action_steps = self._actions_map.get(component.kind, {}).get(function)

        if not action_steps:
            logger.warning(f"Unknown Pilar action called: {function} for component {component.kind}")
            # Rzucamy wyjątek (jak w Alpace), klient dostanie poprawny kod błędu
            raise NotImplementedError(f"Unknown action: {function}")

        logger.info(f"Executing Pilar action: {function} with data {data}")
        
        # Usunięty ogólny blok try-except, by błędy natywnie przeszły do OcaBoxa
        for step in action_steps:
            if 'verify' in step:
                verify_data = step['verify']
                tolerance = float(verify_data.get('tolerance', 0.05))
                timeout_s = int(verify_data.get('timeout', 120))
                
                logger.info(f"Oczekiwanie na docelowe koordynaty (timeout={timeout_s}s, tolerancja={tolerance})...")
                
                targets = {}
                for var_name, var_val_tmpl in verify_data.items():
                    if var_name in ('tolerance', 'timeout'): 
                        continue
                    if isinstance(var_val_tmpl, str) and var_val_tmpl.startswith('{') and var_val_tmpl.endswith('}'):
                        arg_name = var_val_tmpl.strip('{}')
                        if arg_name not in data:
                            raise ValueError(f"Missing argument '{arg_name}' for verify step")
                        targets[var_name] = float(data[arg_name])
                    else:
                        targets[var_name] = float(var_val_tmpl)

                position_reached = False
                for _ in range(timeout_s):
                    all_match = True
                    for var_name, target_val in targets.items():
                        curr_val = await self.get(component, var_name)
                        if curr_val is None:
                            all_match = False
                            break
                        try:
                            if abs(float(curr_val) - target_val) > tolerance:
                                all_match = False
                                break
                        except ValueError:
                            all_match = False
                            break
                            
                    if all_match:
                        position_reached = True
                        logger.info(f"Teleskop osiągnął docelową pozycję dla akcji {function}!")
                        break
                        
                    await asyncio.sleep(1.0)
                    
                if not position_reached:
                    # Timeout też leci jako wyjątek prosto do klienta!
                    raise TimeoutError(f"Ruch teleskopu nie zakończył się w wyznaczonym czasie {timeout_s}s.")
                    
                continue 

            step_component_kind = step.get('component', component.kind)
            step_variable = step['variable']
            step_value = step['value']
            
            if isinstance(step_value, str) and step_value.startswith('{') and step_value.endswith('}'):
                arg_name = step_value.strip('{}')
                if arg_name not in data:
                    raise ValueError(f"Missing argument '{arg_name}' for action '{function}'")
                final_value = data[arg_name]
            else:
                final_value = step_value
            
            target_component = component if step_component_kind == component.kind else component.root.component_by_absolute_sys_id(step_component_kind)
            
            # Wysłanie komendy. Ewentualny wyjątek 'COMMAND FAILED' rzucony niżej poleci od razu do oca-boxa
            await self.put(target_component, step_variable, **{step_variable: final_value})

        # --- ZMIANA: Zwracamy None, aby poinformować OcaBox o pełnym sukcesie bez ładunku "Value", tak jak to robi Alpaca ---
        return None

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        logger.warning("Pilar protocol does not support subscriptions.")
        pass
