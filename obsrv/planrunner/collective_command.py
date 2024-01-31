import asyncio
from abc import ABC
from typing import List
from obsrv.planrunner.command import Command


class CollectiveCommand(Command, ABC):
    _NAME = "DEFAULT_COLLECTIVE"

    def __init__(self, command_dict: dict, plan_data, previous_command, id_: str = None,
                 loop: asyncio.AbstractEventLoop = None, **kwargs):
        super().__init__(command_dict=command_dict, plan_data=plan_data, previous_command=previous_command,
                         id_=id_, loop=loop, **kwargs)
        self._subcommands: List[Command] = []

    def __len__(self):
        return len(self._subcommands)

    def __getitem__(self, index):
        return self._subcommands[index]

    def __iter__(self):
        return iter(self._subcommands)

    def get_sub(self, id_: str):
        for i in self._subcommands:
            if i.get_id() == id_:
                return i
        return None

    def get_status_dict(self):
        dic = super().get_status_dict()
        d = {}
        for sub in self:
            d[sub.get_id()] = sub.get_status_dict()
        dic["subcommands"] = d
        return dic
