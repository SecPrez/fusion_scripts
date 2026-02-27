"""PySide6 GUI for fusion-cam — point-and-click CAM workflow."""

import threading

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QPushButton, QTreeWidget,
    QTreeWidgetItem, QGroupBox, QStatusBar, QHeaderView, QFileDialog,
)

from .client import FusionCAMClient

# ---------------------------------------------------------------------------
# Helpers (mirrored from cli.py)
# ---------------------------------------------------------------------------

def _fmt_time(seconds) -> str:
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
        mm = float(val) * 10
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


# ---------------------------------------------------------------------------
# Signal bridge — marshal results from worker threads to the Qt main thread
# ---------------------------------------------------------------------------

class _SignalBridge(QObject):
    result_ready = Signal(object, object)  # (callback, result)
    error_ready = Signal(str)              # status message


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class FusionCAMWindow(QMainWindow):
    def __init__(self, host: str = "127.0.0.1", port: int = 54321):
        super().__init__()
        self.host = host
        self.port = port

        self._docs: list[dict] = []
        self._setups: list[dict] = []
        self._operations: list[dict] = []
        self._selected_setup: str | None = None

        self._signals = _SignalBridge()
        self._signals.result_ready.connect(self._on_result)
        self._signals.error_ready.connect(self._set_status)

        self.setWindowTitle("Fusion CAM")
        self.resize(640, 540)
        self.setMinimumSize(500, 400)

        self._build_ui()
        self._refresh()

    # -- UI construction ----------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Top bar
        top = QHBoxLayout()
        title = QLabel("Fusion CAM")
        font = title.font()
        font.setPointSize(16)
        font.setBold(True)
        title.setFont(font)
        top.addWidget(title)
        top.addStretch()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._refresh)
        top.addWidget(self.btn_refresh)
        layout.addLayout(top)

        # Documents
        doc_group = QGroupBox("Documents")
        doc_layout = QVBoxLayout(doc_group)
        self.doc_list = QListWidget()
        self.doc_list.currentRowChanged.connect(self._on_doc_select)
        doc_layout.addWidget(self.doc_list)
        layout.addWidget(doc_group)

        # Setups
        setup_group = QGroupBox("Setups")
        setup_layout = QVBoxLayout(setup_group)
        self.setup_list = QListWidget()
        self.setup_list.currentRowChanged.connect(self._on_setup_select)
        setup_layout.addWidget(self.setup_list)
        layout.addWidget(setup_group)

        # Operations
        ops_group = QGroupBox("Operations")
        ops_layout = QVBoxLayout(ops_group)
        self.ops_tree = QTreeWidget()
        self.ops_tree.setHeaderLabels(["Name", "Strategy", "Tool", "Time", "Status"])
        header = self.ops_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.ops_tree.setColumnWidth(0, 140)
        self.ops_tree.setColumnWidth(1, 90)
        self.ops_tree.setColumnWidth(3, 65)
        self.ops_tree.setColumnWidth(4, 75)
        self.ops_tree.setRootIsDecorated(False)
        ops_layout.addWidget(self.ops_tree)
        layout.addWidget(ops_group, stretch=1)

        # Buttons
        btn_row = QHBoxLayout()
        self.btn_rename = QPushButton("Rename Ops")
        self.btn_rename.clicked.connect(self._rename_ops)
        btn_row.addWidget(self.btn_rename)
        self.btn_nc = QPushButton("NC Create")
        self.btn_nc.clicked.connect(self._nc_create)
        btn_row.addWidget(self.btn_nc)
        self.btn_post = QPushButton("Post Process")
        self.btn_post.clicked.connect(self._post_process)
        btn_row.addWidget(self.btn_post)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Output folder
        import os
        self._output_folder = os.path.expanduser("~/Downloads")
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Output:"))
        self.folder_label = QLabel(self._output_folder)
        self.folder_label.setStyleSheet("color: gray;")
        folder_row.addWidget(self.folder_label, stretch=1)
        self.btn_browse = QPushButton("Browse\u2026")
        self.btn_browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.btn_browse)
        layout.addLayout(folder_row)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    # -- Background bridge calls -------------------------------------------

    def _run_in_bg(self, func, callback, error_prefix="Error"):
        """Run *func(client)* in a background thread, emit signal with result."""
        def _worker():
            try:
                with FusionCAMClient(self.host, self.port) as client:
                    result = func(client)
                self._signals.result_ready.emit(callback, result)
            except ConnectionRefusedError:
                self._signals.error_ready.emit(
                    "Cannot connect \u2014 is the bridge running?")
            except Exception as exc:
                self._signals.error_ready.emit(f"{error_prefix}: {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_result(self, callback, result):
        callback(result)

    # -- Status helpers -----------------------------------------------------

    def _set_status(self, text: str):
        self.status_bar.showMessage(text)

    def _set_buttons_enabled(self, enabled: bool):
        for btn in (self.btn_rename, self.btn_nc, self.btn_post):
            btn.setEnabled(enabled)

    # -- Refresh ------------------------------------------------------------

    def _refresh(self):
        self._set_status("Refreshing\u2026")
        self._set_buttons_enabled(False)
        self._run_in_bg(
            lambda c: c.list_documents(),
            self._on_docs_loaded,
            error_prefix="Refresh failed",
        )

    def _on_docs_loaded(self, result: dict):
        self._docs = result.get("documents", [])
        self.doc_list.blockSignals(True)
        self.doc_list.clear()
        active_idx = None
        for i, d in enumerate(self._docs):
            label = d["name"]
            if d.get("isActive"):
                label += "  (active)"
                active_idx = i
            self.doc_list.addItem(label)
        if active_idx is not None:
            self.doc_list.setCurrentRow(active_idx)
        self.doc_list.blockSignals(False)
        self._set_status("Ready")
        self._load_setups()

    # -- Document selection -------------------------------------------------

    def _on_doc_select(self, row: int):
        if row < 0 or row >= len(self._docs):
            return
        doc = self._docs[row]
        if doc.get("isActive"):
            self._load_setups()
            return
        self._set_status(f"Switching to {doc['name']}\u2026")
        self._run_in_bg(
            lambda c: c.switch_document(doc["name"]),
            self._on_doc_switched,
            error_prefix="Switch failed",
        )

    def _on_doc_switched(self, result: dict):
        self._set_status(f"Switched to {result.get('activeDocument', '?')}")
        self._run_in_bg(
            lambda c: c.list_documents(),
            self._on_docs_loaded,
            error_prefix="Refresh failed",
        )

    # -- Setups -------------------------------------------------------------

    def _load_setups(self):
        self._run_in_bg(
            lambda c: c.list_setups(),
            self._on_setups_loaded,
            error_prefix="Setups failed",
        )

    def _on_setups_loaded(self, setups: list):
        self._setups = setups
        self.setup_list.blockSignals(True)
        self.setup_list.clear()
        for s in setups:
            op_type = s.get("operationType", "?")
            count = s.get("operationCount", 0)
            self.setup_list.addItem(
                f"{s['name']} [{op_type}] \u2014 {count} operation{'s' if count != 1 else ''}")
        self.setup_list.blockSignals(False)
        if setups:
            self.setup_list.setCurrentRow(0)
            self._load_setup_detail(setups[0]["name"])
        else:
            self._clear_ops()
            self._set_buttons_enabled(False)

    # -- Setup selection / operations ---------------------------------------

    def _on_setup_select(self, row: int):
        if row < 0 or row >= len(self._setups):
            return
        self._load_setup_detail(self._setups[row]["name"])

    def _load_setup_detail(self, name: str):
        self._selected_setup = name
        self._set_status(f"Loading {name}\u2026")
        self._run_in_bg(
            lambda c: c.get_setup_detail(name),
            self._on_detail_loaded,
            error_prefix="Detail failed",
        )

    def _on_detail_loaded(self, detail: dict):
        self._operations = detail.get("operations", [])
        self._populate_ops()
        self._set_buttons_enabled(True)
        self._set_status("Ready")

    def _populate_ops(self):
        self.ops_tree.clear()
        for op in self._operations:
            tool = op.get("tool") or {}
            desc = tool.get("description", "")
            diameter = _fmt_diameter(tool.get("tool_diameter"))
            if diameter:
                desc = f"{desc} ({diameter})" if desc else diameter
            item = QTreeWidgetItem([
                op.get("name", ""),
                op.get("strategy", ""),
                desc,
                _fmt_time(op.get("machiningTime")),
                _toolpath_status(op),
            ])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.ops_tree.addTopLevelItem(item)

    def _clear_ops(self):
        self._operations = []
        self.ops_tree.clear()

    # -- Folder picker ------------------------------------------------------

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", self._output_folder)
        if folder:
            self._output_folder = folder
            self.folder_label.setText(folder)

    # -- Actions ------------------------------------------------------------

    def _require_setup(self) -> str | None:
        if self._selected_setup is None:
            self._set_status("Select a setup first.")
            return None
        return self._selected_setup

    def _rename_ops(self):
        name = self._require_setup()
        if name is None:
            return
        self._set_status(f"Renaming operations in {name}\u2026")
        self._set_buttons_enabled(False)
        self._run_in_bg(
            lambda c: c.rename_operations(name),
            lambda r: self._on_action_done("Rename", r, name),
            error_prefix="Rename failed",
        )

    def _nc_create(self):
        name = self._require_setup()
        if name is None:
            return
        folder = self._output_folder
        self._set_status(f"Creating NC programs for {name}\u2026")
        self._set_buttons_enabled(False)
        self._run_in_bg(
            lambda c: c.create_nc_programs(name, output_folder=folder),
            lambda r: self._on_action_done("NC Create", r, name),
            error_prefix="NC Create failed",
        )

    def _post_process(self):
        name = self._require_setup()
        if name is None:
            return
        folder = self._output_folder
        self._set_status(f"Post-processing {name}\u2026")
        self._set_buttons_enabled(False)

        def _do_post(client):
            client.create_nc_programs(name, output_folder=folder)
            return client.post_nc_programs(name, output_folder=folder)

        self._run_in_bg(
            _do_post,
            self._on_post_done,
            error_prefix="Post failed",
        )

    def _on_action_done(self, action: str, result: dict, setup_name: str):
        if result.get("skipped"):
            self._set_status(f"{action}: skipped \u2014 {result.get('reason', '?')}")
            self._set_buttons_enabled(True)
            return
        if "renamed" in result:
            n = len(result["renamed"])
            self._set_status(f"Renamed {n} operation{'s' if n != 1 else ''}")
        elif "programs" in result:
            progs = result["programs"]
            parts = [f"{p['name']} ({p['status']})" for p in progs]
            self._set_status("NC: " + ", ".join(parts))
        else:
            self._set_status(f"{action}: done")
        self._load_setup_detail(setup_name)

    def _on_post_done(self, result: dict):
        posted = result.get("posted", [])
        version = result.get("version", "?")
        files = [p["file"] for p in posted]
        self._set_status(f"Posted v{version}: {', '.join(files)}")
        self._set_buttons_enabled(True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication([])
    window = FusionCAMWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
