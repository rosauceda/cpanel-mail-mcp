"""The Claude Desktop bundle wrapper (mcpb/src/server.py) explains import failures.

Windows blocks are simulated: the probe (LoadLibraryExW) and the Smart App
Control registry read are replaced, so these run on any OS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "mcpb" / "src" / "server.py"
PYD = r"C:\ext\.venv\Lib\site-packages\pydantic_core\_pydantic_core.cp312-win_amd64.pyd"
BLOCKED_ES = (
    "DLL load failed while importing _pydantic_core: "
    "Una directiva de Control de aplicaciones bloqueó este archivo."
)


@pytest.fixture
def wrapper(monkeypatch):
    spec = importlib.util.spec_from_file_location("mcpb_wrapper", WRAPPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_probe_load", lambda path: None)
    monkeypatch.setattr(module, "_smart_app_control_state", lambda: None)
    return module


def dll_error(message: str = BLOCKED_ES) -> ImportError:
    return ImportError(message, name="_pydantic_core", path=PYD)


def test_app_control_block_with_smart_app_control_on(wrapper, monkeypatch):
    monkeypatch.setattr(wrapper, "_smart_app_control_state", lambda: 1)
    msg = wrapper._explain_load_failure(dll_error())
    assert msg.startswith("Correo DMF no pudo arrancar: Windows bloqueo")
    assert PYD in msg
    assert "Smart App Control" in msg and '"Desactivado"' in msg
    assert "No es un problema de internet" in msg
    assert "Revisa que tengas conexion" not in msg
    assert "manda este mensaje a TI" not in msg


def test_app_control_block_with_smart_app_control_off_points_to_it(wrapper, monkeypatch):
    monkeypatch.setattr(wrapper, "_smart_app_control_state", lambda: 0)
    msg = wrapper._explain_load_failure(dll_error())
    assert "manda este mensaje a TI" in msg
    assert "Smart App Control esta apagado" in msg
    assert '"Desactivado"' not in msg
    for folder in wrapper._extension_folders():
        assert folder in msg


def test_app_control_block_with_unknown_state_lists_both_causes(wrapper):
    msg = wrapper._explain_load_failure(dll_error())
    assert '"Desactivado"' in msg
    assert "Manda este mensaje a TI" in msg


def test_english_windows_message_is_recognised(wrapper):
    err = dll_error(
        "DLL load failed while importing _pydantic_core: "
        "An Application Control policy has blocked this file."
    )
    assert wrapper._classify(err)[0] == "policy"


def test_probe_code_works_in_any_windows_language(wrapper, monkeypatch):
    monkeypatch.setattr(wrapper, "_probe_load", lambda path: 4551)
    err = dll_error(
        "DLL load failed while importing _pydantic_core: "
        "Eine Anwendungssteuerungsrichtlinie hat diese Datei blockiert."
    )
    kind, _, code = wrapper._classify(err)
    assert (kind, code) == ("policy", 4551)
    assert "(codigo 4551)" in wrapper._explain_load_failure(err)


def test_probe_gets_the_blocked_file_path(wrapper, monkeypatch):
    seen = []
    monkeypatch.setattr(wrapper, "_probe_load", lambda path: seen.append(path))
    wrapper._classify(dll_error())
    assert seen == [PYD]


def test_code_without_windows_text(wrapper):
    err = dll_error("DLL load failed with error code 4551 while importing _pydantic_core")
    kind, _, code = wrapper._classify(err)
    assert (kind, code) == ("policy", 4551)


def test_oserror_with_winerror(wrapper):
    class BlockedLoad(OSError):
        winerror = 1260

    err = BlockedLoad("blocked")
    err.filename = r"C:\ext\some.dll"
    assert wrapper._classify(err)[0] == "policy"
    assert r"C:\ext\some.dll" in wrapper._explain_load_failure(err)


def test_antivirus_block(wrapper, monkeypatch):
    monkeypatch.setattr(wrapper, "_probe_load", lambda path: 225)
    msg = wrapper._explain_load_failure(dll_error("DLL load failed while importing _rust: x"))
    assert "antivirus" in msg and "exclusion" in msg


def test_other_dll_failure_is_generic(wrapper, monkeypatch):
    monkeypatch.setattr(wrapper, "_probe_load", lambda path: 126)
    err = dll_error("DLL load failed while importing _rust: No se puede encontrar el módulo especificado.")
    assert wrapper._classify(err)[0] == "native"
    msg = wrapper._explain_load_failure(err)
    assert "desinstala la extension" in msg
    assert "Smart App Control" not in msg


def test_missing_module_asks_for_reinstall(wrapper, monkeypatch):
    probed = []
    monkeypatch.setattr(wrapper, "_probe_load", lambda path: probed.append(path))
    msg = wrapper._explain_load_failure(ModuleNotFoundError("No module named 'mcp'", name="mcp"))
    assert "falta un componente" in msg and "No module named 'mcp'" in msg
    assert "Smart App Control" not in msg
    assert probed == []


def test_broken_python_import_is_not_probed_as_a_dll(wrapper, monkeypatch):
    # "cannot import name" errors carry the .py path; it must not be loaded as a DLL.
    probed = []
    monkeypatch.setattr(wrapper, "_probe_load", lambda path: probed.append(path))
    err = ImportError(
        "cannot import name 'FastMCP' from 'mcp.server'",
        name="mcp.server",
        path="/x/mcp/server/__init__.py",
    )
    assert wrapper._classify(err)[0] == "missing"
    assert probed == []


def test_dll_error_found_through_exception_chain(wrapper):
    try:
        try:
            raise dll_error()
        except ImportError as inner:
            raise ImportError("pydantic could not be imported") from inner
    except ImportError as outer:
        kind, err, _ = wrapper._classify(outer)
    assert kind == "policy" and err.path == PYD


def test_message_text_is_ascii_apart_from_windows_text(wrapper, monkeypatch):
    err = dll_error("DLL load failed while importing _pydantic_core: blocked")
    for state in (0, 1, None):
        monkeypatch.setattr(wrapper, "_smart_app_control_state", lambda state=state: state)
        monkeypatch.setattr(wrapper, "_probe_load", lambda path: 4551)
        assert wrapper._explain_load_failure(err).isascii()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("CPANEL_USER", "user@example.com")
    monkeypatch.setenv("CPANEL_PASS", "secret")
    monkeypatch.setenv("CPANEL_IMAP_HOST", "mail.example.com")
    # main() sets MCP_TRANSPORT itself; this makes monkeypatch undo it afterwards.
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)


def test_main_prints_diagnosis_to_stderr_only(wrapper, configured, monkeypatch, capsys):
    monkeypatch.setattr(wrapper, "_smart_app_control_state", lambda: 1)

    def blocked():
        raise dll_error()

    monkeypatch.setattr(wrapper, "_load_server", blocked)
    with pytest.raises(SystemExit) as exit_info:
        wrapper.main()
    assert exit_info.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""  # stdout is the MCP JSON-RPC channel
    assert "Smart App Control" in err and PYD in err


def test_main_falls_back_if_diagnosis_fails(wrapper, configured, monkeypatch, capsys):
    def boom(exc):
        raise RuntimeError("diagnosis bug")

    def blocked():
        raise dll_error()

    monkeypatch.setattr(wrapper, "_explain_load_failure", boom)
    monkeypatch.setattr(wrapper, "_load_server", blocked)
    with pytest.raises(SystemExit):
        wrapper.main()
    assert "_pydantic_core" in capsys.readouterr().err


def test_main_runs_the_server_when_import_works(wrapper, configured, monkeypatch):
    calls = []
    monkeypatch.setattr(wrapper, "_load_server", lambda: lambda: calls.append("run"))
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    wrapper.main()
    assert calls == ["run"]
    assert wrapper.os.environ["MCP_TRANSPORT"] == "stdio"
