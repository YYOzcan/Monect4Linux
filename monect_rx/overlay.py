"""On-screen helper window, driven by the receiver over stdin (one JSON object per line).

* Blackboard: the phone draws on the PC screen. A click-through, always-on-top,
  full-screen layer; transparent background unless the phone picks a colour.
* Picture projector: shows a photo sent from the phone full screen.

Commands: {"cmd": "bb", "on": bool} | {"cmd": "bg", "color": argb}
          {"cmd": "begin", "key": str, "id": int, "w": float, "color": argb, "mode": int,
           "x": float, "y": float}
          {"cmd": "points", "key": str, "id": int, "pts": [x, y, x, y, ...]}
          {"cmd": "end", "key": str, "id": int} | {"cmd": "clear"}
          {"cmd": "image", "path": str} | {"cmd": "image_close"} | {"cmd": "quit"}
Coordinates are 0..1 of the screen; widths are a fraction of the screen width.
"""
from __future__ import annotations

import json
import sys
import threading

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QWidget


def argb(v: int) -> QColor:
    v &= 0xFFFFFFFF
    return QColor((v >> 16) & 255, (v >> 8) & 255, v & 255, (v >> 24) & 255)


class Board(QWidget):
    def __init__(self) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.X11BypassWindowManagerHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("Monect Blackboard")
        self.bg = 0
        self.layer: QImage | None = None
        self.live: dict[tuple[str, int], dict] = {}

    def fit(self) -> None:
        geo = QGuiApplication.primaryScreen().geometry()
        self.setGeometry(geo)
        if self.layer is None or self.layer.size() != geo.size():
            old = self.layer
            self.layer = QImage(geo.size(), QImage.Format_ARGB32_Premultiplied)
            self.layer.fill(Qt.transparent)
            if old is not None:
                QPainter(self.layer).drawImage(0, 0, old)

    def clear(self) -> None:
        if self.layer is not None:
            self.layer.fill(Qt.transparent)
        self.live.clear()
        self.update()

    def segment(self, st: dict, x0: float, y0: float, x1: float, y1: float) -> None:
        if self.layer is None:
            return
        w, h = self.layer.width(), self.layer.height()
        p = QPainter(self.layer)
        p.setRenderHint(QPainter.Antialiasing)
        mode = st["mode"]
        if mode & 1:                              # eraser
            if mode & 2 or not self.bg:
                p.setCompositionMode(QPainter.CompositionMode_Clear)
                color = QColor(0, 0, 0, 0)
            else:
                color = argb(self.bg)
        else:
            color = argb(st["color"])
        pen = QPen(color, max(1.0, st["w"] * w), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        a, b = QPointF(x0 * w, y0 * h), QPointF(x1 * w, y1 * h)
        if a == b:
            p.drawPoint(a)
        else:
            p.drawLine(a, b)
        p.end()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        if self.bg:
            p.fillRect(self.rect(), argb(self.bg))
        if self.layer is not None:
            p.drawImage(0, 0, self.layer)


class Viewer(QLabel):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Monect Projector")
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:black")
        self.pix = QPixmap()

    def show_image(self, path: str) -> None:
        self.pix = QPixmap(path)
        self.showFullScreen()
        self._rescale()
        self.raise_()
        self.activateWindow()

    def _rescale(self) -> None:
        if not self.pix.isNull():
            self.setPixmap(self.pix.scaled(self.size(), Qt.KeepAspectRatio,
                                           Qt.SmoothTransformation))

    def resizeEvent(self, ev) -> None:
        self._rescale()
        super().resizeEvent(ev)

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key_Escape, Qt.Key_Q):
            self.hide()


class Bridge(QObject):
    cmd = Signal(dict)


def main() -> None:
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    board, viewer, bridge = Board(), Viewer(), Bridge()

    def handle(m: dict) -> None:
        c = m.get("cmd")
        if c == "bb":
            if m.get("on"):
                board.fit()
                board.show()
                board.raise_()
            else:
                board.hide()
        elif c == "bg":
            board.bg = int(m.get("color", 0))
            board.update()
        elif c == "begin":
            st = {"w": float(m["w"]), "color": int(m["color"]), "mode": int(m["mode"]),
                  "last": (float(m["x"]), float(m["y"]))}
            board.live[(m["key"], int(m["id"]))] = st
            board.segment(st, *st["last"], *st["last"])
            board.update()
        elif c == "points":
            st = board.live.get((m["key"], int(m["id"])))
            if st:
                pts = m["pts"]
                for i in range(0, len(pts) - 1, 2):
                    x, y = float(pts[i]), float(pts[i + 1])
                    board.segment(st, *st["last"], x, y)
                    st["last"] = (x, y)
                board.update()
        elif c == "end":
            board.live.pop((m["key"], int(m["id"])), None)
        elif c == "clear":
            board.clear()
        elif c == "image":
            viewer.show_image(m["path"])
        elif c == "image_close":
            viewer.hide()
        elif c == "quit":
            app.quit()

    bridge.cmd.connect(handle)

    def reader() -> None:
        for line in sys.stdin:
            try:
                bridge.cmd.emit(json.loads(line))
            except (ValueError, KeyError):
                continue
        bridge.cmd.emit({"cmd": "quit"})

    threading.Thread(target=reader, daemon=True).start()
    app.exec()


if __name__ == "__main__":
    main()
