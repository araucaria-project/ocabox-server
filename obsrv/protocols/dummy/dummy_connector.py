"""
Dummy Protocol Connector for Testing
Logs commands and provides example values for telescope mount operations.
"""
import logging
import time
from typing import Iterable, Callable, Tuple

logger = logging.getLogger(__name__.rsplit('.')[-1])


class Connector:
    """Base connector class for all telescope protocols."""

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        raise NotImplementedError

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        raise NotImplementedError

    async def call(self, component: 'Component', function: str, **data):
        raise NotImplementedError

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        raise NotImplementedError

    def __del__(self):
        pass


class DummyConnector(Connector):
    """Dummy connector that logs commands and returns example values for testing."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        logger.info('Dummy connector created for testing')
        self.connected = False
        self.slewing = False
        self.tracking = True
        # Example coordinates (approximate OCA location)
        self.ra = 15.5  # hours
        self.dec = -24.5  # degrees
        self.azimuth = 180.0  # degrees
        self.altitude = 45.0  # degrees

    async def get(self, component: 'Component', variable: str, kind=None, **data):
        """Handle GET requests with dummy responses."""
        logger.info(f"DUMMY GET: {component.name}.{variable} (kind={kind}, data={data})")
        
        # Mount-specific responses
        if hasattr(component, 'kind') and component.kind == 'telescope':
            if variable == 'connected':
                return self.connected
            elif variable == 'slewing':
                return self.slewing
            elif variable == 'tracking':
                return self.tracking
            elif variable == 'rightascension':
                return self.ra
            elif variable == 'declination':
                return self.dec
            elif variable == 'azimuth':
                return self.azimuth
            elif variable == 'altitude':
                return self.altitude
            elif variable == 'athome':
                return False
            elif variable == 'atpark':
                return False
            elif variable == 'canfindHome':
                return True
            elif variable == 'canpark':
                return True
            elif variable == 'canslew':
                return True
            elif variable == 'cantrack':
                return True
            elif variable == 'siderealtime':
                # Return current LST approximation
                return (time.time() / 3600) % 24
                
        # Generic responses for other components
        if variable == 'connected':
            return True
        elif variable == 'name':
            return f"Dummy {component.name}"
        elif variable == 'description':
            return f"Dummy {component.kind or 'device'} for testing"
        
        # Default response
        logger.warning(f"DUMMY GET: Unknown variable {variable} for {component.name}")
        return None

    async def put(self, component: 'Component', variable: str, kind=None, **data):
        """Handle PUT requests by logging the action."""
        logger.info(f"DUMMY PUT: {component.name}.{variable} = {data} (kind={kind})")
        
        # Mount-specific actions
        if hasattr(component, 'kind') and component.kind == 'telescope':
            if variable == 'connected':
                self.connected = data.get('value', data.get('Connected', False))
                logger.info(f"DUMMY: Mount connection set to {self.connected}")
            elif variable == 'tracking':
                self.tracking = data.get('value', data.get('Tracking', True))
                logger.info(f"DUMMY: Mount tracking set to {self.tracking}")
            elif variable == 'rightascension':
                self.ra = data.get('value', data.get('RightAscension', self.ra))
                logger.info(f"DUMMY: Mount RA set to {self.ra}")
            elif variable == 'declination':
                self.dec = data.get('value', data.get('Declination', self.dec))
                logger.info(f"DUMMY: Mount Dec set to {self.dec}")
                
        return True

    async def call(self, component: 'Component', function: str, **data):
        """Handle function calls by logging the action."""
        logger.info(f"DUMMY CALL: {component.name}.{function}({data})")
        
        # Mount-specific function calls
        if hasattr(component, 'kind') and component.kind == 'telescope':
            if function == 'slewtocoordinates':
                ra = data.get('RightAscension', data.get('ra'))
                dec = data.get('Declination', data.get('dec'))
                logger.info(f"DUMMY: Starting slew to RA={ra}, Dec={dec}")
                self.slewing = True
                self.ra = ra if ra is not None else self.ra
                self.dec = dec if dec is not None else self.dec
                # In real implementation, would start async slew and update slewing status
            elif function == 'abortslew':
                logger.info("DUMMY: Aborting slew")
                self.slewing = False
            elif function == 'park':
                logger.info("DUMMY: Parking mount")
                self.slewing = False
            elif function == 'unpark':
                logger.info("DUMMY: Unparking mount")
            elif function == 'findhome':
                logger.info("DUMMY: Finding home position")
                self.slewing = False
            elif function == 'setpark':
                logger.info("DUMMY: Setting park position")
                
        return True

    async def subscribe(self, variables: Iterable[Tuple[str, str]], callback: Callable):
        """Subscribe to variable changes (dummy implementation)."""
        logger.info(f"DUMMY SUBSCRIBE: {list(variables)}")
        # In a real implementation, this would set up periodic callbacks
        pass

    def __del__(self):
        logger.info("Dummy connector destroyed")