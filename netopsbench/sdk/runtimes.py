"""Public runtime manager exports."""

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime.manager import RuntimeManager as _RuntimeManager
from netopsbench.platform.runtime.manager import RuntimePool as _RuntimePool

RuntimeManager = _RuntimeManager
RuntimePool = _RuntimePool

RuntimeManager.__module__ = __name__
RuntimePool.__module__ = __name__

__all__ = ["RuntimeIdentity", "RuntimeManager", "RuntimePool"]
