import confuse
from abc import ABC
from typing import List

from obcom.comunication.base_zmq_communication_object import BaseZmqCommunicationObject

from obsrv.ob_config import SingletonConfig


class BaseRouterWithConfig(BaseZmqCommunicationObject, ABC):
    DEFAULT_NAME = 'SocketConfigReader'
    TYPE = 'default_socket_config_reader'
    _SING_CONF = SingletonConfig

    def __init__(self, name: str = None, port: int = None, **kwargs):
        super().__init__(name=name, port=port, **kwargs)

    def get_cfg(self, name_cfg: str, default=None, use_default_settings=True):
        return self._get_cfg(name_cfg, default, use_default_settings)

    def get_cfg_deep(self, name_cfg: List[str], default=None, use_default_settings=True):
        return self._get_cfg_deep(name_cfg, default, use_default_settings)

    def _get_cfg_deep(self, name_cfg: List[str], default=None, use_default_settings=True):
        """
        The method looks deep for a value in the configuration file and returns it.

        :param name_cfg: list of names in dist config value
        :param default: default value if key not exist in config
        :param use_default_settings: use default settings if default value is None and can not get settings
        :return: config value or None if method can't find it
        """
        def build_request(name):
            c = self._SING_CONF.get_config()[self.TYPE][name]
            for n in name_cfg:
                c = c[n]
            return c
        try:
            value = build_request(self.name).get()
        except confuse.exceptions.NotFoundError:
            if default is None and use_default_settings:
                try:
                    value = build_request(self.DEFAULT_NAME).get()
                except confuse.exceptions.NotFoundError:
                    value = default
            else:
                value = default
        return value

    def _get_cfg(self, name_cfg: str, default=None, use_default_settings=True):
        """
        The method looks for a value in the configuration file and returns it.

        :param name_cfg: name of config value
        :param default: default value if key not exist in config
        :param use_default_settings: use default settings if default value is None and can not get settings
        :return: config value or None if method can't find it
        """
        return self._get_cfg_deep(name_cfg=[name_cfg], default=default, use_default_settings=use_default_settings)