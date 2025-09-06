from dataclasses import dataclass
from serverish.messenger import Messenger
from obsrv.communication.base_request_solver_protocol import BaseRequestSolverProtocol


@dataclass
class TreeData:
    """
    this is an object containing the global data of the entire ocabox tree.
    """
    target_requests: BaseRequestSolverProtocol
    nats_messenger: Messenger = None

    def __post_init__(self):
        if self.nats_messenger is None:
            self.nats_messenger = Messenger()
