"""BESO spectrograph camera connector.

Talks to the BESO server (a line-based TCP command interface, one client at a time, one reply per command)
and exposes it through the same ``Connector`` interface as the ALPACA and Pilar connectors. Which BESO
command serves which OCABOX parameter is defined entirely in ``beso_config.yml``.

The device tree builds one connector per component with ``protocol: beso``; all connectors pointing at the
same address share one ``BesoSession`` (connection, queue, cache, exposure tracking).
"""
import asyncio
import json
import logging
from typing import Callable, Dict, Iterable, Optional, Tuple

from obsrv.protocols.alpaca.alpaca_connector import Connector
from obsrv.protocols.beso.beso_config import DEFAULT_CONFIG_PATH, BesoConfig, ComponentSpec, load_config
from obsrv.protocols.beso.beso_errors import BesoUnsupportedError, to_tree_error
from obsrv.protocols.beso.beso_logging import RepeatLimiter
from obsrv.protocols.beso.beso_session import BesoSession, acquire_session, release_session

logger = logging.getLogger(__name__.rsplit('.')[-1])

_CONFIG_CACHE: Dict[Tuple[str, str], BesoConfig] = {}


def get_config(path: Optional[str] = None, overrides: Optional[dict] = None) -> BesoConfig:
    """Load and validate once per file; every connector of the process shares the same object
    (connectors of one BESO address must agree on the mapping, see acquire_session)."""
    key = (path or DEFAULT_CONFIG_PATH, json.dumps(overrides, sort_keys=True, default=repr) if overrides else '')
    config = _CONFIG_CACHE.get(key)
    if config is None:
        config = load_config(key[0], overrides)
        _CONFIG_CACHE[key] = config
        logger.info("BESO configuration %s loaded: %d components, %d queries", config.path,
                    len(config.components), len(config.queries))
        if config.todos:
            logger.warning("BESO configuration has %d entries marked TODO (reply formats or mappings not verified "
                           "on the device); list them with: python -m obsrv.protocols.beso.beso_config",
                           len(config.todos))
    return config


class BesoConnector(Connector):

    def __init__(self, config_path: Optional[str] = None, config_overrides: Optional[dict] = None, **kwargs):
        super().__init__(**kwargs)
        # Raises BesoConfigError on an invalid configuration, before any request is served.
        self._config = get_config(config_path, config_overrides)
        self._sessions: Dict[str, BesoSession] = {}
        self._log = RepeatLimiter(self._config.settings.log_repeat_interval)
        self._subscribe_logged = False
        logger.info('BESO connector created')

    # --------------------------------------------------------------------------- Connector interface
    async def get(self, component: 'Component', variable: str, kind=None, **data):
        where = self._where(component, variable, kind)
        try:
            spec = self._component_spec(component, kind)
            read = spec.reads.get(variable)
            if read is None:
                if variable in spec.writes:
                    raise BesoUnsupportedError(f"{variable!r} can only be written")
                raise BesoUnsupportedError(f"{variable!r} is not mapped for BESO")
            return await self._session(component).read(read)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._raise_translated(exc, where)

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        where = self._where(component, variable, kind)
        try:
            spec = self._component_spec(component, kind)
            write = spec.writes.get(variable)
            if write is None:
                if variable in spec.reads:
                    raise BesoUnsupportedError(f"{variable!r} is read-only")
                raise BesoUnsupportedError(f"{variable!r} is not mapped for BESO")
            return await self._session(component).write(write, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._raise_translated(exc, where)

    async def call(self, component: 'Component', function: str, **data):
        return await self.put(component, function, **data)

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        # Subscriptions are served by TreeCache/TreeConditionalFreezer through cyclic `get`.
        if not self._subscribe_logged:
            self._subscribe_logged = True
            logger.info("BESO connector has no native subscriptions; values are served through cyclic get")

    async def close(self):
        """Release the shared sessions; the last connector of an address closes its connection."""
        sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            await release_session(session)

    # ------------------------------------------------------------------------------------- helpers
    def _component_spec(self, component, kind) -> ComponentSpec:
        kind = kind if kind else component.kind
        device = component.device_nr
        spec = self._config.component(kind, device)
        if spec is None:
            raise BesoUnsupportedError(f"no BESO mapping for component {kind}/{device}")
        return spec

    def _session(self, component) -> BesoSession:
        address = component.get_option_recursive('address')
        if not address:
            raise BesoUnsupportedError(f"no 'address' configured for component {component.sys_id}")
        session = self._sessions.get(address)
        if session is None or session.closed or session.loop is not asyncio.get_running_loop():
            session = acquire_session(address, self._config)
            self._sessions[address] = session
        return session

    @staticmethod
    def _where(component, variable: str, kind) -> str:
        kind = kind if kind else getattr(component, 'kind', '?')
        return f"BESO {kind}/{getattr(component, 'device_nr', '?')}.{variable}"

    def _raise_translated(self, exc: BaseException, where: str):
        error = to_tree_error(exc, where)
        should_log, suppressed = self._log.check((where, type(exc)))
        if should_log:
            logger.warning("%s failed: %s%s", where, exc, f" ({suppressed} similar suppressed)" if suppressed else "")
        if error is exc:
            raise exc
        raise error from exc
