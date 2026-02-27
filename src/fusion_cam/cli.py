"""CLI entry point for fusion-cam."""

import argparse
import re
import sys

from .client import FusionCAMClient


def _fmt_time(seconds) -> str:
    """Format a machining-time value (seconds or None) as e.g. '12m 34s'."""
    if seconds is None:
        return "N/A"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _fmt_diameter(val) -> str:
    if val is None:
        return ""
    try:
        mm = float(val) * 10  # Fusion stores tool dims in cm internally
        return f"\u00d8{mm:.2f}mm"
    except (TypeError, ValueError):
        return str(val)


def _toolpath_status(op: dict) -> str:
    if op.get("hasError"):
        return "Error"
    if not op.get("hasToolpath"):
        return "No toolpath"
    if op.get("isToolpathValid"):
        return "Valid \u2713"
    return "Invalid"


def _list_documents(client: FusionCAMClient):
    result = client.list_documents()
    docs = result.get("documents", [])
    if not docs:
        print("No documents open in Fusion 360.")
        return
    print("Open documents:")
    for i, d in enumerate(docs, 1):
        marker = " *" if d.get("isActive") else ""
        print(f"  {i}. {d['name']}{marker}")


def _switch_document(client: FusionCAMClient, name: str):
    result = client.switch_document(name)
    print(f"Switched to: {result['activeDocument']}")


def _print_setups(client: FusionCAMClient):
    setups = client.list_setups()
    if not setups:
        print("No CAM setups found in the active document.")
        return
    print("CAM Setups in active document:")
    for i, s in enumerate(setups, 1):
        op_type = s.get("operationType", "Unknown")
        count = s.get("operationCount", 0)
        print(f"  {i}. {s['name']} [{op_type}] \u2014 {count} operation{'s' if count != 1 else ''}")


def _print_setup_detail(client: FusionCAMClient, name: str):
    detail = client.get_setup_detail(name)
    print(f"Setup: {detail['name']}")
    print(f"Type: {detail.get('operationType', 'Unknown')}")
    print(f"Active: {'Yes' if detail.get('isActive') else 'No'}")
    ops = detail.get("operations", [])
    if not ops:
        print("\n  (no operations)")
        return
    print(f"\nOperations:")
    for i, op in enumerate(ops, 1):
        print(f"\n  {i}. {op['name']}")
        print(f"     Strategy: {op.get('strategy', 'N/A')}")

        tool = op.get("tool")
        if tool:
            desc = tool.get("description", "Unknown tool")
            diameter = _fmt_diameter(tool.get("tool_diameter"))
            flutes = tool.get("tool_numberOfFlutes")
            extra = []
            if diameter:
                extra.append(diameter)
            if flutes is not None:
                extra.append(f"{int(flutes)} flute{'s' if int(flutes) != 1 else ''}")
            if extra:
                desc = f"{desc} ({', '.join(extra)})"
            print(f"     Tool: {desc}")

        print(f"     Toolpath: {_toolpath_status(op)}")
        print(f"     Machining Time: {_fmt_time(op.get('machiningTime'))}")

        params = op.get("parameters", {})
        if params:
            print("     Parameters:")
            for k, v in params.items():
                label = re.sub(r"([a-z])([A-Z])", r"\1 \2", k.replace("tool_", "")).replace("_", " ").title()
                print(f"       {label}: {v}")


def _rename_operations(client: FusionCAMClient, name: str):
    result = client.rename_operations(name)
    if result.get("skipped"):
        print(f"Skipped: {result['reason']}")
        return
    renames = result.get("renamed", [])
    print(f"Renamed {len(renames)} operations in {name}:")
    for r in renames:
        print(f"  {r['old']}  ->  {r['new']}")


def _create_nc_programs(client: FusionCAMClient, name: str):
    result = client.create_nc_programs(name)
    programs = result.get("programs", [])
    for p in programs:
        status = p["status"]
        ops = p.get("operations")
        if ops:
            print(f"  {p['name']}  [{status}] ({', '.join(ops)})")
        else:
            print(f"  {p['name']}  [{status}]")


def _post_nc_programs(client: FusionCAMClient, name: str):
    result = client.post_nc_programs(name)
    version = result.get("version")
    posted = result.get("posted", [])
    print(f"Posted {len(posted)} NC program{'s' if len(posted) != 1 else ''} (v{version}):")
    for p in posted:
        print(f"  {p['ncProgram']}  ->  {p['file']}")


def main():
    parser = argparse.ArgumentParser(
        prog="fusion-cam",
        description="Query CAM data from a running Fusion 360 instance via FusionCAMBridge.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bridge host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=54321, help="Bridge port (default: 54321)")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("docs", help="List open documents (* = active)")
    switch_parser = sub.add_parser("switch", help="Switch the active document")
    switch_parser.add_argument("name", help="Document name")
    sub.add_parser("setups", help="List all CAM setups")
    detail_parser = sub.add_parser("setup", help="Show detail for a specific setup")
    detail_parser.add_argument("name", help="Setup name")
    rename_parser = sub.add_parser("rename", help="Rename operations (Op1 Bore, Op2 ..., Op3 ...)")
    rename_parser.add_argument("name", help="Setup name")
    nc_parser = sub.add_parser("nc-create", help="Create NC programs (bores=Op1, rest=Op2)")
    nc_parser.add_argument("name", help="Setup name")
    post_parser = sub.add_parser("post", help="Post-process NC programs to ~/Downloads with auto-versioned filenames")
    post_parser.add_argument("name", help="Setup name")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    try:
        with FusionCAMClient(host=args.host, port=args.port) as client:
            if args.command == "docs":
                _list_documents(client)
            elif args.command == "switch":
                _switch_document(client, args.name)
            elif args.command == "setups":
                _print_setups(client)
            elif args.command == "setup":
                _print_setup_detail(client, args.name)
            elif args.command == "rename":
                _rename_operations(client, args.name)
            elif args.command == "nc-create":
                _create_nc_programs(client, args.name)
            elif args.command == "post":
                _post_nc_programs(client, args.name)
    except ConnectionRefusedError:
        print(
            "Could not connect to FusionCAMBridge.\n"
            "Make sure Fusion 360 is running and the FusionCAMBridge add-in is started.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
