import os
import logging
import multiprocessing
import threading
import confuse

from obsrv.utils.singleton import SingletonMeta

logger = logging.getLogger(__name__.rsplit('.')[-1])


class SingletonConfig(metaclass=SingletonMeta):
    app_name = 'ocabox-server'
    _DEFAULT_CONFIG_FILE_NAME = 'config.yaml'
    _SECRET_CONFIG_FILE_NAME = '.secrets.yaml'
    _config: confuse.ConfigView = None
    additional_files = []

    @classmethod
    def get_config(cls, add_files: list = None, rebuild=False) -> confuse.ConfigView:
        """
        Returns configuration singleton
        """
        with multiprocessing.Lock():
            if cls._config is None or rebuild:
                if add_files is not None:
                    cls.additional_files += add_files
                cls._config = cls._parse_config_files()
        return cls._config

    @classmethod
    def add_config_file(cls, f: str):
        with multiprocessing.Lock():
            cls.additional_files += [f]

    @classmethod
    def add_config_file_from_config_dir(cls, f: str):
        cls.add_config_file(os.path.join(cls.get_path_to_config_dir(), f))

    @staticmethod
    def _get_inst_dir():
        thisfile = __file__
        thisdir = os.path.dirname(thisfile)
        return thisdir

    @classmethod
    def get_package_file_path(cls, f):
        """
        Returns absolute path to file in package directory.
        """
        return os.path.join(cls._get_inst_dir(), f)

    @classmethod
    def get_path_to_config_dir(cls):
        return os.path.join(cls._get_inst_dir(), 'configuration')

    @classmethod
    def _get_default_config_files(cls):
        # from every classes inheriting from this one: /obsrv/config.yaml / /ob/config.yaml / else...
        return [os.path.join(cls._get_inst_dir(), cls._DEFAULT_CONFIG_FILE_NAME)]

    @classmethod
    def _get_system_config_files(cls):
        return [
            os.path.join('/usr/local/etc/ocabox', cls._DEFAULT_CONFIG_FILE_NAME),  # /etc/ocabox/config.yaml
            os.path.join('/usr/local/etc/ocabox', cls._SECRET_CONFIG_FILE_NAME)  # /etc/ocabox/.secrets.yaml
        ]

    @classmethod
    def _get_volume_config_files(cls):
        return [
            os.path.join(cls._get_inst_dir(), 'configuration', cls._DEFAULT_CONFIG_FILE_NAME),
            os.path.join(cls._get_inst_dir(), 'configuration', cls._SECRET_CONFIG_FILE_NAME),
            # /ob/configuration/config.yaml
        ]

    @classmethod
    def get_config_files(cls):
        # first is getting by SingletonConfig because we get a original config.yaml from obcom, rest is getting by
        # cls so will be getting from the project files inheriting from this project
        # for example first is always obcom/config.yaml but second will be changed to for example
        # obsrv/config.yaml or ob/config.yaml
        return SingletonConfig._get_default_config_files() + \
            cls._get_default_config_files() + \
            cls._get_system_config_files() + \
            cls._get_volume_config_files() + \
            cls.additional_files

    @classmethod
    def _parse_config_files(cls, files: list or None = None, parse_default_locations=True):
        """Parses set of config files"""
        fil = []
        if parse_default_locations:
            fil += [os.path.join(os.path.expanduser(d)) for d in cls.get_config_files()]
        if files is not None:
            fil += files
        config = confuse.Configuration(cls.app_name, read=False)
        for f in fil:
            logger.debug('Trying config: %s', f)
            try:
                source = confuse.YamlSource(f, optional=True)
                config.set(source)
                if len(source) > 0:
                    logger.info('Using config: %s', f)
                else:
                    logger.info('Empty config: %s', f)
            except confuse.ConfigReadError:
                logger.warning('Corrupted config: %s', f)
        return config


class OBConfig:

    def __init__(self):
        self._config_file: list = []
        self._config: confuse.ConfigView or None = None

    @property
    def config(self) -> confuse.ConfigView:
        if self._config is None:
            with threading.Lock():
                if self._config is None:
                    logger.info('Looking for config files')
                    self._config = SingletonConfig.get_config(add_files=self._config_file)
        return self._config

    @config.setter
    def config(self, config: confuse.ConfigView):
        self._config = config

    @staticmethod
    def fast_config():
        with threading.Lock():
            config = SingletonConfig.get_config()
        return config
