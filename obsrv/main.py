#!/usr/bin/env python
import asyncio
import importlib
import importlib.util
import logging
import os
import sys
from obsrv.ob_config import SingletonConfig

import signal

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger('main')


# config najpierw z ob/config.yaml potem z configuration.config.yaml
# taka wartośc w env dla build file OCABOX_BUILD_FILE_NAME


def load_from_file(filename, method_name):
    """Method importing a file by given name and return method witch give name from it"""
    method_inst = None
    mod_name, file_ext = os.path.splitext(os.path.split(filename)[-1])
    if file_ext.lower() == '.py':
        if os.path.isabs(filename):
            spec = importlib.util.spec_from_file_location(mod_name, filename)
            py_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(py_mod)
        else:
            py_mod = importlib.import_module('obsrv.configuration.' + mod_name, __name__)
    else:
        logger.error(f"wrong file type witch tree build. Expected '.py' file got: {filename}")
        raise RuntimeError
    if hasattr(py_mod, method_name):
        method_inst = getattr(py_mod, method_name)()
    else:
        logger.error(f"Can not find method '{method_name}' in file {filename}")
        raise RuntimeError
    return method_inst

def _splash():
    splash = r"""
_/_/_/_/_/  _/_/_/    _/_/_/   
   _/        _/    _/          
  _/        _/    _/           
 _/        _/    _/            
_/      _/_/_/    _/_/_/   v2
                           OCABOX Server       
"""

    print(splash)

def main(argv=None):
    if argv is None:
        argv = sys.argv

    _splash()
    try:
        level = SingletonConfig.get_config()['LOG_LEVEL'].get()
        if level is not None:
            logger.info(f'Setting Loglevel to {level}')
            logging.getLogger().setLevel(level)
    except Exception:
        pass
    try:
        ocabox_version = SingletonConfig.get_config()['OCABOX_VERSION'].get()
    except Exception:
        ocabox_version = "1"
    if ocabox_version[0] != '2':
        logger.error(f"The version of the configuration is {ocabox_version}. Required version is 2. Check config.yaml, check tree_build.py, set OCABOX_VERSION in your config file")
        logger.error(f"Note, that OCABOX Server 2 has different packages paths to be imported, e.g. obsrv.data_colection.specialistic_components is depreaced")
        return 1
    try:
        BUILD_FILE = os.environ.get('OCABOX_BUILD_FILE_NAME', None)
        if BUILD_FILE is None:
            BUILD_FILE = SingletonConfig.get_config()['OCABOX_BUILD_FILE_NAME'].get()
        if BUILD_FILE is None:
            logger.error(f"Can not find any bu§ild tree file")
            raise RuntimeError
        logger.info(f"Tree will be build from: {BUILD_FILE}")

        vr = load_from_file(BUILD_FILE, 'tree_build')
        rs = vr.request_solver
    except RuntimeError as e:
        logger.error(f"Aborting: {e}")
        return 1

    coro = vr.main_coroutine()

    try:
        loop = asyncio.get_running_loop()
        logger.warning("WARNING! Async loop is run before the main method is called")
    except RuntimeError:
        loop = asyncio.new_event_loop()

    def ask_exit():
        raise KeyboardInterrupt
    loop.add_signal_handler(signal.SIGINT, ask_exit)

    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(rs.run_tree())
        loop.run_until_complete(coro)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            # cancel router tasks
            vr.stop()
            vr_stop = asyncio.gather(vr.get_stop_task(), rs.stop_tree(), return_exceptions=True)
            loop.run_until_complete(vr_stop)

            # make sure if all task is finished (router task and every other in this loop)
            all_tasks = asyncio.all_tasks(loop)
            if not all_tasks:
                logger.info('All task in loop is finished')
            else:
                logger.error('Some of the tasks in current loop is still running')
                # This is not a good solution, some tasks may be still running, the loop is not canceling yet. (mka)
                # For now, we stick to the main task closing mechanism, so this shouldn't happen. If we change something, it will be changed
                raise RuntimeError


            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

    return 0


if __name__ == '__main__':
    retcode = main(sys.argv)
    sys.exit(retcode)
