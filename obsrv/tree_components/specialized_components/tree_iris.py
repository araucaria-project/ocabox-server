"""
TreeIrisObservatory - tree adapter of the IRIS telescope.

IRIS mixes protocols (Pilar: mount, focuser, cover; ALPACA: dome; IRIS-CCD: camera, filter wheel). The universal
:class:`Observatory` already gives every component the connector of its own ``protocol``, so this adapter is the
ALPACA one (request routing, timeouts, deadline shedding) with its own configuration section
(``data_collection.TreeIrisObservatory``) and the IRIS-CCD sessions closed when the tree stops.
"""
import logging

from obsrv.protocols.iris_ccd.iris_ccd_connector import IrisCcdConnector
from obsrv.tree_components.specialized_components.tree_alpaca import TreeAlpacaObservatory

logger = logging.getLogger(__name__.rsplit('.')[-1])


class TreeIrisObservatory(TreeAlpacaObservatory):
    """
    Tree adapter of the IRIS observatory.

    :param component_name: Name of tree component for debugging
    :param observatory_name: Observatory name used to get configuration from config file (``tree.<name>``)
    """

    COMPONENT_DEFAULT_NAME: str = 'TreeIrisObservatory'

    async def stop(self):
        await IrisCcdConnector.aclose_all()
        await super().stop()
