"""Monect PC Remote receiver – desktop UI (system tray + settings window).

Talks to the receiver service over the local control socket.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap, QPen
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QRadioButton,
                               QSlider, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
                               QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from .control import SOCK
from . import i18n
from .i18n import detect, t


def LANG_IS_TR() -> bool:
    return i18n.LANG == 0



AUTOSTART = Path.home() / ".config/autostart/monect4linux.desktop"
MENU_ENTRY = Path.home() / ".local/share/applications/monect4linux.desktop"


def autostart_enabled() -> bool:
    r = subprocess.run(["systemctl", "--user", "is-enabled", "monect-receiver"],
                       capture_output=True, text=True)
    return r.stdout.strip() == "enabled" and AUTOSTART.exists()


def set_autostart(on: bool) -> None:
    """Enable/disable the receiver service and the tray app at login."""
    subprocess.run(["systemctl", "--user", "enable" if on else "disable", "monect-receiver"],
                   capture_output=True)
    if on:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        src = MENU_ENTRY.read_text() if MENU_ENTRY.exists() else (
            "[Desktop Entry]\nType=Application\nName=Monect4Linux\n"
            f"Exec={Path(sys.argv[0]).resolve()}\n")
        lines = [l + " --hidden" if l.startswith("Exec=") and "--hidden" not in l else l
                 for l in src.splitlines()]
        AUTOSTART.write_text("\n".join(lines) + "\n")
    else:
        AUTOSTART.unlink(missing_ok=True)


def make_icon(connected: bool) -> QIcon:
    """Original glyph: a phone in front of a monitor; green dot when connected."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#1f6feb"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 8, 50, 34, 5, 5)                 # monitor
    p.drawRect(24, 42, 10, 8)
    p.drawRoundedRect(16, 50, 26, 4, 2, 2)
    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(36, 20, 22, 38, 4, 4)               # phone
    p.setBrush(QColor("#0d1117"))
    p.drawRoundedRect(39, 24, 16, 28, 2, 2)
    if connected:
        p.setBrush(QColor("#2ea043"))
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawEllipse(4, 44, 16, 16)
    p.end()
    return QIcon(pm)


class Link:
    """Line-JSON client for the control socket with auto-reconnect."""

    def __init__(self, on_message) -> None:
        self.sock = QLocalSocket()
        self.buf = QByteArray()
        self.on_message = on_message
        self.sock.readyRead.connect(self._read)
        self.sock.connected.connect(self._hello)
        self.timer = QTimer()
        self.timer.timeout.connect(self._ensure)
        self.timer.start(2000)
        self._ensure()

    def _hello(self) -> None:
        self.send({"cmd": "status"})
        self.send({"cmd": "get_settings"})

    @property
    def up(self) -> bool:
        return self.sock.state() == QLocalSocket.ConnectedState

    def _ensure(self) -> None:
        if self.sock.state() == QLocalSocket.UnconnectedState:
            self.sock.connectToServer(str(SOCK))
        elif self.up:
            self.send({"cmd": "status"})

    def send(self, obj: dict) -> None:
        if self.up:
            self.sock.write((json.dumps(obj) + "\n").encode())
            self.sock.flush()

    def _read(self) -> None:
        self.buf += self.sock.readAll()
        while (i := self.buf.indexOf(b"\n")) >= 0:
            line = bytes(self.buf.left(i))
            self.buf.remove(0, i + 1)
            try:
                self.on_message(json.loads(line))
            except ValueError:
                pass


