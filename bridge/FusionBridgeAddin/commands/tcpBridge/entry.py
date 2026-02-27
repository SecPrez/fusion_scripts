"""TCP bridge command — exposes CAM data over a TCP socket on 127.0.0.1:54321.

Query from the external CLI:  fusion-cam setups
"""

import adsk.core
import adsk.cam
import json
import re
import socket
import threading
import traceback

from ...lib import fusionAddInUtils as futil

app = adsk.core.Application.get()

HOST = "127.0.0.1"
PORT = 54321

_server_thread: threading.Thread = None
_server_socket: socket.socket = None
_stop_event = threading.Event()

_custom_event_id = "FusionBridgeAddinTCPEvent"
_custom_event: adsk.core.CustomEvent = None

_pending = {}  # request_id -> {command, args, result_event, result}
_pending_lock = threading.Lock()
_next_id = 0


# ---------------------------------------------------------------------------
# Query helpers — these run on the main thread via CustomEvent
# ---------------------------------------------------------------------------

_OP_TYPE_NAMES = {
    0: "Milling",
    1: "Turning",
    2: "Cutting",
    3: "FFF",
    4: "Inspection",
}


def _op_type_str(op_type):
    try:
        return _OP_TYPE_NAMES.get(int(op_type), str(op_type))
    except Exception:
        return str(op_type)


def _get_cam():
    doc = app.activeDocument
    if doc is None:
        return None
    return adsk.cam.CAM.cast(doc.products.itemByProductType("CAMProductType"))


def _param_value(params, name):
    try:
        p = params.itemByName(name)
        if p is None:
            return None
        expr = p.expression
        try:
            return p.value.value
        except Exception:
            return expr
    except Exception:
        return None


def _tool_info(tool):
    if tool is None:
        return None
    info = {"description": tool.description}
    try:
        tp = tool.parameters
        for key in (
            "tool_diameter",
            "tool_numberOfFlutes",
            "tool_fluteLength",
            "tool_type",
            "tool_bodyLength",
            "tool_overallLength",
        ):
            info[key] = _param_value(tp, key)
    except Exception:
        pass
    return info


def _operation_detail(cam, op):
    detail = {
        "name": op.name,
        "strategy": op.strategy,
        "hasToolpath": op.hasToolpath,
        "isToolpathValid": op.isToolpathValid,
    }
    try:
        detail["hasError"] = op.hasError
        detail["hasWarning"] = op.hasWarning
    except Exception:
        pass
    try:
        detail["tool"] = _tool_info(op.tool)
    except Exception:
        detail["tool"] = None
    try:
        detail["machiningTime"] = cam.getMachiningTime(op)
    except Exception:
        detail["machiningTime"] = None

    params_of_interest = [
        "tool_feedCutting", "tool_feedEntry", "tool_feedExit",
        "tool_feedPlunge", "tool_feedRamp", "tool_spindleSpeed",
        "tolerance", "maximumStepover", "maximumStepdown",
        "optimalLoad", "useStockToLeave", "stockToLeave",
        "finishingOverlap",
    ]
    op_params = {}
    try:
        p = op.parameters
        for name in params_of_interest:
            v = _param_value(p, name)
            if v is not None:
                op_params[name] = v
    except Exception:
        pass
    detail["parameters"] = op_params
    return detail


def _handle_list_documents(_args):
    docs = []
    active_name = None
    if app.activeDocument:
        active_name = app.activeDocument.name
    for i in range(app.documents.count):
        doc = app.documents.item(i)
        docs.append({
            "name": doc.name,
            "isActive": doc.name == active_name,
        })
    return {"documents": docs, "activeDocument": active_name}


def _handle_switch_document(args):
    target = args.get("name", "")
    for i in range(app.documents.count):
        doc = app.documents.item(i)
        if doc.name == target:
            doc.activate()
            return {"activeDocument": doc.name}
    return {"error": f"Document '{target}' not found. Use 'docs' to list open documents."}


def _handle_list_setups(_args):
    cam = _get_cam()
    if cam is None:
        return {"error": "No CAM product found in the active document."}
    setups = []
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        setups.append({
            "name": s.name,
            "operationType": _op_type_str(s.operationType),
            "operationCount": s.allOperations.count,
        })
    return {"setups": setups}


def _handle_get_setup_detail(args):
    cam = _get_cam()
    if cam is None:
        return {"error": "No CAM product found in the active document."}
    target = args.get("setup_name", "")
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        if s.name == target:
            ops = []
            for j in range(s.allOperations.count):
                ops.append(_operation_detail(cam, s.allOperations.item(j)))
            return {
                "setup": {
                    "name": s.name,
                    "operationType": _op_type_str(s.operationType),
                    "isActive": s.isActive,
                    "operationCount": s.allOperations.count,
                    "operations": ops,
                }
            }
    return {"error": f"Setup '{target}' not found."}


