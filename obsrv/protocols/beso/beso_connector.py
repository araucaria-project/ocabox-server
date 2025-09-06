"""
BESO Protocol Connector for spectrograph control.
Implements the base Connector interface for BESO spectrograph systems.
"""
import asyncio
import logging
from typing import Iterable, Callable, Tuple

from obsrv.protocols.alpaca.alpaca_connector import Connector

logger = logging.getLogger(__name__.rsplit('.')[-1])


class BesoConnector(Connector):
    """Connector for BESO spectrograph protocol."""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._connected = False
        logger.info('BESO connector created')
    
    async def get(self, component: 'Component', variable: str, kind=None, **data):
        """Get a value from BESO spectrograph system."""
        if not self._connected:
            raise RuntimeError("BESO connector not connected")
        
        # Mock implementation for demonstration
        if component.kind == "camera" and variable == "temperature":
            return -110.0  # Mock cooled camera temperature
        elif component.kind == "camera" and variable == "exposurestate":
            return "idle"  # Mock exposure state
        elif component.kind == "switch" and variable == "position":
            return "science"  # Mock optical switch position
        elif variable == "calibration_status":
            return {"thar_lamp": False, "bias_lamp": True}
        else:
            logger.warning(f"Unknown BESO GET: {component.kind}.{variable}")
            return None
    
    async def put(self, component: 'Component', variable: str, kind=None, **data):
        """Send a command to BESO spectrograph system."""
        if not self._connected:
            raise RuntimeError("BESO connector not connected")
        
        # Mock implementation for demonstration
        if component.kind == "camera" and variable == "startexposure":
            exposure_time = data.get('Duration', 1.0)
            logger.info(f"BESO: Starting {exposure_time}s exposure")
            return {"status": "exposure_started", "exposure_time": exposure_time}
        elif component.kind == "switch" and variable == "setposition":
            position = data.get('Position', 'science')
            logger.info(f"BESO: Setting optical switch to {position}")
            return {"status": "moving", "target_position": position}
        elif variable == "thar_lamp":
            state = data.get('State', False)
            logger.info(f"BESO: ThAr lamp {'ON' if state else 'OFF'}")
            return {"status": "lamp_set", "state": state}
        else:
            logger.warning(f"Unknown BESO PUT: {component.kind}.{variable}")
            return {"status": "unknown_command"}
    
    async def call(self, component: 'Component', function: str, **data):
        """Call a function on BESO system."""
        logger.info(f"BESO CALL: {component.kind}.{function}")
        return {"status": "called", "function": function}
    
    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        """Subscribe to BESO system updates."""
        logger.info(f"BESO SUBSCRIBE: {list(variables)}")
        # Mock subscription - in real implementation would set up TCP/HTTP polling
        pass
    
    async def connect(self):
        """Connect to BESO spectrograph system."""
        # Mock connection
        await asyncio.sleep(0.1)  # Simulate connection time
        self._connected = True
        logger.info("Connected to BESO spectrograph system")
        return True
    
    async def disconnect(self):
        """Disconnect from BESO spectrograph system."""
        self._connected = False
        logger.info("Disconnected from BESO spectrograph system")