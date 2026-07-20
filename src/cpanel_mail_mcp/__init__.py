"""cpanel-mail-mcp — MCP server for IMAP/SMTP email accounts."""
from .server import main, mcp

__version__ = "0.3.0"
__all__ = ["main", "mcp", "__version__"]
