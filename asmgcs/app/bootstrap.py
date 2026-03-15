from __future__ import annotations

import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from asmgcs.views import MainWindow


def build_application(argv: list[str] | None = None) -> QApplication:
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Spatially Aware A-SMGCS HMI")
    app.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#121212"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#101418"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#171b1f"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#d8dee9"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#d8dee9"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#f4fbff"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#102028"))
    app.setPalette(palette)
    app.setStyleSheet(
        "QToolTip {"
        " background-color: #f4fbff;"
        " color: #102028;"
        " border: 1px solid #52d6ff;"
        " border-radius: 12px;"
        " padding: 10px 12px;"
        " font-family: Segoe UI;"
        " font-size: 10.5pt;"
        " selection-background-color: #d7f6ff;"
        " }"
    )
    return app


def run_application(argv: list[str] | None = None) -> int:
    app = build_application(argv)
    window = MainWindow()
    window.show()
    return app.exec()


__all__ = ["build_application", "run_application"]