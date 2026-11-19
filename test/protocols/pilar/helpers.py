"""Shared helpers of the Pilar tests: protocol settings in short times and a component stand-in."""
import copy
from types import SimpleNamespace

from obsrv.protocols.pilar.pilar_connector import load_protocol_config


def fast_settings(**changes) -> dict:
    """pilar_config.yml with times short enough for the fake Pilar. ``changes``: 'section.key' -> value."""
    cfg = copy.deepcopy(load_protocol_config())
    cfg['link'].update({'reconnect_cooldown': 0.5})
    cfg['timeouts'].update({'connect': 1.0, 'greeting': 1.0, 'command': 0.5, 'pool_wait': 1.0,
                            'stalled_connection': 5.0})
    cfg['watcher'].update({'poll_interval': 0.05, 'snapshot_max_age': 0.1, 'freeze_after': 0.2,
                           'freeze_suspect_after': 0.1, 'freeze_snapshot_max_age': 3.0,
                           'post_freeze_guard': 0.4, 'outage_after': 2.0, 'command_freeze_wait': 3.0,
                           'retrack_retry': 0.1, 'retrack_window': 1.0})
    cfg['mount'].update({'speed_window': 0.1, 'settle_time': 0.2, 'start_delay_altaz': 0.15,
                         'start_delay_track': 0.5, 'start_delay_abort': 0.3, 'max_slew_time': 10.0,
                         'sync_slew_timeout': 10.0, 'queue_wait': 10.0})
    cfg['procedures']['power'].update({'timeout_on': 5.0, 'timeout_off': 10.0})
    cfg['procedures']['cover'].update({'timeout': 5.0})
    cfg['procedures']['focuser'].update({'speed': 0.25, 'timeout_margin': 2.0, 'still_time': 0.1})
    cfg['procedures']['derotator'].update({'still_time': 0.1, 'timeout': 5.0})
    for path, value in changes.items():
        node = cfg
        *parents, key = path.split('.')
        for p in parents:
            node = node[p]
        node[key] = value
    return cfg


def component(kind: str, address: str, device_number: int = 0, epoch=2000, sys_id=None):
    """What the connector uses of a device-tree component."""
    options = {'address': address, 'epoch': epoch}
    return SimpleNamespace(kind=kind, sys_id=sys_id or f'iris.{kind}', device_nr=device_number,
                           get_option_recursive=lambda name: options.get(name))
