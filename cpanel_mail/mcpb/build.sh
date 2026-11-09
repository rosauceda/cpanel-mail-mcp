#!/usr/bin/env bash
# Arma dist/cpanel-mail-dmf-<version>.mcpb, la extension "Correo DMF" para
# Claude Desktop: el manifest y el wrapper de esta carpeta mas el paquete
# cpanel_mail_mcp de ../src, que va copiado dentro del bundle porque PyPI solo
# tiene 0.7.0.
#
# Uso: mcpb/build.sh   (necesita Node para npx)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
mcpb="@anthropic-ai/mcpb@2.1.2"

version="$(sed -n 's/^  "version": "\(.*\)",$/\1/p' "$here/manifest.json")"
py_version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$here/pyproject.toml")"
if [[ -z "$version" || "$version" != "$py_version" ]]; then
  echo "La version de manifest.json ($version) y la de pyproject.toml ($py_version) no coinciden." >&2
  exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

cp "$here/manifest.json" "$here/pyproject.toml" "$here/icon.png" "$root/LICENSE" "$stage/"
mkdir -p "$stage/src"
cp "$here/src/server.py" "$stage/src/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$root/src/cpanel_mail_mcp" "$stage/src/"

mkdir -p "$root/dist"
out="$root/dist/cpanel-mail-dmf-$version.mcpb"
npx -y "$mcpb" validate "$stage/manifest.json"
npx -y "$mcpb" pack "$stage" "$out"
echo "Listo: $out"
