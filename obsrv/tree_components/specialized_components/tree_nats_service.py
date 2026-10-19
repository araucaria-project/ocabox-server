"""TreeNatsService — the generic NATS protocol driver: a TIC subtree served by a NATS service.

A service of the ocabox-tcs family (the optical-path service first, the guider next) is reached
through TIC like any device, so it gets the client API, the error model and the access control of
the tree for free. Address and subject are the same tokens of the subject grammar
``<class>.<realm>.<scope>.<element>.<item>`` (oca_nats_config#2), only permuted::

    GET  <scope>.<realm>.<element>.<item>   ⇐ status.<realm>.<scope>.<element>.<item>   (JetStream, pushed)
    PUT  <scope>.<realm>.<element>.<item>   ⇒ rpc.<realm>.<scope>.<element>.<item>      (core NATS request/reply)
         liveness                            ⇐ svc.heartbeat.<service_type>.<variant>   (TCS library)

Reads are push end to end: every ``status`` message goes into the enclosing cache
(``set_value_sink``), so waiting subscribers get a change within one loop tick and a GET never
touches the network. A value is current while the service is alive — the service publishes every
change and repeats its state as keepalive — so a GET answered from the mirror is stamped *now*;
once the service is silent for ``3 × keepalive`` every GET is 4005. Contact is measured by the
broker's clock (JetStream timestamp), so state replayed after a TIC restart does not count as a
sign of life.

Writes behave like a slew: the service accepts or refuses at once (``RpcReply``), progress and
outcome follow as status. The permission was checked by the blocker above this provider; it never
travels on — the caller does, for the service's audit.

Placement in tree-build: under the telescope's ``TreeBaseRequestBlocker`` (in a broker whose
default is the observatory adapter), so PUTs pass reservation and safety cutoff.
"""
import logging
import time
from typing import Optional

from datamodels.messages import Caller, RpcError, RpcReply, RpcRequest, StatusValue, Subject
from obcom.data_colection.address import Address, AddressError
from obcom.data_colection.coded_error import BaseCodedError, TreeOtherError, TreeStructureError
from obcom.data_colection.value import TreeValueError, Value
from obcom.data_colection.value_call import ValueRequest
from pydantic import TypeAdapter, ValidationError
from serverish.base.datetime import dt_ensure_datetime
from serverish.messenger import get_callbacksubscriber, get_rpcrequester
from serverish.messenger.msg_rpc_req import MessengerRequestNoResponders, MessengerRequestTimeout

from obsrv.tree_components.base_components.tree_provider import TreeProvider
from obsrv.tree_components.specialized_components.tree_base_request_blocker import TreeBaseRequestBlocker
from obsrv.tree_components.specialized_components.tree_conditional_freezer import strip_tree_internal_fields

logger = logging.getLogger(__name__.rsplit('.')[-1])

_REPLY = TypeAdapter(RpcReply)
#: Parameters consumed by the blocker; a service never sees them.
_GATE_PARAMS = (TreeBaseRequestBlocker.SPECIAL_PERMISSION_PARAM, TreeBaseRequestBlocker.SAFETY_CUTOFF_BYPASS_PARAM)


def coded_error(code: int, message: str, severity: str) -> BaseCodedError:
    """The obcom exception of ``code``'s group (1xxx address, 2xxx value, 3xxx structure, 4xxx other)."""
    if 1000 <= code < 2000:
        return AddressError(code=code, message=message, severity=severity)
    if 2000 <= code < 3000:
        return TreeValueError(code=code, message=message, severity=severity)
    if 3000 <= code < 4000:
        return TreeStructureError(code=code, message=message, severity=severity)
    return TreeOtherError(code=code, message=message, severity=severity)


