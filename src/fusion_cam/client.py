"""TCP client that talks to the FusionCAMBridge add-in inside Fusion 360."""

import json
import socket

from .queries import (
    CMD_CREATE_NC_PROGRAMS, CMD_GET_SETUP_DETAIL, CMD_LIST_DOCUMENTS,
    CMD_LIST_SETUPS, CMD_POST_NC_PROGRAMS, CMD_RENAME_OPERATIONS,
    CMD_SWITCH_DOCUMENT,
)


class FusionCAMClient:
    """Connect to the FusionCAMBridge and query CAM data."""

    def __init__(self, host: str = "127.0.0.1", port: int = 54321):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None

    # -- connection management ------------------------------------------------

    def connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self.host, self.port))

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- low-level protocol ---------------------------------------------------

    def _send_command(self, command: str, **args) -> dict:
        if self._sock is None:
            raise RuntimeError("Not connected. Call connect() first.")
        payload = {"command": command}
        if args:
            payload["args"] = args
        self._sock.sendall(json.dumps(payload).encode() + b"\n")
        return self._recv_response()

    def _recv_response(self) -> dict:
        buf = b""
        while b"\n" not in buf:
            chunk = self._sock.recv(8192)
            if not chunk:
                raise ConnectionError("Connection closed by bridge.")
            buf += chunk
        line, _ = buf.split(b"\n", 1)
        return json.loads(line)

    # -- public queries -------------------------------------------------------

    def list_documents(self) -> dict:
        """Return list of open documents and which is active."""
        resp = self._send_command(CMD_LIST_DOCUMENTS)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp

    def switch_document(self, name: str) -> dict:
        """Switch the active document by name."""
        resp = self._send_command(CMD_SWITCH_DOCUMENT, name=name)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp

    def list_setups(self) -> list[dict]:
        """Return a list of CAM setup summaries."""
        resp = self._send_command(CMD_LIST_SETUPS)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp["setups"]

    def get_setup_detail(self, setup_name: str) -> dict:
        """Return full detail for a single setup (including operations)."""
        resp = self._send_command(CMD_GET_SETUP_DETAIL, setup_name=setup_name)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp["setup"]

    def rename_operations(self, setup_name: str) -> dict:
        """Rename operations in a setup (Op1 Bore, Op2 ..., Op3 ...)."""
        resp = self._send_command(CMD_RENAME_OPERATIONS, setup_name=setup_name)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp

    def create_nc_programs(self, setup_name: str, output_folder: str | None = None) -> dict:
        """Create NC programs: bores in Op1, everything else in Op2."""
        kwargs = {"setup_name": setup_name}
        if output_folder is not None:
            kwargs["output_folder"] = output_folder
        resp = self._send_command(CMD_CREATE_NC_PROGRAMS, **kwargs)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp

    def post_nc_programs(self, setup_name: str, output_folder: str | None = None) -> dict:
        """Post-process NC programs, outputting versioned .nc files."""
        kwargs = {"setup_name": setup_name}
        if output_folder is not None:
            kwargs["output_folder"] = output_folder
        resp = self._send_command(CMD_POST_NC_PROGRAMS, **kwargs)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp
