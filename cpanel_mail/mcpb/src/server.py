"""Punto de entrada del bundle MCPB "Correo DMF".

El bundle lleva el paquete cpanel_mail_mcp copiado en src/ (build.sh lo toma
de ../src) y declara solo sus dependencias en pyproject.toml; uv las instala
en un entorno administrado la primera vez que Claude arranca la extension.
Este archivo valida la configuracion que el usuario capturo en el dialogo de
instalacion y arranca el servidor real sobre stdio.

Si el servidor no se puede importar, explica la causa con pasos que el usuario
o TI puedan seguir. El caso que motivo esto: en Windows, Smart App Control o
una politica de App Control (WDAC) bloquea las DLL nativas sin firma que uv
descarga (por ejemplo _pydantic_core.pyd), y el mensaje anterior culpaba a la
conexion a internet. Python solo deja en el ImportError el texto del error
traducido al idioma del sistema, asi que repetimos la carga con ctypes para
obtener el codigo numerico de Windows.

Todo lo que se imprima aqui va a stderr a proposito: stdout esta reservado
para el protocolo JSON-RPC de MCP y cualquier basura ahi rompe la sesion. Los
textos van sin acentos por si stderr no termina leyendose como UTF-8.
"""

from __future__ import annotations

import os
import re
import sys

REQUIRED = {
    "CPANEL_USER": "Correo electronico",
    "CPANEL_PASS": "Contrasena del buzon",
    "CPANEL_IMAP_HOST": "Servidor de correo",
}

# Codigos Win32 de LoadLibrary cuando una politica bloquea la DLL: 1260 es
# AppLocker / directiva de restriccion de software; 4550-4599 es la familia
# ERROR_SYSTEM_INTEGRITY_* de App Control (WDAC) y Smart App Control. El 4551
# es "Una directiva de Control de aplicaciones bloqueo este archivo".
_POLICY_CODES = frozenset({1260, *range(4550, 4600)})
# 225 / 226: el antivirus detecto el archivo o lo mando a cuarentena.
_ANTIVIRUS_CODES = frozenset({225, 226})

# Respaldo cuando no hay codigo: fragmentos del mensaje de Windows en ingles y
# espanol, comparados en minusculas.
_POLICY_MARKERS = (
    "application control",
    "control de aplicaciones",
    "smart app control",
    "system integrity policy",
    "integridad del sistema",
    "group policy",
    "directiva de grupo",
)
_ANTIVIRUS_MARKERS = ("virus", "unwanted software", "software potencialmente")

# CPython usa este formato cuando Windows no le da texto para el codigo.
_CODE_IN_MESSAGE = re.compile(r"error code (\d+)")

_SAC_KEY = r"SYSTEM\CurrentControlSet\Control\CI\Policy"
_SAC_VALUE = "VerifiedAndReputablePolicyState"  # 0 apagado, 1 activo, 2 evaluacion

_HEADLINES = {
    "policy": "Correo DMF no pudo arrancar: Windows bloqueo uno de sus componentes.",
    "antivirus": "Correo DMF no pudo arrancar: el antivirus bloqueo uno de sus componentes.",
    "native": "Correo DMF no pudo arrancar: Windows no pudo cargar uno de sus componentes.",
}

_SAC_ON = """\
No es un problema de internet ni de tu contrasena. En esta PC esta activado el
Control inteligente de aplicaciones (Smart App Control) de Windows, que no deja
cargar archivos sin firma digital como este.

Como resolverlo:
  1. Abre Seguridad de Windows > Control de aplicaciones y del explorador >
     Configuracion de Control inteligente de aplicaciones.
  2. Cambialo a "Desactivado". Si TI administra esta PC, consultalo antes con
     ellos: en algunas versiones de Windows ya no se puede volver a activar
     sin reinstalar.
  3. Cierra Claude por completo (tambien desde el icono junto al reloj) y
     vuelve a abrirlo."""

_IT_POLICY = """\
No es un problema de internet ni de tu contrasena. Una politica de control de
aplicaciones de la empresa (App Control for Business/WDAC, AppLocker o Intune)
no deja cargar este archivo. Smart App Control esta apagado en esta PC, asi
que el bloqueo viene de esa politica.

Como resolverlo: manda este mensaje a TI. Necesitan autorizar los archivos
.pyd y .dll de estas carpetas, de preferencia por hash o por firmante (una
regla por ruta dentro de AppData normalmente no basta):
{folders}
Despues cierra Claude por completo (tambien desde el icono junto al reloj) y
vuelve a abrirlo."""

_POLICY_UNKNOWN = """\
No es un problema de internet ni de tu contrasena. Una politica de control de
aplicaciones de Windows no deja cargar este archivo. Puede venir de:

  - El Control inteligente de aplicaciones (Smart App Control). Revisalo en
    Seguridad de Windows > Control de aplicaciones y del explorador >
    Configuracion de Control inteligente de aplicaciones. Si esta activado,
    cambialo a "Desactivado". Si TI administra esta PC, consultalo antes con
    ellos: en algunas versiones de Windows ya no se puede volver a activar
    sin reinstalar.
  - Una politica de TI (App Control for Business/WDAC, AppLocker o Intune).
    Manda este mensaje a TI: necesitan autorizar los archivos .pyd y .dll de
    estas carpetas, de preferencia por hash o por firmante:
{folders}

Despues cierra Claude por completo (tambien desde el icono junto al reloj) y
vuelve a abrirlo."""

_ANTIVIRUS = """\
No es un problema de internet ni de tu contrasena. El antivirus marco este
archivo como sospechoso y lo bloqueo o lo mando a cuarentena.

Como resolverlo: manda este mensaje a TI para que agreguen una exclusion para
esta carpeta y restauren el archivo de la cuarentena:
{folders}
Despues cierra Claude por completo (tambien desde el icono junto al reloj) y
vuelve a abrirlo."""