class TreeNatsService(TreeProvider):
    """A TIC subtree ``<scope>.<realm>`` served by the NATS service whose realm is ``source_name``.

    :param component_name: tree component name (logs, ``tree.<component_name>.*`` config)
    :param source_name: the realm: the service's one-token TCS ``service_type`` (e.g. ``optics``)
    :param scope: the scope it serves: a telescope id (``jk15``)
    :param heartbeat_subject: the service's TCS heartbeat; default ``svc.heartbeat.<realm>.<scope>``
    :param keepalive: the service's keepalive period in seconds; silent for ``3 × keepalive`` = dead
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeNatsService'
    VALUE_PUSHING = True
    DEAD_AFTER_KEEPALIVES: int = 3

    def __init__(self, component_name: str, source_name: str, scope: str,
                 heartbeat_subject: Optional[str] = None, keepalive: float = 10.0, **kwargs):
        super().__init__(component_name=component_name, source_name=source_name, subcontractor=None, **kwargs)
        # validates realm and scope as subject tokens at tree-build time
        Subject(message_class='status', realm=source_name, scope=scope, element='self', item='x')
        self._realm = source_name
        self._scope = scope
        self._heartbeat_subject = heartbeat_subject or f'svc.heartbeat.{source_name}.{scope}'
        self._keepalive = float(keepalive)
        self._timeout_multiplier = 0.8
        self._mirror: dict[tuple[str, str], object] = {}  # (element, item) -> last value
        self._contact: Optional[float] = None  # broker time of the last message from the service
        self._subscribers = []

    # ------------------------------------------------------------------ lifecycle

    async def run(self):
        await super().run()
        status = get_callbacksubscriber(f'status.{self._realm}.{self._scope}.>', deliver_policy='last_per_subject')
        heartbeat = get_callbacksubscriber(self._heartbeat_subject, deliver_policy='last')
        self._subscribers = [status, heartbeat]
        await status.subscribe(self._on_status)
        await heartbeat.subscribe(self._on_heartbeat)
        logger.info(f'{self.get_name()}: serving {self._scope}.{self._realm} from status.{self._realm}.{self._scope}.>')

    async def stop(self):
        for subscriber in self._subscribers:
            await subscriber.close()
        self._subscribers = []
        await super().stop()

    # ------------------------------------------------------------------ inbound

    def _touch(self, meta: dict) -> float:
        """Record contact at the broker time of ``meta``; return the time to stamp its value with:
        *now* for a message that is itself a sign of life, its broker time for a replay."""
        broker_ts = dt_ensure_datetime(meta.get('nats', {}).get('timestamp') or meta.get('ts'))
        sent = broker_ts.timestamp() if broker_ts is not None else time.time()
        if self._contact is None or sent > self._contact:
            self._contact = sent
        return time.time() if self._alive_at(sent) else sent

    async def _on_heartbeat(self, data: dict, meta: dict) -> None:
        self._touch(meta)

    async def _on_status(self, data: dict, meta: dict) -> None:
        subject = meta.get('nats', {}).get('subject', '?')
        try:
            parsed = Subject.parse(subject)
            status = StatusValue.model_validate(data)
        except ValueError as e:  # pydantic's ValidationError is a ValueError
            logger.error(f'{self.get_name()}: ignoring malformed status on {subject}: {e}')
            return
        stamp = self._touch(meta)
        self._mirror[(parsed.element, parsed.item)] = status.value
        if self._value_sink is not None:
            address = Address(f'{self._scope}.{self._realm}.{parsed.element}.{parsed.item}')
            await self._value_sink(address, Value(status.value, stamp))

    # ------------------------------------------------------------------ liveness

    def _alive_at(self, t: float) -> bool:
        return time.time() - t < self.DEAD_AFTER_KEEPALIVES * self._keepalive

    def is_alive(self) -> bool:
        return self._contact is not None and self._alive_at(self._contact)

    def _silent(self) -> TreeOtherError:
        since = 'never heard from' if self._contact is None else f'silent for {time.time() - self._contact:.0f} s'
        return TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                              message=f'service {self._realm!r} for {self._scope} is {since}')

    # ------------------------------------------------------------------ requests

    async def get_value(self, request: ValueRequest, **kwargs) -> Value:
        element, item = self._element_item(request)
        if request.request_type == 'PUT':
            return await self._put(request, element, item)
        if not self.is_alive():
            raise self._silent()
        try:
            return Value(self._mirror[(element, item)], time.time())
        except KeyError:
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f'{self._realm!r} has not published {element}.{item} for {self._scope}')

    def _element_item(self, request: ValueRequest) -> tuple[str, str]:
        tail = request.address[request.index:]
        if len(tail) != 2:
            raise AddressError(address=request.address, code=1001,
                               message=f'a {self._realm!r} address is <element>.<item>, got {".".join(tail) or "nothing"}')
        return tail[0], tail[1]

    async def _put(self, request: ValueRequest, element: str, item: str) -> Value:
        arguments = strip_tree_internal_fields(request.request_data) or {}
        parameters = {k: v for k, v in arguments.items() if k not in _GATE_PARAMS}
        remaining = request.request_timeout - time.time()
        if remaining <= 0:
            raise TreeOtherError(code=4004, severity=TreeOtherError.SEVERITY_TEMPORARY,
                                 message=f'request deadline passed {-remaining:.2f} s ago; not sent to {self._realm!r}')
        timeout = remaining * self._timeout_multiplier
        user = request.user
        caller = Caller(name=user.name, id=bytes(user.id_).hex()) if user is not None else None
        body = RpcRequest(parameters=parameters, caller=caller, timeout=timeout)
        subject = f'rpc.{self._realm}.{self._scope}.{element}.{item}'
        try:
            data, _ = await get_rpcrequester(subject).request(data=body.model_dump(mode='json'), timeout=timeout)
        except MessengerRequestNoResponders:
            raise TreeOtherError(code=4005, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f'nothing answers {subject}: service {self._realm!r} for {self._scope} is not running')
        except MessengerRequestTimeout:
            raise TreeOtherError(code=4002, severity=TreeOtherError.SEVERITY_NORMAL,
                                 message=f'{subject} did not answer within {timeout:.1f} s')
        try:
            reply = _REPLY.validate_python(data)
        except ValidationError as e:
            raise TreeValueError(code=2002, severity=TreeValueError.SEVERITY_NORMAL,
                                 message=f'{subject} answered outside the RpcReply contract: {e}')
        if isinstance(reply, RpcError):
            raise coded_error(reply.code, f'{self._realm}: {reply.message}', reply.severity.value)
        return Value(reply.result, time.time())

    # ------------------------------------------------------------------ discovery

    def get_publishable_config(self) -> Optional[dict]:
        return {
            "role": "service",
            "key": self._realm,
            "address": f"{self._scope}.{self._realm}",
            "type": "nats_service",
            "subjects": {
                "status": f"status.{self._realm}.{self._scope}.>",
                "rpc": f"rpc.{self._realm}.{self._scope}.>",
                "heartbeat": self._heartbeat_subject,
            },
        }
