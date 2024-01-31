from obsrv.util_functions.enum_map_base_class import BaseArgsMap, StrVal


class _TrackingOptions(str, BaseArgsMap):
    ON: StrVal = StrVal("on", True)
    OFF: StrVal = StrVal("off", False)


class _ScreenLight(str, BaseArgsMap):
    AUTO = "auto"
    ON = "on"
    OFF = "off"


class _MirrorCover(str, BaseArgsMap):
    AUTO = "auto"
    OPEN = "open"
    CLOSE = "close"


class _DomeFollow(str, BaseArgsMap):
    ON: StrVal = StrVal("on", True)
    OFF: StrVal = StrVal("off", False)


class CommandArgsMap(BaseArgsMap):
    TRACKING: _TrackingOptions = _TrackingOptions("traking")
    OBJ_NAME = StrVal("object_name", "")
    RA = StrVal("ra", None)
    DEC = StrVal("dec", None)
    EPOCH = StrVal("epoch", "2000")
    UOBI = StrVal("uobi", 0)
    OBSERVER = StrVal("observer", "")
    AZ = StrVal("az", None)
    ALT = StrVal("alt", None)
    SCREEN_LIGHT: _ScreenLight = _ScreenLight("screen_light_mode")
    MIRROR_COVER: _MirrorCover = _MirrorCover("mirror_cover")
    DOME_FOLLOW: _DomeFollow = _DomeFollow("dome_follow")
    SEQ = StrVal("seq", "")
    FOCUSING = StrVal("pos", "")
    FOCUSING_OFFSET = StrVal("defocus", 0)
    DITHER = StrVal("dither", "")
