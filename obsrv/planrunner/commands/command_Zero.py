from obsrv.planrunner.command import Command


class CommandZero(Command):
    _NAME = "ZERO"

    def __init__(self, kw_args: dict, ln: str, commands: list = None, label: str = "", message: str = "", **kwargs):
        super().__init__(kw_args=kw_args, ln=ln, label=label, message=message)
