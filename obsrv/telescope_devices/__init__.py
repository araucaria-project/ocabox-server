# The drivers subclass the tree's contracts, so they are imported after it, here: any use of the tree has them.
from obsrv.telescope_devices import drivers  # noqa: F401
