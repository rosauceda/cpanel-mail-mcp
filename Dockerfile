# cpanel-mail-mcp — HTTP MCP server image (Dokploy, Docker, Compose).
#
# Default mode: MCP_AUTH_MODE=credentials — each client (e.g. n8n) sends its
# mailbox login in X-Email-User / X-Email-Password headers; the container
# stores no passwords. Required env: CPANEL_HOST (your IMAP/SMTP server).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp

USER mcp
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080 \
    MCP_AUTH_MODE=credentials
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('MCP_PORT', '8080'), timeout=3)"]

CMD ["cpanel-mail-mcp"]