def _handle_rename_operations(args):
    cam = _get_cam()
    if cam is None:
        return {"error": "No CAM product found in the active document."}
    target = args.get("setup_name", "")
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        if s.name == target:
            ops = s.allOperations
            if ops.count == 0:
                return {"error": f"Setup '{target}' has no operations."}

            first_op = ops.item(0)
            if first_op.strategy != "bore":
                return {"skipped": True, "reason": "First operation is not a bore."}

            renames = []
            for j in range(ops.count):
                op = ops.item(j)
                old_name = op.name
                if j == 0:
                    new_name = "Op1 Bore"
                else:
                    new_name = f"Op2 {op.name}"
                op.name = new_name
                renames.append({"old": old_name, "new": new_name})
            return {"renamed": renames}
    return {"error": f"Setup '{target}' not found."}


def _strip_doc_version(name):
    """Remove trailing ' v2', ' v12', etc. from a Fusion document name."""
    return re.sub(r"\s+v\d+$", "", name)


def _find_nc_program(cam, name):
    """Find an NC program by name, or return None."""
    for i in range(cam.ncPrograms.count):
        nc = cam.ncPrograms.item(i)
        if nc.name == name:
            return nc
    return None


def _set_nc_output_folder(nc, folder):
    """Set the output folder on an NC program via its parameters."""
    p = nc.parameters.itemByName("nc_program_output_folder")
    if p is not None:
        try:
            p.value.value = folder
        except Exception:
            try:
                p.expression = f'"{folder}"'
            except Exception:
                pass


def _handle_create_nc_programs(args):
    cam = _get_cam()
    if cam is None:
        return {"error": "No CAM product found in the active document."}
    target = args.get("setup_name", "")

    setup = None
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        if s.name == target:
            setup = s
            break
    if setup is None:
        return {"error": f"Setup '{target}' not found."}

    ops = setup.allOperations
    if ops.count == 0:
        return {"error": f"Setup '{target}' has no operations."}

    # Split operations into bores and others
    bore_ops = []
    other_ops = []
    for j in range(ops.count):
        op = ops.item(j)
        if op.strategy == "bore":
            bore_ops.append(op)
        else:
            other_ops.append(op)

    if not bore_ops:
        return {"error": "No bore operations found in setup."}

    # Delete all existing NC programs before creating fresh ones
    deleted = []
    for i in range(cam.ncPrograms.count - 1, -1, -1):
        nc = cam.ncPrograms.item(i)
        deleted.append(nc.name)
        nc.deleteMe()

    import os
    file_name = _strip_doc_version(app.activeDocument.name)
    output_dir = os.path.expanduser(args.get("output_folder", "~/Downloads"))
    op1_name = f"{file_name} Op1"
    op2_name = f"{file_name} Op2"

    created = []

    # Op1 — bore operations
    nc_input = cam.ncPrograms.createInput()
    nc_input.operations = bore_ops
    nc1 = cam.ncPrograms.add(nc_input)
    nc1.name = op1_name
    _set_nc_output_folder(nc1, output_dir)
    created.append({"name": op1_name, "status": "created",
                     "operations": [op.name for op in bore_ops]})

    # Op2 — everything else
    if other_ops:
        nc_input2 = cam.ncPrograms.createInput()
        nc_input2.operations = other_ops
        nc2 = cam.ncPrograms.add(nc_input2)
        nc2.name = op2_name
        _set_nc_output_folder(nc2, output_dir)
        created.append({"name": op2_name, "status": "created",
                         "operations": [op.name for op in other_ops]})

    return {"programs": created, "deleted": deleted}


def _next_file_version(output_dir, prefix):
    """Scan output_dir for '{prefix} v{N}.nc' and return N+1."""
    import os
    max_v = 0
    pattern = re.compile(re.escape(prefix) + r" v(\d+)\.\w+$")
    if os.path.isdir(output_dir):
        for f in os.listdir(output_dir):
            m = pattern.match(f)
            if m:
                max_v = max(max_v, int(m.group(1)))
    return max_v + 1


