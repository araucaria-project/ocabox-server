"""
IRIS CCD Protocol Connector for infrared camera control.
Implements the base Connector interface for custom IRIS CCD driver.
"""
import asyncio
import logging
from typing import Iterable, Callable, Tuple

from obsrv.protocols.alpaca.alpaca_connector import Connector

logger = logging.getLogger(__name__.rsplit('.')[-1])


class IrisCcdConnector(Connector):
    """Connector for IRIS CCD custom driver protocol."""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._connected = False
        self._exposure_state = "idle"
        logger.info('IRIS CCD connector created')
    
    async def get(self, component: 'Component', variable: str, kind=None, **data):
        """Get a value from IRIS CCD system."""
        if not self._connected:
            raise RuntimeError("IRIS CCD connector not connected")
        
        # Mock implementation for demonstration
        if component.kind == "camera" and variable == "temperature":
            return -80.0  # Mock cooled IR camera temperature
        elif component.kind == "camera" and variable == "exposurestate":
            return self._exposure_state  # Mock exposure state
        elif component.kind == "camera" and variable == "coolerstatus":
            return "cooling"  # Mock cooler status
        elif component.kind == "switch" and variable == "shutter":
            return "closed"  # Mock shutter state
        else:
            logger.warning(f"Unknown IRIS CCD GET: {component.kind}.{variable}")
            return None
    
    async def put(self, component: 'Component', variable: str, kind=None, **data):
        """Send a command to IRIS CCD system."""
        if not self._connected:
            raise RuntimeError("IRIS CCD connector not connected")
        
        # Mock implementation for demonstration
        if component.kind == "camera" and variable == "startexposure":
            exposure_time = data.get('Duration', 1.0)
            self._exposure_state = "exposing"
            logger.info(f"IRIS CCD: Starting {exposure_time}s exposure")
            
            # Simulate exposure completion
            asyncio.create_task(self._simulate_exposure(exposure_time))
            
            return {"status": "exposure_started", "exposure_time": exposure_time}
        elif component.kind == "camera" and variable == "settemperature":
            temp = data.get('Temperature', -80.0)
            logger.info(f"IRIS CCD: Setting temperature to {temp}°C")
            return {"status": "cooling", "target_temp": temp}
        elif component.kind == "switch" and variable == "shutter":
            state = data.get('State', 'closed')
            logger.info(f"IRIS CCD: Setting shutter {state}")
            return {"status": "shutter_set", "state": state}
        else:
            logger.warning(f"Unknown IRIS CCD PUT: {component.kind}.{variable}")
            return {"status": "unknown_command"}
    
    async def _simulate_exposure(self, duration: float):
        """Simulate exposure completion after duration."""
        await asyncio.sleep(duration)
        self._exposure_state = "ready"
        logger.info(f"IRIS CCD: Exposure completed")
    
    async def call(self, component: 'Component', function: str, **data):
        """Call a function on IRIS CCD system."""
        logger.info(f"IRIS CCD CALL: {component.kind}.{function}")
        return {"status": "called", "function": function}
    
    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        """Subscribe to IRIS CCD system updates."""
        logger.info(f"IRIS CCD SUBSCRIBE: {list(variables)}")
        # Mock subscription - in real implementation would set up driver callbacks
        pass
    
    async def connect(self):
        """Connect to IRIS CCD system."""
        # Mock connection
        await asyncio.sleep(0.1)  # Simulate connection time
        self._connected = True
        logger.info("Connected to IRIS CCD system")
        return True
    
    async def disconnect(self):
        """Disconnect from IRIS CCD system."""
        self._connected = False
        logger.info("Disconnected from IRIS CCD system")