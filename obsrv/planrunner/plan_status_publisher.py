from serverish.base import dt_utcnow_array
from serverish.messenger import get_publisher
from obsrv.comunication.nats_streams import NatsStreams


class PlanStatusPublisher:

    def __init__(self, plan_id: str, telescope_id: str):
        self._plan_current_state = {}  # this is a dictionary witch command's status only witch one was finished
        self._plan_id: str = plan_id
        self._telescope_id: str = telescope_id

    def update_status_local(self, cmd_id: str, data: dict):
        """
        Method update local command status dict.

        :param cmd_id: command id
        :param data: dictionary witch status data
        """
        self._plan_current_state[cmd_id] = self._plan_current_state.get(cmd_id, {}) | data

    def reset_status_local(self, cmd_id):
        """
        Method reset local command status dict.

        :param cmd_id: command id
        """
        self._plan_current_state[cmd_id] = {}

    async def update_status(self, cmd_id: str, data: dict):
        """
        Method update and send to server command status dict

        :param cmd_id: command id
        :param data: dictionary witch status data
        """
        self.update_status_local(cmd_id=cmd_id, data=data)
        await self._send_program_current_state()

    async def reset_status(self, cmd_id: str):
        """
        Method reset and send to server command status dict.

        :param cmd_id:
        """
        self.reset_status_local(cmd_id=cmd_id)
        await self._send_program_current_state()

    @staticmethod
    async def send_program_state(plan_id: str, dic: dict, telescope_id: str):
        """
        Method send observation plan state to NATS stream.

        :param plan_id: observation plan id
        :param dic: dictionary witch observation plan initial state
        :param telescope_id: telescope id like "zb08"
        :return: None
        """
        publisher = get_publisher(NatsStreams.PLAN_MANAGER_PLAN.format(telescope_id))
        await publisher.publish(data={'id': plan_id,
                                      'published': dt_utcnow_array(),
                                      'state': dic},
                                meta={
                                    "message_type": "program_state",  # IMPORTANT one of pre declared types
                                    "tags": ["plan_state"],
                                    'sender': 'Ocabox server',
                                })

    async def _send_program_current_state(self):
        """
        Method send observation plan current state to NATS stream.

        :return: None
        """
        publisher = get_publisher(NatsStreams.PLAN_MANAGER_STATUS.format(self._telescope_id))
        await publisher.publish(data={'id': self._plan_id,
                                      'published': dt_utcnow_array(),
                                      'state': self._plan_current_state},
                                meta={
                                    "message_type": "program_current_state",  # IMPORTANT one of pre declared types
                                    "tags": ["plan_current_state"],
                                    'sender': 'Ocabox server',
                                })

    def get_status_local(self, cmd_id) -> dict:
        """
        Method return status dict for command with giving id.
        :param cmd_id: command id
        :return: local status dict
        """
        return self._plan_current_state.get(cmd_id, {})
