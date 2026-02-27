# Fusion CAM — Project Guide

## Architecture

Two-component system: a **bridge add-in** running inside Fusion 360 and a **Python client** that talks to it over TCP.

```
┌───────────────────────┐         TCP :54321         ┌──────────────────┐
│   Fusion 360          │  ◄──── JSON + newline ────► │  fusion-cam CLI  │
│   FusionBridgeAddin   │                             │  fusion-cam-gui  │
│   (Python add-in)     │                             │  FusionCAMClient │
└───────────────────────┘                             └──────────────────┘
```

The bridge runs a TCP server on `127.0.0.1:54321` in a background thread. Requests are marshalled to Fusion's main thread via `CustomEvent` (required for all Fusion API access). The client sends newline-delimited JSON commands and receives newline-delimited JSON responses.

## Project Structure

```
fusion/
├── CLAUDE.md                         # This file
├── pyproject.toml                    # Package config, entry points
├── bridge/
│   └── FusionBridgeAddin/
│       ├── FusionBridgeAddin.py      # Add-in entry (run/stop)
│       ├── config.py                 # DEBUG flag, ADDIN_NAME
│       └── commands/
│           └── tcpBridge/entry.py    # TCP server + all CAM query handlers
└── src/
    └── fusion_cam/
        ├── __init__.py               # Exports FusionCAMClient
        ├── queries.py                # Command name constants
        ├── client.py                 # FusionCAMClient (TCP client)
        ├── cli.py                    # CLI entry point (fusion-cam)
        └── gui.py                    # GUI entry point (fusion-cam-gui)
```

## Entry Points

| Command          | Module              | Description                   |
|------------------|---------------------|-------------------------------|
| `fusion-cam`     | `fusion_cam.cli`    | CLI with subcommands          |
| `fusion-cam-gui` | `fusion_cam.gui`    | Tkinter GUI                   |

## Bridge Commands

All commands use `{"command": "name", "args": {...}}` request format. Responses are JSON objects (with `"error"` key on failure).

| Command              | Args                  | Response                                      |
|----------------------|-----------------------|-----------------------------------------------|
| `list_documents`     | —                     | `{documents: [{name, isActive}], activeDocument}` |
| `switch_document`    | `{name}`              | `{activeDocument}`                            |
| `list_setups`        | —                     | `{setups: [{name, operationType, operationCount}]}` |
| `get_setup_detail`   | `{setup_name}`        | `{setup: {name, operationType, isActive, operations: [...]}}` |
| `rename_operations`  | `{setup_name}`        | `{renamed: [{old, new}]}` or `{skipped, reason}` |
| `create_nc_programs` | `{setup_name}`        | `{programs: [{name, status, operations}]}`    |
| `post_nc_programs`   | `{setup_name}`        | `{posted: [{ncProgram, file, version}], version}` |

## Client API (`FusionCAMClient`)

```python
with FusionCAMClient(host="127.0.0.1", port=54321) as client:
    client.list_documents()                    # -> dict
    client.switch_document("Part v3")          # -> dict
    client.list_setups()                       # -> list[dict]
    client.get_setup_detail("Setup1")          # -> dict
    client.rename_operations("Setup1")         # -> dict
    client.create_nc_programs("Setup1")        # -> dict
    client.post_nc_programs("Setup1")          # -> dict
```

All methods raise `RuntimeError` on bridge errors and `ConnectionError` on socket issues.

## Development Workflow

### Bridge Add-In Setup
The add-in directory must be **symlinked** into Fusion's add-ins folder:
- macOS: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
- Windows: `%APPDATA%/Autodesk/Autodesk Fusion 360/API/AddIns/`

### Restart Requirements
After editing bridge code (`bridge/FusionBridgeAddin/`), you must **stop and restart** the add-in in Fusion 360 (Utilities > Add-Ins > Stop, then Run). There is no hot-reload.

Client-side code changes (`src/fusion_cam/`) take effect immediately if installed with `pip install -e .`.

### Installing
```bash
pip install -e .
```

## Known Quirks & API Notes

- **`nc_program_filename` param**: Set via `fn_param.value.value = name` (not `.expression`). Falls back to `.expression` if that fails.
- **`NCProgramPostProcessOptions.create()`**: Takes no arguments — call it bare.
- **`nc_input.operations` assignment**: Accepts a plain Python list of operation objects (e.g., `nc_input.operations = [op1, op2]`).
- **Tool dimensions in cm**: Fusion stores tool diameters in centimeters. Multiply by 10 for mm display.
- **Document name versioning**: Fusion appends ` v2`, ` v3`, etc. to document names. The bridge strips this when creating NC program names (`re.sub(r"\s+v\d+$", "", name)`).
- **Operation type enum**: `{0: Milling, 1: Turning, 2: Cutting, 3: FFF, 4: Inspection}`.
- **Threading**: All Fusion API calls must run on the main thread. The bridge uses `CustomEvent` + `fireCustomEvent()` to marshal from the TCP thread. Event handlers are kept in a global list to prevent garbage collection.
- **Timeout**: Bridge requests time out after 30 seconds.
- **GUI dependency**: The GUI uses PySide6 (Qt for Python, LGPL). The CLI and client library have no external dependencies.
