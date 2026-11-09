# Correo DMF (extension de Claude Desktop)

Fuente del bundle `.mcpb` que se instala en Claude Desktop. Corre el servidor
en la computadora del usuario, sobre stdio, con la cuenta que captura al
instalar la extension.

- `manifest.json`: nombre, campos de configuracion y lista de herramientas.
- `pyproject.toml`: dependencias que uv instala la primera vez que arranca.
- `src/server.py`: wrapper que valida la configuracion, arranca el servidor y,
  si no carga, explica por que en el log de la extension.
- `icon.png`

El paquete `cpanel_mail_mcp` no se duplica aqui: `build.sh` lo copia de
`../src` al armar el bundle, porque PyPI solo tiene 0.7.0 (la version que
confunde numeros de secuencia con UIDs).

## Armar

```bash
./mcpb/build.sh
```

Deja `dist/cpanel-mail-dmf-<version>.mcpb`. Antes de armar una version nueva
sube `version` en `manifest.json` y en `pyproject.toml`; el script se niega si
no coinciden.
