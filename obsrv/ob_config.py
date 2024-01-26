import os
import logging
from obcom.ob_config import SingletonConfig as SC_Common

logger = logging.getLogger(__name__.rsplit('.')[-1])


class SingletonConfig(SC_Common):
    app_name = 'ocabox-server'

    @staticmethod
    def _get_inst_dir():
        thisfile = __file__
        thisdir = os.path.dirname(thisfile)
        return thisdir

