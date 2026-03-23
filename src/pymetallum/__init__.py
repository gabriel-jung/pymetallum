from importlib.metadata import version

from . import app, core

__version__ = version("pymetallum")

__all__ = [
    "__version__",
    "app",
    "core",
]
