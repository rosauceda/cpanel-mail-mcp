"""cpanel-mail-mcp — MCP server for IMAP/SMTP email accounts."""
from importlib.metadata import PackageNotFoundError, version

from .server import main, mcp

try:
    __version__ = version("cpanel-mail-mcp")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0+unknown"
__all__ = ["main", "mcp", "__version__"]
