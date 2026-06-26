"""Patent Creator backend package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("patent-creator-backend")
except PackageNotFoundError:
    __version__ = "0.0.0"