def _handle_post_nc_programs(args):
    import os
    cam = _get_cam()
    if cam is None:
        return {"error": "No CAM product found in the active document."}
    target = args.get("setup_name", "")

    setup = None
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        if s.name == target:
            setup = s
            break
    if setup is None:
        return {"error": f"Setup '{target}' not found."}

    file_name = _strip_doc_version(app.activeDocument.name)
    op1_name = f"{file_name} Op1"
    op2_name = f"{file_name} Op2"

    output_dir = os.path.expanduser(args.get("output_folder", "~/Downloads"))

    # Determine version — use same version for both, based on max found
    v1 = _next_file_version(output_dir, f"{file_name} Op1")
    v2 = _next_file_version(output_dir, f"{file_name} Op2")
    version = max(v1, v2)

    posted = []
    for nc_name, op_label in [(op1_name, "Op1"), (op2_name, "Op2")]:
        nc = _find_nc_program(cam, nc_name)
        if nc is None:
            continue
        out_filename = f"{file_name} {op_label} v{version}"
        # Set the output filename parameter
        fn_param = nc.parameters.itemByName("nc_program_filename")
        original_filename = None
        if fn_param is not None:
            try:
                original_filename = fn_param.expression
            except Exception:
                pass
            try:
                fn_param.value.value = out_filename
            except Exception:
                try:
                    fn_param.expression = out_filename
                except Exception:
                    pass
        post_options = adsk.cam.NCProgramPostProcessOptions.create()
        result = nc.postProcess(post_options)
        # Restore original filename
        if fn_param is not None and original_filename is not None:
            try:
                fn_param.value.value = original_filename
            except Exception:
                try:
                    fn_param.expression = original_filename
                except Exception:
                    pass
        posted.append({
            "ncProgram": nc_name,
            "file": out_filename,
            "version": version,
            "postResult": str(result),
        })

    if not posted:
        return {"error": f"No NC programs found ({op1_name}, {op2_name}). Run nc-create first."}

    return {"posted": posted, "version": version}


COMMANDS = {
    "list_documents": _handle_list_documents,
    "switch_document": _handle_switch_document,
    "list_setups": _handle_list_setups,
    "get_setup_detail": _handle_get_setup_detail,
    "rename_operations": _handle_rename_operations,
    "create_nc_programs": _handle_create_nc_programs,
    "post_nc_programs": _handle_post_nc_programs,
}


# ---------------------------------------------------------------------------
# CustomEvent callback — runs on the Fusion main thread
# ---------------------------------------------------------------------------

def _on_custom_event(event_args: adsk.core.CustomEventArgs):
    request_id = event_args.additionalInfo
    with _pending_lock:
        req = _pending.get(request_id)
    if req is None:
        return
    try:
        command = req["command"]
        args = req.get("args") or {}
        handler = COMMANDS.get(command)
        if handler is None:
            req["result"] = {"error": f"Unknown command: {command}"}
        else:
            req["result"] = handler(args)
    except Exception:
        req["result"] = {"error": traceback.format_exc()}
    finally:
        req["result_event"].set()


# ---------------------------------------------------------------------------
# TCP server (background thread)
# ---------------------------------------------------------------------------

def _handle_client(conn: socket.socket):
    global _next_id
    buf = b""
    try:
        while not _stop_event.is_set():
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    conn.sendall(json.dumps({"error": "Invalid JSON"}).encode() + b"\n")
                    continue

                evt = threading.Event()
                with _pending_lock:
                    rid = str(_next_id)
                    _next_id += 1
                    _pending[rid] = {
                        "command": msg.get("command", ""),
                        "args": msg.get("args"),
                        "result_event": evt,
                        "result": None,
                    }
                app.fireCustomEvent(_custom_event_id, rid)
                evt.wait(timeout=30)
                with _pending_lock:
                    result = _pending.pop(rid, {}).get("result")
                if result is None:
                    result = {"error": "Timeout waiting for Fusion main thread."}
                conn.sendall(json.dumps(result).encode() + b"\n")
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _server_loop():
    global _server_socket
    _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _server_socket.settimeout(1.0)
    _server_socket.bind((HOST, PORT))
    _server_socket.listen(2)
    futil.log(f"FusionBridgeAddin: TCP bridge listening on {HOST}:{PORT}")
    while not _stop_event.is_set():
        try:
            conn, _addr = _server_socket.accept()
            threading.Thread(target=_handle_client, args=(conn,), daemon=True).start()
        except socket.timeout:
            continue
        except OSError:
            break
    try:
        _server_socket.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Add-in command lifecycle (called by commands/__init__.py)
# ---------------------------------------------------------------------------

def start():
    global _custom_event, _server_thread
    _stop_event.clear()

    _custom_event = app.registerCustomEvent(_custom_event_id)
    futil.add_handler(_custom_event, _on_custom_event, name="tcp_bridge_event")

    _server_thread = threading.Thread(target=_server_loop, daemon=True)
    _server_thread.start()
    futil.log("FusionBridgeAddin: TCP bridge started.")


def stop():
    global _server_socket, _server_thread, _custom_event
    _stop_event.set()

    if _server_socket:
        try:
            _server_socket.close()
        except Exception:
            pass

    if _server_thread:
        _server_thread.join(timeout=3)

    try:
        app.unregisterCustomEvent(_custom_event_id)
    except Exception:
        pass

    futil.log("FusionBridgeAddin: TCP bridge stopped.")
