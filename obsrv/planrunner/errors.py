
class ResourceError(Exception):
    def __init__(self, msg=""):
        self.msg = msg


class PlanBuildError(Exception):
    def __init__(self, msg=""):
        self.msg = msg
