import logging

from obsrv.planrunner.commands.command_Sequence import CommandSequence
from obsrv.planrunner.commands.command_Zero import CommandZero

logger = logging.getLogger(__name__)


class ObsPlnReader:

    def __init__(self):
        self._obs_structure = None

    @classmethod
    def read_file(cls, path: str) -> dict:
        # here will be use parser
        dic = {'commands': [
            {
                'command': 'SEQUENCE',
                'kwargs': {'execute_at_time': '16:00'},
                'commands':
                    [
                        {
                            'command': 'ZERO',
                            'kwargs': {'seq': '15/I/0'}
                        },
                        {
                            'command': 'DARK',
                            'kwargs': {'seq': '0/V/300,10/I/200'}
                        },
                        {
                            'command': 'DOMEFLAT',
                            'kwargs': {'seq': '7/V/20,7/I/20'}
                        },
                        {
                            'command': 'DOMEFLAT',
                            'kwargs': {'seq': '10/str_u/100', 'domeflat_lamp': 0.7}
                        }
                    ]
            },
            {
                'command': 'SEQUENCE',
                'kwargs': {'execute_at_time': '02:21:43', 'priority': 30},
                'commands': [
                    {
                        'command': 'OBJECT',
                        'args': ['FF_Aql', '18:58:14.75', '17:21:39.29'],
                        'kwargs': {'seq': '2/I/60,2/V/70'}
                    }
                ]
            }
        ]}
        return dic

    @classmethod
    def create_obs_structure(cls, dic: dict):
        return cls._unpack_dict(dic=dic)

    @classmethod
    def _unpack_dict(cls, dic: dict):
        dic = cls.read_file('xxx')
        commands_tree = []

        cs = CommandSequence()

    @classmethod
    def unpack_list(cls, input_list):
        out_list = []
        for item in input_list:
            if isinstance(item, dict):
                c = cls.from_dict(item)
                if c is not None:
                    out_list.append(c)
                else:
                    logger.warning(f"Can not read command")
        return out_list

    @classmethod
    def from_dict(cls, dic: dict):
        command_type = dic.get("command")
        if command_type and command_type in cls.MAP_COMMANDS:
            command_class = cls.MAP_COMMANDS.get(command_type)(**dic)
        else:
            command_class = None  # todo maby raise error
        return command_class

    MAP_COMMANDS: dict = {
        "SEQUENCE": CommandSequence,
        "ZERO": CommandZero,
    }
