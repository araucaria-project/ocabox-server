"""
Pillar Protocol Connector for IRIS telescope mount control.
Implements the base Connector interface for Pillar telescope systems.
"""
import asyncio
import logging
from typing import Iterable, Callable, Tuple

from obsrv.protocols.alpaca.alpaca_connector import Connector

logger = logging.getLogger(__name__.rsplit('.')[-1])


class PillarConnector(Connector):
    """Connector for Pillar telescope mount protocol."""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._connected = False
        logger.info('Pillar connector created')
    
    async def get(self, component: 'Component', variable: str, kind=None, **data):
        """Get a value from Pillar telescope system."""
        if not self._connected:
            raise RuntimeError("Pillar connector not connected")
        
        # Mock implementation for demonstration
        if component.kind == "telescope" and variable == "rightascension":
            return 12.5  # Mock RA
        elif component.kind == "telescope" and variable == "declination":
            return 45.0  # Mock Dec
        elif component.kind == "telescope" and variable == "tracking":
            return True  # Mock tracking status
        elif component.kind == "focuser" and variable == "position":
            return 5000  # Mock focuser position
        else:
            logger.warning(f"Unknown Pillar GET: {component.kind}.{variable}")
            return None
    
    async def put(self, component: 'Component', variable: str, kind=None, **data):
        """Send a command to Pillar telescope system."""
        if not self._connected:
            raise RuntimeError("Pillar connector not connected")
        
        # Mock implementation for demonstration
        if component.kind == "telescope" and variable == "slewtocoordinates":
            ra = data.get('RightAscension', 0)
            dec = data.get('Declination', 0)
            logger.info(f"Pillar: Slewing to RA={ra}, Dec={dec}")
            return {"status": "slewing_started", "estimated_time": 30.0}
        elif component.kind == "focuser" and variable == "move":
            position = data.get('Position', 0)
            logger.info(f"Pillar: Moving focuser to {position}")
            return {"status": "moving", "target_position": position}
        else:
            logger.warning(f"Unknown Pillar PUT: {component.kind}.{variable}")
            return {"status": "unknown_command"}
    
    async def call(self, component: 'Component', function: str, **data):
        """Call a function on Pillar system."""
        logger.info(f"Pillar CALL: {component.kind}.{function}")
        return {"status": "called", "function": function}
    
    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        """Subscribe to Pillar system updates."""
        logger.info(f"Pillar SUBSCRIBE: {list(variables)}")
        # Mock subscription - in real implementation would set up websocket/polling
        pass
    
    async def connect(self):
        """Connect to Pillar telescope system."""
        # Mock connection
        await asyncio.sleep(0.1)  # Simulate connection time
        self._connected = True
        logger.info("Connected to Pillar telescope system")
        return True
    
    async def disconnect(self):
        """Disconnect from Pillar telescope system."""
        self._connected = False
        logger.info("Disconnected from Pillar telescope system")