class Window(QMainWindow):
    def __init__(self, app: "Tray") -> None:
        super().__init__()
        self.app = app
        self.setWindowTitle(t("app"))
        self.setWindowIcon(make_icon(False))
        self.resize(640, 480)
        tabs = QTabWidget()
        tabs.addTab(self._status_tab(), t("tab_status"))
        tabs.addTab(self._security_tab(), t("tab_security"))
        tabs.addTab(self._stream_tab(), t("tab_rd"))
        tabs.addTab(self._help_tab(), t("tab_help"))
        self.setCentralWidget(tabs)

    # --- tabs ---------------------------------------------------------------------
    def _status_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.service_lbl = QLabel(t("connecting_svc"))
        self.service_lbl.setStyleSheet("font-weight: bold")
        v.addWidget(self.service_lbl)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        save = QPushButton(t("save"))
        save.clicked.connect(lambda: self.app.set({"name": self.name_edit.text().strip()}))
        row = QHBoxLayout()
        row.addWidget(self.name_edit)
        row.addWidget(save)
        form.addRow(t("pc_name"), row)
        self.ip_lbl = QLabel("-")
        self.ip_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        big = QFont()
        big.setPointSize(big.pointSize() + 4)
        big.setBold(True)
        self.ip_lbl.setFont(big)
        form.addRow(t("ip"), self.ip_lbl)
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("English", "en")
        self.lang_combo.addItem("Türkçe", "tr")
        self.lang_combo.activated.connect(self._lang_changed)
        form.addRow(t("language"), self.lang_combo)
        self.autostart = QCheckBox(t("autostart"))
        self.autostart.setChecked(autostart_enabled())
        self.autostart.toggled.connect(set_autostart)
        form.addRow(self.autostart)
        v.addLayout(form)
        v.addWidget(QLabel(t("devices")))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([t("col_device"), "IP", t("col_app"), t("col_state")])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table)
        btn = QPushButton(t("disconnect"))
        btn.clicked.connect(self._disconnect_selected)
        v.addWidget(btn)
        return w

    def _security_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        box = QGroupBox(t("who"))
        bv = QVBoxLayout(box)
        self.sec_group = QButtonGroup(self)
        self.sec_buttons = {}
        for key, text in (("open", t("sec_open")),
                          ("confirm", t("sec_confirm")),
                          ("password", t("sec_password"))):
            rb = QRadioButton(text)
            self.sec_group.addButton(rb)
            self.sec_buttons[key] = rb
            bv.addWidget(rb)
        v.addWidget(box)
        form = QFormLayout()
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.Password)
        self.pw_edit.setPlaceholderText(t("pw_hint"))
        form.addRow(t("password"), self.pw_edit)
        self.remember = QCheckBox(t("remember"))
        form.addRow(self.remember)
        v.addLayout(form)
        row = QHBoxLayout()
        apply_btn = QPushButton(t("apply"))
        apply_btn.clicked.connect(self._apply_security)
        forget = QPushButton(t("forget"))
        forget.clicked.connect(lambda: self.app.set({"clear_trusted": True}))
        row.addWidget(apply_btn)
        row.addWidget(forget)
        v.addLayout(row)
        v.addStretch()
        return w

    def _stream_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.br_slider = QSlider(Qt.Horizontal)
        self.br_slider.setRange(2, 40)
        self.br_lbl = QLabel()
        self.br_slider.valueChanged.connect(lambda v: self.br_lbl.setText(f"{v} Mbit/s"))
        row = QHBoxLayout()
        row.addWidget(self.br_slider)
        row.addWidget(self.br_lbl)
        form.addRow(t("quality"), row)
        self.fps_combo = QComboBox()
        for f in (30, 45, 60):
            self.fps_combo.addItem(f"{f} fps", f)
        form.addRow(t("max_fps"), self.fps_combo)
        note = QLabel(t("rd_note"))
        note.setWordWrap(True)
        form.addRow(note)
        btn = QPushButton(t("rd_apply"))
        btn.clicked.connect(lambda: self.app.set({"bitrate": self.br_slider.value() * 1_000_000,
                                                   "max_fps": self.fps_combo.currentData()}))
        form.addRow(btn)
        return w

    def _help_tab(self) -> QWidget:
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(t("help_html"))
        return tb

    # --- actions --------------------------------------------------------------------
    def _apply_security(self) -> None:
        mode = next(k for k, b in self.sec_buttons.items() if b.isChecked())
        upd = {"security": mode, "remember_devices": self.remember.isChecked()}
        if self.pw_edit.text():
            upd["password"] = self.pw_edit.text()
            self.pw_edit.clear()
        if mode == "password" and not (self.app.settings.get("has_password") or "password" in upd):
            QMessageBox.warning(self, t("app"), t("no_pw"))
            return
        self.app.set(upd)

    def _lang_changed(self) -> None:
        self.app.set({"language": self.lang_combo.currentData()})
        QMessageBox.information(self, t("app"), t("restart_lang"))

    def _disconnect_selected(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.app.link.send({"cmd": "disconnect", "ip": self.table.item(row, 1).text()})

    # --- updates --------------------------------------------------------------------
    def show_status(self, st: dict) -> None:
        self.service_lbl.setText(t("svc_ok"))
        self.service_lbl.setStyleSheet("font-weight: bold; color: #2ea043")
        if not self.name_edit.hasFocus():
            self.name_edit.setText(st.get("name", ""))
        self.ip_lbl.setText("   ".join(st.get("ips", [])) or "-")
        devs = st.get("devices", [])
        self.table.setRowCount(len(devs))
        for r, d in enumerate(devs):
            state = {"connected": t("st_connected"), "connecting": t("st_connecting"), "new": t("st_new")}.get(
                d.get("state"), d.get("state", ""))
            if d.get("remote_desktop"):
                state += t("st_rd")
            for c, val in enumerate((d.get("name", "?"), d.get("ip", ""),
                                     f"{d.get('app', '')} {d.get('platform', '')}", state)):
                self.table.setItem(r, c, QTableWidgetItem(val))

    def show_offline(self) -> None:
        self.service_lbl.setText(t("svc_down"))
        self.service_lbl.setStyleSheet("font-weight: bold; color: #cf222e")

    def show_settings(self, s: dict) -> None:
        i = self.lang_combo.findData(s.get("language", "en"))
        self.lang_combo.setCurrentIndex(max(i, 0))
        btn = self.sec_buttons.get(s.get("security", "open"))
        if btn:
            btn.setChecked(True)
        self.remember.setChecked(bool(s.get("remember_devices", True)))
        self.br_slider.setValue(int(s.get("bitrate", 8_000_000)) // 1_000_000)
        i = self.fps_combo.findData(int(s.get("max_fps", 60)))
        self.fps_combo.setCurrentIndex(max(i, 0))

    def closeEvent(self, ev) -> None:                  # close to tray
        ev.ignore()
        self.hide()


class Tray:
    def __init__(self, qapp: QApplication, show: bool) -> None:
        self.qapp = qapp
        self.settings: dict = {}
        self.devices: list = []
        self.win = Window(self)
        self.tray = QSystemTrayIcon(make_icon(False))
        self.tray.setToolTip(t("app"))
        menu = QMenu()
        self.status_action = QAction(t("waiting"))
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()
        act = QAction(t("open_window"))
        act.triggered.connect(self.show_window)
        menu.addAction(act)
        restart = QAction(t("restart"))
        restart.triggered.connect(lambda: subprocess.Popen(
            ["systemctl", "--user", "restart", "monect-receiver"]))
        menu.addAction(restart)
        quit_act = QAction(t("quit_ui"))
        quit_act.triggered.connect(qapp.quit)
        menu.addAction(quit_act)
        self._menu, self._actions = menu, (act, restart, quit_act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_window() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self.link = Link(self.on_message)
        self.link.sock.disconnected.connect(self.win.show_offline)
        if show:
            self.show_window()

    def show_window(self) -> None:
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()

    def set(self, upd: dict) -> None:
        self.link.send({"cmd": "set_settings", "settings": upd})

    def on_message(self, m: dict) -> None:
        kind = m.get("type")
        if kind == "status":
            self.devices = m.get("devices", [])
            self.win.show_status(m)
            connected = [d for d in self.devices if d.get("state") == "connected"]
            self.tray.setIcon(make_icon(bool(connected)))
            text = (", ".join(d["name"] for d in connected) + " " + t("is_connected")) if connected \
                else t("waiting")
            self.status_action.setText(text)
            self.tray.setToolTip(t("app") + " – " + text)
        elif kind == "settings":
            self.settings = m.get("settings", {})
            self.win.show_settings(self.settings)
        elif kind == "event":
            ev = m.get("event")
            if ev == "connected":
                self.tray.showMessage(t("app"), f"{m.get('device')} {t('joined')}", make_icon(True), 3000)
                self.link.send({"cmd": "status"})
            elif ev == "disconnected":
                self.tray.showMessage(t("app"), f"{m.get('device', '?')} {t('left')}",
                                      make_icon(False), 3000)
                self.link.send({"cmd": "status"})
            elif ev == "confirm":
                self.ask(m)

    def ask(self, m: dict) -> None:
        box = QMessageBox(QMessageBox.Question, t("req_title"),
                          f"<b>{m.get('device')}</b> ({m.get('ip')}) {t('req_text')}")
        allow = box.addButton(t("allow"), QMessageBox.AcceptRole)
        box.addButton(t("deny"), QMessageBox.RejectRole)
        box.setWindowFlag(Qt.WindowStaysOnTopHint)
        box.exec()
        self.link.send({"cmd": "confirm", "id": m.get("id"), "accept": box.clickedButton() is allow})


def main() -> None:
    try:
        import pathlib
        cfg = json.loads((pathlib.Path.home() / ".config/monect-linux/settings.json").read_text())
    except Exception:
        cfg = {}
    detect(cfg.get("language"))
    qapp = QApplication(sys.argv)
    qapp.setApplicationName("monect-receiver")
    qapp.setApplicationDisplayName(t("app"))
    qapp.setDesktopFileName("monect4linux")
    qapp.setQuitOnLastWindowClosed(False)
    Tray(qapp, show="--hidden" not in sys.argv)
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
