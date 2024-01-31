

class BaseCommandError(Exception):
    pass


class CriticalCommandError(BaseCommandError):
    pass


class NormalCommandError(BaseCommandError):
    pass

