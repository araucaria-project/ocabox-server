from dataclasses import dataclass
from typing import ClassVar

from obsrv.ob_config import SingletonConfig


@dataclass
class NatsStreams:
    ALPACA_CONFIG: ClassVar[str] = SingletonConfig.get_config()['nats']["streams"]['alpaca_config'].get()  # "tic.config.observatory"
    PLAN_MANAGER_PLAN: ClassVar[str] = SingletonConfig.get_config()['nats']["streams"]['plan_stream'].get()  # tic.status.{}.program.current
    PLAN_MANAGER_STATUS: ClassVar[str] = SingletonConfig.get_config()['nats']["streams"]['status_stream'].get()  # tic.status.{}.program.state
