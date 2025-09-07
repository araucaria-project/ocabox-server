"""
Universal Connector Factory - Creates protocol-specific connectors.
Lives in protocols/ because it creates protocol connectors.
"""
import logging

logger = logging.getLogger(__name__.rsplit('.')[-1])


def _load_all_protocols():
    """Load all available protocol connectors."""
    classes = {}
    
    # Try to load ALPACA protocol
    try:
        from obsrv.protocols.alpaca.alpaca_connector import AlpacaConnector
        classes['alpaca'] = AlpacaConnector
    except ImportError:
        pass
    
    # Try to load Pillar protocol
    try:
        from obsrv.protocols.pillar.pillar_connector import PillarConnector
        classes['pillar'] = PillarConnector
    except ImportError:
        pass
    
    # Try to load BESO protocol  
    try:
        from obsrv.protocols.beso.beso_connector import BesoConnector
        classes['beso'] = BesoConnector
    except ImportError:
        pass
    
    # Try to load IRIS CCD protocol
    try:
        from obsrv.protocols.iris_ccd.iris_ccd_connector import IrisCcdConnector
        classes['iris_ccd'] = IrisCcdConnector
    except ImportError:
        pass
    
    # Try to load Dummy protocol (for testing)
    try:
        from obsrv.protocols.dummy.dummy_connector import DummyConnector
        classes['dummy'] = DummyConnector
    except ImportError:
        pass
    
    return classes


def create_connector(protocol: str):
    """Create a connector for the specified protocol."""
    connector_classes = _load_all_protocols()
    
    if protocol not in connector_classes:
        available = list(connector_classes.keys())
        raise ValueError(f"Unknown protocol: {protocol}. Available: {available}")
    
    return connector_classes[protocol]()