_NATIVE = """\
Prueba esto:
  1. Cierra Claude por completo (tambien desde el icono junto al reloj) y
     vuelve a abrirlo.
  2. Si sigue fallando, desinstala la extension Correo DMF y vuelve a
     instalarla.
  3. Si aun falla, manda este mensaje a TI: puede ser un antivirus o una
     politica de seguridad de Windows."""

_MISSING = """\
Correo DMF no pudo arrancar: falta un componente de la extension.

  Error original: {original}

La instalacion quedo incompleta o danada. Desinstala la extension Correo DMF y
vuelve a instalarla con internet disponible: la primera vez que arranca
descarga sus dependencias."""


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


def _use_utf8_stderr() -> None:
    """Escribe stderr en UTF-8, que es como Claude lee el log.

    En Windows Python usa cp1252 cuando stderr es un pipe, y los acentos del
    mensaje de Windows salian rotos ("bloque  este archivo").
    """
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def _find_load_error(exc: BaseException) -> BaseException:
    """Devuelve la excepcion de la cadena que reporta una DLL que no cargo."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and isinstance(getattr(current, "winerror", None), int):
            return current
        if isinstance(current, ImportError) and "DLL load failed" in str(current):
            return current
        current = current.__cause__ or current.__context__
    return exc


def _probe_load(path: str) -> int | None:
    """Repite la carga de la DLL y devuelve el codigo de GetLastError.

    Devuelve None fuera de Windows, si ctypes tampoco carga o si esta vez la
    DLL si cargo.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        load = kernel32.LoadLibraryExW
        load.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD)
        load.restype = wintypes.HMODULE
        # Mismas banderas que CPython al cargar extensiones:
        # LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR.
        handle = load(path, None, 0x00001000 | 0x00000100)
        if handle:
            kernel32.FreeLibrary.argtypes = (wintypes.HMODULE,)
            kernel32.FreeLibrary(handle)
            return None
        return ctypes.get_last_error() or None
    except Exception:
        return None


def _smart_app_control_state() -> int | None:
    """0 apagado, 1 activo, 2 evaluacion; None si no es Windows o no se pudo leer."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SAC_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _SAC_VALUE)
        return int(value)
    except (ImportError, OSError, TypeError, ValueError):
        return None


def _windows_error_code(err: BaseException) -> int | None:
    code = getattr(err, "winerror", None)
    if isinstance(code, int):
        return code
    match = _CODE_IN_MESSAGE.search(str(err))
    if match:
        return int(match.group(1))
    path = getattr(err, "path", None) or getattr(err, "filename", None)
    if isinstance(path, str) and path:
        return _probe_load(path)
    return None


def _classify(exc: BaseException) -> tuple[str, BaseException, int | None]:
    """Clasifica la falla: "policy", "antivirus", "native" o "missing"."""
    err = _find_load_error(exc)
    text = str(err)
    if not (isinstance(err, OSError) or "DLL load failed" in text):
        # Un modulo que no esta instalado o un import roto: no es una DLL.
        return "missing", err, None
    code = _windows_error_code(err)
    lowered = text.lower()
    if code in _POLICY_CODES:
        return "policy", err, code
    if code in _ANTIVIRUS_CODES:
        return "antivirus", err, code
    if any(marker in lowered for marker in _POLICY_MARKERS):
        return "policy", err, code
    if any(marker in lowered for marker in _ANTIVIRUS_MARKERS):
        return "antivirus", err, code
    return "native", err, code


def _extension_folders() -> list[str]:
    """El entorno de la extension y el Python que instalo uv."""
    folders = [sys.prefix]
    if sys.base_prefix != sys.prefix:
        folders.append(sys.base_prefix)
    return folders


def _folder_lines(folders: list[str]) -> str:
    return "\n".join("      " + folder for folder in folders)


def _explain_load_failure(exc: BaseException) -> str:
    kind, err, code = _classify(exc)
    original = str(err)
    if code is not None and str(code) not in original:
        original += f" (codigo {code})"

    if kind == "missing":
        return _MISSING.format(original=original)
    if kind == "policy":
        sac_state = _smart_app_control_state()
        template = _SAC_ON if sac_state == 1 else _IT_POLICY if sac_state == 0 else _POLICY_UNKNOWN
        advice = template.format(folders=_folder_lines(_extension_folders()))
    elif kind == "antivirus":
        advice = _ANTIVIRUS.format(folders=_folder_lines([sys.prefix]))
    else:
        advice = _NATIVE

    details = []
    blocked = getattr(err, "path", None) or getattr(err, "filename", None)
    if blocked:
        details.append(f"  Archivo:        {blocked}")
    details.append(f"  Error original: {original}")
    return "\n".join([_HEADLINES[kind], "", *details, "", advice])


def _load_server():
    from cpanel_mail_mcp.server import main as run_server

    return run_server


def main() -> None:
    _use_utf8_stderr()
    _clean_blank_optionals()
    _check_config()

    # Forzamos stdio: este bundle corre como subproceso de Claude, nunca como
    # servidor HTTP. Si alguien exporta MCP_TRANSPORT en su shell, lo ignoramos.
    os.environ["MCP_TRANSPORT"] = "stdio"

    try:
        run_server = _load_server()
    except (ImportError, OSError) as exc:
        try:
            message = _explain_load_failure(exc)
        except Exception:  # el diagnostico nunca debe tapar el error original
            message = f"Correo DMF no pudo arrancar: {exc!r}"
        print(message, file=sys.stderr)
        raise SystemExit(1) from exc

    run_server()


if __name__ == "__main__":
    main()
