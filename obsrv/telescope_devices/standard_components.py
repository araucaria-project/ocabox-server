"""
Standard telescope component types for universal observatory architecture.
These are protocol-agnostic component types used across all telescope systems.
"""


class StandardTelescopeComponents:
    MOUNT = "telescope"
    DOME = "dome"
    CAMERA = "camera"
    FILTERWHEEL = "filterwheel"
    FOCUSER = "focuser"
    ROTATOR = "rotator"
    SWITCH = "switch"
    SAFETYMONITOR = "safetymonitor"
    COVERCALIBRATOR = "covercalibrator"
    TERTIARY = "tertiary"