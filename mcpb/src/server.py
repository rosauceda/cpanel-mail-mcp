"""Punto de entrada del bundle MCPB para cpanel-mail-mcp.

El bundle no lleva el servidor adentro: declara `cpanel-mail-mcp` como
dependencia en pyproject.toml y uv lo instala en un entorno administrado la
primera vez que Claude arranca la extension. Este archivo solo valida la
configuracion que el usuario capturo en el dialogo de instalacion y arranca
el servidor real sobre stdio.

Todo lo que se imprima aqui va a stderr a proposito: stdout esta reservado
para el protocolo JSON-RPC de MCP y cualquier basura ahi rompe la sesion.
"""

from __future__ import annotations

import os
import sys

REQUIRED = {
    "CPANEL_USER": "Correo electronico",
    "CPANEL_PASS": "Contrasena del buzon",
    "CPANEL_IMAP_HOST": "Servidor de correo",
}


def _clean_blank_optionals() -> None:
    """Borra las variables opcionales que llegaron vacias.

    Claude sustituye ${user_config.x} por cadena vacia cuando el campo es
    opcional y el usuario no lo lleno. Dejar CPANEL_FROM_NAME="" haria que
    los correos salgan con un display name vacio, y un
    EMAIL_SEND_CONFIRMATION_CODE="" es falsy pero mejor no dejarlo puesto.
    """
    for var in ("CPANEL_FROM_NAME", "EMAIL_SEND_CONFIRMATION_CODE"):
        if var in os.environ and not os.environ[var].strip():
            del os.environ[var]


def _check_config() -> None:
    missing = [label for var, label in REQUIRED.items() if not os.environ.get(var, "").strip()]
    if missing:
        print(
            "No se pudo iniciar la extension: falta configuracion.\n"
            "Campos vacios: " + ", ".join(missing) + "\n"
            "Abre Configuracion > Extensiones > Correo DMF y completalos.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> None:
    _clean_blank_optionals()
    _check_config()

    # Forzamos stdio: este bundle corre como subproceso de Claude, nunca como
    # servidor HTTP. Si alguien exporta MCP_TRANSPORT en su shell, lo ignoramos.
    os.environ["MCP_TRANSPORT"] = "stdio"

    try:
        from cpanel_mail_mcp.server import main as run_server
    except ImportError as exc:  # pragma: no cover - depende del entorno de uv
        print(
            f"No se pudo cargar cpanel-mail-mcp ({exc}).\n"
            "Revisa que tengas conexion a internet la primera vez que abres la "
            "extension: uv necesita descargar las dependencias una sola vez.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    run_server()


if __name__ == "__main__":
    main()
