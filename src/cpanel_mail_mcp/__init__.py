"""cpanel-mail-mcp — MCP server for IMAP/SMTP email accounts."""
from .server import main, mcp

__version__ = "0.5.3"
__all__ = ["main", "mcp", "__version__"]
