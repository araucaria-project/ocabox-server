"""Refuse to import against an ocabox-common older than the API this server calls."""
from importlib.metadata import PackageNotFoundError, version

from packaging.version import Version

REQUIRED = {
    "ocabox-common": "1.6.0",  # obcom.optics (optical-element kinds)
    "datamodels": "0.5.0",  # datamodels.messages (NATS service driver)
    "serverish": "2.2.1",  # TreeNatsService: on 2.0.x it subscribes but never receives the status
}


def check_dependency_versions(installed=version) -> None:
    for dist, minimum in REQUIRED.items():
        try:
            found = installed(dist)
        except PackageNotFoundError as e:
            raise ImportError(f"{dist} is not installed; ocabox-server requires >= {minimum}") from e
        if Version(found) < Version(minimum):
            raise ImportError(f"ocabox-server requires {dist} >= {minimum}, found {found}")
