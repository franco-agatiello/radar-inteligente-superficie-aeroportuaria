from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGraphicsScene,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from asmgcs.app import build_radar_viewmodel
from asmgcs.app.support_traffic import SupportTrafficService
from asmgcs.infrastructure import OpenSkyTelemetryWorker
from asmgcs.physics.safety_criteria import (
    DEFAULT_DB_FILENAME,
    KNOWN_OTHER_KINDS,
    KNOWN_PRIMARY_KINDS,
    KNOWN_SECTORS,
    SafetyCriteriaRepository,
    SafetyCriteriaRule,
)
from asmgcs.physics.zone_tuning import KNOWN_ZONE_SECTORS, ZoneTuningRepository, ZoneTuningRule
from asmgcs.viewmodels import RadarViewModel, RadarViewState
from asmgcs.views.graphics_items import SurfaceRadarView
from asmgcs.views.radar_scene import RadarSceneController
from asmgcs.views.viewport import ViewportStateMachine
from gis_manager import GISContext, GISManager
from models import AIRPORTS, ANIMATION_FPS, AirportConfig, GeoReference, RenderState, TelemetrySnapshot, WebMercatorMapper


RENDER_FPS = ANIMATION_FPS
SUPPORT_SYNC_HZ = 10.0
DETAIL_PANEL_REFRESH_HZ = 4.0

SECTOR_ROW_COLOR_BY_NAME = {
    "runway": QColor(255, 110, 110, 64),
    "taxiway": QColor(120, 210, 255, 56),
    "apron": QColor(255, 215, 96, 56),
    "global": QColor(140, 156, 168, 42),
}
SECTOR_TEXT_COLOR = QColor("#eafcff")


class StartMenuPage(QWidget):
    airport_selected = Signal(str)
    criteria_requested = Signal()
    zone_polish_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._build_layout()

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 48, 48, 48)
        layout.setSpacing(18)
        hero = QFrame(self)
        hero.setStyleSheet("QFrame { background-color: #171b1f; border: 1px solid #2d3842; border-radius: 18px; }")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(28, 28, 28, 28)
        hero_layout.setSpacing(12)
        title = QLabel("Spatially Aware A-SMGCS HMI")
        title.setStyleSheet("font-size: 24pt; font-weight: 700; color: #7ce3ff;")
        subtitle = QLabel(
            "Select an airport to launch true-scale aircraft rendering, Overpass-derived aeroway polygons, graph-aware predictive routing, and collision monitoring."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("font-size: 11.5pt; color: #d8dee9;")
        hero_layout.addWidget(title)
        hero_layout.addWidget(subtitle)
        criteria_button = QPushButton("Open Safety Criteria")
        criteria_button.setMinimumHeight(54)
        criteria_button.setCursor(Qt.CursorShape.PointingHandCursor)
        criteria_button.setStyleSheet(
            "QPushButton { background-color: #0f1418; color: #e6fbff; border: 1px solid #355160; border-radius: 12px; font-size: 11.5pt; font-weight: 600; padding: 12px 16px; }"
            "QPushButton:hover { background-color: #152029; border-color: #52d6ff; }"
        )
        criteria_button.clicked.connect(self.criteria_requested.emit)
        zone_polish_button = QPushButton("Open Zone Polish")
        zone_polish_button.setMinimumHeight(54)
        zone_polish_button.setCursor(Qt.CursorShape.PointingHandCursor)
        zone_polish_button.setStyleSheet(
            "QPushButton { background-color: #0f1418; color: #e6fbff; border: 1px solid #355160; border-radius: 12px; font-size: 11.5pt; font-weight: 600; padding: 12px 16px; }"
            "QPushButton:hover { background-color: #152029; border-color: #52d6ff; }"
        )
        zone_polish_button.clicked.connect(self.zone_polish_requested.emit)
        layout.addWidget(hero)
        hero_layout.addWidget(criteria_button)
        hero_layout.addWidget(zone_polish_button)
        for airport in AIRPORTS.values():
            button = QPushButton(f"{airport.code}  |  {airport.display_name}")
            button.setMinimumHeight(72)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { background-color: #101418; color: #e6fbff; border: 1px solid #2f434f; border-radius: 14px; font-size: 13pt; font-weight: 600; padding: 16px; text-align: left; }"
                "QPushButton:hover { background-color: #162028; border-color: #52d6ff; }"
            )
            button.clicked.connect(lambda checked=False, code=airport.code: self.airport_selected.emit(code))
            layout.addWidget(button)
        layout.addStretch(1)


class SafetyCriteriaWindow(QWidget):
    def __init__(self, criteria_repository: SafetyCriteriaRepository) -> None:
        super().__init__()
        self.criteria_repository = criteria_repository
        self._visible_criteria_rules: tuple[SafetyCriteriaRule, ...] = ()
        self._selected_criteria_rule_id: int | None = None
        self._build_layout()
        self._set_editor_enabled(True)
        self._refresh_airport_rules()

    def _build_layout(self) -> None:
        self.setWindowTitle("Safety Criteria Database")
        self.resize(980, 760)
        self.setStyleSheet(
            "QWidget { background-color: #12161a; color: #d8dee9; }"
            "QLabel { color: #d8dee9; }"
            "QComboBox, QDoubleSpinBox, QTableWidget { background-color: #101418; color: #e6fbff; border: 1px solid #2e3640; border-radius: 8px; padding: 4px 6px; }"
            "QPushButton { background-color: #0f1418; color: #e6fbff; border: 1px solid #355160; border-radius: 10px; font-size: 10.5pt; padding: 8px 12px; }"
            "QPushButton:hover { background-color: #152029; border-color: #52d6ff; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        title = QLabel("Safety Criteria Database")
        title.setStyleSheet("font-size: 18pt; font-weight: 700; color: #7ce3ff;")
        info = QLabel("Edit distance rules outside the map view. Changes are stored in SQLite and applied live.")
        info.setWordWrap(True)
        info.setStyleSheet("font-size: 10.5pt; color: #9fb8c6;")
        layout.addWidget(title)
        layout.addWidget(info)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.criteria_airport_filter = QComboBox()
        for airport in AIRPORTS.values():
            self.criteria_airport_filter.addItem(f"{airport.code} | {airport.display_name}", airport.code)
        self.criteria_airport_filter.currentIndexChanged.connect(self._refresh_airport_rules)
        self.criteria_sector_filter = QComboBox()
        self.criteria_sector_filter.addItem("All sectors", "all")
        for sector_name in KNOWN_SECTORS:
            self.criteria_sector_filter.addItem(sector_name.title(), sector_name)
        self.criteria_sector_filter.currentIndexChanged.connect(self._refresh_criteria_table)
        self.criteria_primary_filter = QComboBox()
        self.criteria_primary_filter.addItem("All origins", "all")
        for primary_kind in KNOWN_PRIMARY_KINDS:
            self.criteria_primary_filter.addItem(primary_kind.title(), primary_kind)
        self.criteria_primary_filter.currentIndexChanged.connect(self._refresh_criteria_table)
        self.criteria_other_filter = QComboBox()
        self.criteria_other_filter.addItem("All targets", "all")
        for other_kind in KNOWN_OTHER_KINDS:
            self.criteria_other_filter.addItem(other_kind.title(), other_kind)
        self.criteria_other_filter.currentIndexChanged.connect(self._refresh_criteria_table)
        for widget in (self.criteria_airport_filter, self.criteria_sector_filter, self.criteria_primary_filter, self.criteria_other_filter):
            top_row.addWidget(widget)
        layout.addLayout(top_row)

        self.criteria_info_label = QLabel("")
        self.criteria_info_label.setWordWrap(True)
        self.criteria_info_label.setStyleSheet("font-size: 10pt; color: #9fb8c6;")
        layout.addWidget(self.criteria_info_label)

        self.criteria_table = QTableWidget(0, 8)
        self.criteria_table.setHorizontalHeaderLabels(["Sector", "Origin", "Target", "V Min", "V Max", "Green", "Yellow", "Red"])
        self.criteria_table.verticalHeader().setVisible(False)
        self.criteria_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.criteria_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.criteria_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.criteria_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.criteria_table.itemSelectionChanged.connect(self._load_selected_criteria_rule)
        layout.addWidget(self.criteria_table, stretch=4)

        form_grid = QGridLayout()
        form_grid.setHorizontalSpacing(8)
        form_grid.setVerticalSpacing(6)
        self.criteria_sector_edit = QComboBox()
        for sector_name in KNOWN_SECTORS:
            self.criteria_sector_edit.addItem(sector_name.title(), sector_name)
        self.criteria_primary_edit = QComboBox()
        for primary_kind in KNOWN_PRIMARY_KINDS:
            self.criteria_primary_edit.addItem(primary_kind.title(), primary_kind)
        self.criteria_other_edit = QComboBox()
        for other_kind in KNOWN_OTHER_KINDS:
            self.criteria_other_edit.addItem(other_kind.title(), other_kind)
        self.criteria_speed_min_spin = self._build_criteria_spinbox(0.0, 80.0, 1, " m/s")
        self.criteria_speed_max_spin = self._build_criteria_spinbox(0.1, 120.0, 1, " m/s")
        self.criteria_green_spin = self._build_criteria_spinbox(0.0, 500.0, 1, " m")
        self.criteria_yellow_spin = self._build_criteria_spinbox(0.0, 500.0, 1, " m")
        self.criteria_red_spin = self._build_criteria_spinbox(0.0, 500.0, 1, " m")
        form_grid.addWidget(QLabel("Sector"), 0, 0)
        form_grid.addWidget(self.criteria_sector_edit, 0, 1)
        form_grid.addWidget(QLabel("Origin"), 0, 2)
        form_grid.addWidget(self.criteria_primary_edit, 0, 3)
        form_grid.addWidget(QLabel("Target"), 1, 0)
        form_grid.addWidget(self.criteria_other_edit, 1, 1)
        form_grid.addWidget(QLabel("V Min"), 1, 2)
        form_grid.addWidget(self.criteria_speed_min_spin, 1, 3)
        form_grid.addWidget(QLabel("V Max"), 2, 0)
        form_grid.addWidget(self.criteria_speed_max_spin, 2, 1)
        form_grid.addWidget(QLabel("Green"), 2, 2)
        form_grid.addWidget(self.criteria_green_spin, 2, 3)
        form_grid.addWidget(QLabel("Yellow"), 3, 0)
        form_grid.addWidget(self.criteria_yellow_spin, 3, 1)
        form_grid.addWidget(QLabel("Red"), 3, 2)
        form_grid.addWidget(self.criteria_red_spin, 3, 3)
        layout.addLayout(form_grid)

        button_row = QHBoxLayout()
        self.criteria_new_button = QPushButton("New Rule")
        self.criteria_new_button.clicked.connect(self._prepare_new_criteria_rule)
        self.criteria_save_button = QPushButton("Save Rule")
        self.criteria_save_button.clicked.connect(self._save_criteria_rule)
        self.criteria_delete_button = QPushButton("Delete Rule")
        self.criteria_delete_button.clicked.connect(self._delete_criteria_rule)
        self.criteria_reset_button = QPushButton("Reset Airport")
        self.criteria_reset_button.clicked.connect(self._reset_criteria_rules)
        for button in (self.criteria_new_button, self.criteria_save_button, self.criteria_delete_button, self.criteria_reset_button):
            button_row.addWidget(button)
        layout.addLayout(button_row)

    def focus_airport(self, airport_code: str | None) -> None:
        if not airport_code:
            self.show()
            self.raise_()
            self.activateWindow()
            return
        index = self.criteria_airport_filter.findData(airport_code)
        if index >= 0:
            self.criteria_airport_filter.setCurrentIndex(index)
        self.show()
        self.raise_()
        self.activateWindow()

    def _build_criteria_spinbox(self, minimum: float, maximum: float, decimals: int, suffix: str) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox()
        spinbox.setRange(minimum, maximum)
        spinbox.setDecimals(decimals)
        spinbox.setSingleStep(0.5 if decimals > 0 else 1.0)
        spinbox.setSuffix(suffix)
        return spinbox

    def _set_editor_enabled(self, enabled: bool) -> None:
        for widget in (
            self.criteria_airport_filter,
            self.criteria_sector_filter,
            self.criteria_primary_filter,
            self.criteria_other_filter,
            self.criteria_table,
            self.criteria_sector_edit,
            self.criteria_primary_edit,
            self.criteria_other_edit,
            self.criteria_speed_min_spin,
            self.criteria_speed_max_spin,
            self.criteria_green_spin,
            self.criteria_yellow_spin,
            self.criteria_red_spin,
            self.criteria_new_button,
            self.criteria_save_button,
            self.criteria_delete_button,
            self.criteria_reset_button,
        ):
            widget.setEnabled(enabled)

    def _selected_airport_code(self) -> str:
        return str(self.criteria_airport_filter.currentData())

    def _refresh_airport_rules(self) -> None:
        airport_code = self._selected_airport_code()
        self.criteria_repository.ensure_airport_defaults(airport_code)
        self._selected_criteria_rule_id = None
        self._refresh_criteria_table()

    def _refresh_criteria_table(self) -> None:
        airport_code = self._selected_airport_code()
        sector_name = self.criteria_sector_filter.currentData()
        primary_kind = self.criteria_primary_filter.currentData()
        other_kind = self.criteria_other_filter.currentData()
        self._visible_criteria_rules = self.criteria_repository.list_rules(
            airport_code,
            sector_name=None if sector_name == "all" else str(sector_name),
            primary_kind=None if primary_kind == "all" else str(primary_kind),
            other_kind=None if other_kind == "all" else str(other_kind),
        )
        self.criteria_table.blockSignals(True)
        self.criteria_table.setRowCount(len(self._visible_criteria_rules))
        selected_row = 0
        for row_index, rule in enumerate(self._visible_criteria_rules):
            values = (
                rule.sector_name,
                rule.primary_kind,
                rule.other_kind,
                f"{rule.speed_min_mps:.1f}",
                f"{rule.speed_max_mps:.1f}",
                f"{rule.green_m:.1f}",
                f"{rule.yellow_m:.1f}",
                f"{rule.red_m:.1f}",
            )
            background_color = SECTOR_ROW_COLOR_BY_NAME.get(rule.sector_name)
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                if background_color is not None:
                    item.setBackground(background_color)
                    item.setForeground(SECTOR_TEXT_COLOR)
                self.criteria_table.setItem(row_index, column_index, item)
            if rule.rule_id == self._selected_criteria_rule_id:
                selected_row = row_index
        self.criteria_table.blockSignals(False)
        if self._visible_criteria_rules:
            self.criteria_table.selectRow(selected_row)
            self._load_rule_into_form(self._visible_criteria_rules[selected_row])
        else:
            self._prepare_new_criteria_rule()
        self.criteria_info_label.setText(
            f"SQLite: {self.criteria_repository.db_path.name} | Airport {airport_code} | Changes apply automatically."
        )

    def _load_selected_criteria_rule(self) -> None:
        selected_rows = self.criteria_table.selectionModel().selectedRows()
        if not selected_rows:
            return
        row_index = selected_rows[0].row()
        if 0 <= row_index < len(self._visible_criteria_rules):
            self._load_rule_into_form(self._visible_criteria_rules[row_index])

    def _load_rule_into_form(self, rule: SafetyCriteriaRule) -> None:
        self._selected_criteria_rule_id = rule.rule_id
        self.criteria_sector_edit.setCurrentIndex(max(0, self.criteria_sector_edit.findData(rule.sector_name)))
        self.criteria_primary_edit.setCurrentIndex(max(0, self.criteria_primary_edit.findData(rule.primary_kind)))
        self.criteria_other_edit.setCurrentIndex(max(0, self.criteria_other_edit.findData(rule.other_kind)))
        self.criteria_speed_min_spin.setValue(rule.speed_min_mps)
        self.criteria_speed_max_spin.setValue(rule.speed_max_mps)
        self.criteria_green_spin.setValue(rule.green_m)
        self.criteria_yellow_spin.setValue(rule.yellow_m)
        self.criteria_red_spin.setValue(rule.red_m)

    def _prepare_new_criteria_rule(self) -> None:
        self._selected_criteria_rule_id = None
        sector_name = self.criteria_sector_filter.currentData()
        primary_kind = self.criteria_primary_filter.currentData()
        other_kind = self.criteria_other_filter.currentData()
        self.criteria_sector_edit.setCurrentIndex(max(0, self.criteria_sector_edit.findData("global" if sector_name == "all" else sector_name)))
        self.criteria_primary_edit.setCurrentIndex(max(0, self.criteria_primary_edit.findData("aircraft" if primary_kind == "all" else primary_kind)))
        self.criteria_other_edit.setCurrentIndex(max(0, self.criteria_other_edit.findData("vehicle" if other_kind == "all" else other_kind)))
        self.criteria_speed_min_spin.setValue(0.0)
        self.criteria_speed_max_spin.setValue(8.0)
        self.criteria_green_spin.setValue(30.0)
        self.criteria_yellow_spin.setValue(18.0)
        self.criteria_red_spin.setValue(9.0)
        self.criteria_table.clearSelection()

    def _save_criteria_rule(self) -> None:
        airport_code = self._selected_airport_code()
        green_m = self.criteria_green_spin.value()
        yellow_m = min(self.criteria_yellow_spin.value(), green_m)
        red_m = min(self.criteria_red_spin.value(), yellow_m)
        stored_rule = self.criteria_repository.upsert_rule(
            SafetyCriteriaRule(
                rule_id=self._selected_criteria_rule_id,
                airport_code=airport_code,
                sector_name=str(self.criteria_sector_edit.currentData()),
                primary_kind=str(self.criteria_primary_edit.currentData()),
                other_kind=str(self.criteria_other_edit.currentData()),
                speed_min_mps=self.criteria_speed_min_spin.value(),
                speed_max_mps=max(self.criteria_speed_max_spin.value(), self.criteria_speed_min_spin.value() + 0.1),
                green_m=green_m,
                yellow_m=yellow_m,
                red_m=red_m,
            )
        )
        self._selected_criteria_rule_id = stored_rule.rule_id
        self._refresh_criteria_table()

    def _delete_criteria_rule(self) -> None:
        airport_code = self._selected_airport_code()
        if self._selected_criteria_rule_id is None:
            return
        self.criteria_repository.delete_rule(airport_code, self._selected_criteria_rule_id)
        self._selected_criteria_rule_id = None
        self._refresh_criteria_table()

    def _reset_criteria_rules(self) -> None:
        airport_code = self._selected_airport_code()
        response = QMessageBox.question(
            self,
            "Reset Criteria",
            f"Reset all safety criteria rules for {airport_code} to defaults?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        self.criteria_repository.reset_airport_defaults(airport_code)
        self._selected_criteria_rule_id = None
        self._refresh_criteria_table()


class ZonePolishWindow(QWidget):
    zone_polish_updated = Signal(str)

    def __init__(self, zone_tuning_repository: ZoneTuningRepository) -> None:
        super().__init__()
        self.zone_tuning_repository = zone_tuning_repository
        self._visible_rules: tuple[ZoneTuningRule, ...] = ()
        self._selected_sector_name: str | None = None
        self._build_layout()
        self._refresh_airport_rules()

    def _build_layout(self) -> None:
        self.setWindowTitle("Zone Polish")
        self.resize(760, 620)
        self.setStyleSheet(
            "QWidget { background-color: #12161a; color: #d8dee9; }"
            "QLabel { color: #d8dee9; }"
            "QComboBox, QDoubleSpinBox, QTableWidget { background-color: #101418; color: #e6fbff; border: 1px solid #2e3640; border-radius: 8px; padding: 4px 6px; }"
            "QPushButton { background-color: #0f1418; color: #e6fbff; border: 1px solid #355160; border-radius: 10px; font-size: 10.5pt; padding: 8px 12px; }"
            "QPushButton:hover { background-color: #152029; border-color: #52d6ff; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        title = QLabel("Zone Polish")
        title.setStyleSheet("font-size: 18pt; font-weight: 700; color: #7ce3ff;")
        info = QLabel(
            "Adjust sector buffers and overlay opacity to polish airport zones. Buffer changes affect the spatial logic; opacity only affects visualization."
        )
        info.setWordWrap(True)
        info.setStyleSheet("font-size: 10.5pt; color: #9fb8c6;")
        layout.addWidget(title)
        layout.addWidget(info)

        top_row = QHBoxLayout()
        self.airport_filter = QComboBox()
        for airport in AIRPORTS.values():
            self.airport_filter.addItem(f"{airport.code} | {airport.display_name}", airport.code)
        self.airport_filter.currentIndexChanged.connect(self._refresh_airport_rules)
        top_row.addWidget(self.airport_filter)
        layout.addLayout(top_row)

        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("font-size: 10pt; color: #9fb8c6;")
        layout.addWidget(self.info_label)

        self.rules_table = QTableWidget(0, 3)
        self.rules_table.setHorizontalHeaderLabels(["Sector", "Buffer", "Overlay"])
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.rules_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.rules_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.rules_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.rules_table.itemSelectionChanged.connect(self._load_selected_rule)
        layout.addWidget(self.rules_table, stretch=3)

        form_grid = QGridLayout()
        form_grid.setHorizontalSpacing(8)
        form_grid.setVerticalSpacing(6)
        self.sector_edit = QComboBox()
        for sector_name in KNOWN_ZONE_SECTORS:
            self.sector_edit.addItem(sector_name.title(), sector_name)
        self.buffer_spin = QDoubleSpinBox()
        self.buffer_spin.setRange(-120.0, 120.0)
        self.buffer_spin.setDecimals(1)
        self.buffer_spin.setSingleStep(1.0)
        self.buffer_spin.setSuffix(" m")
        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(0.0, 100.0)
        self.opacity_spin.setDecimals(0)
        self.opacity_spin.setSingleStep(1.0)
        self.opacity_spin.setSuffix(" %")
        form_grid.addWidget(QLabel("Sector"), 0, 0)
        form_grid.addWidget(self.sector_edit, 0, 1)
        form_grid.addWidget(QLabel("Buffer"), 1, 0)
        form_grid.addWidget(self.buffer_spin, 1, 1)
        form_grid.addWidget(QLabel("Overlay opacity"), 2, 0)
        form_grid.addWidget(self.opacity_spin, 2, 1)
        layout.addLayout(form_grid)

        button_row = QHBoxLayout()
        self.save_button = QPushButton("Save Sector")
        self.save_button.clicked.connect(self._save_rule)
        self.reset_button = QPushButton("Reset Airport")
        self.reset_button.clicked.connect(self._reset_airport)
        button_row.addWidget(self.save_button)
        button_row.addWidget(self.reset_button)
        layout.addLayout(button_row)

    def focus_airport(self, airport_code: str | None) -> None:
        if airport_code:
            index = self.airport_filter.findData(airport_code)
            if index >= 0:
                self.airport_filter.setCurrentIndex(index)
        self.show()
        self.raise_()
        self.activateWindow()

    def _selected_airport_code(self) -> str:
        return str(self.airport_filter.currentData())

    def _refresh_airport_rules(self) -> None:
        airport_code = self._selected_airport_code()
        self.zone_tuning_repository.ensure_airport_defaults(airport_code)
        self._visible_rules = self.zone_tuning_repository.list_rules(airport_code)
        self.rules_table.blockSignals(True)
        self.rules_table.setRowCount(len(self._visible_rules))
        selected_row = 0
        for row_index, rule in enumerate(self._visible_rules):
            values = (rule.sector_name, f"{rule.buffer_m:.1f} m", f"{rule.overlay_opacity * 100.0:.0f} %")
            background_color = SECTOR_ROW_COLOR_BY_NAME.get(rule.sector_name)
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                if background_color is not None:
                    item.setBackground(background_color)
                    item.setForeground(SECTOR_TEXT_COLOR)
                self.rules_table.setItem(row_index, column_index, item)
            if rule.sector_name == self._selected_sector_name:
                selected_row = row_index
        self.rules_table.blockSignals(False)
        if self._visible_rules:
            self.rules_table.selectRow(selected_row)
            self._load_rule_into_form(self._visible_rules[selected_row])
        self.info_label.setText(
            f"JSON: {self.zone_tuning_repository.json_path.name} | Airport {airport_code} | Buffer changes affect drivable and sector areas on reload."
        )

    def _load_selected_rule(self) -> None:
        selected_rows = self.rules_table.selectionModel().selectedRows()
        if not selected_rows:
            return
        row_index = selected_rows[0].row()
        if 0 <= row_index < len(self._visible_rules):
            self._load_rule_into_form(self._visible_rules[row_index])

    def _load_rule_into_form(self, rule: ZoneTuningRule) -> None:
        self._selected_sector_name = rule.sector_name
        self.sector_edit.setCurrentIndex(max(0, self.sector_edit.findData(rule.sector_name)))
        self.buffer_spin.setValue(rule.buffer_m)
        self.opacity_spin.setValue(rule.overlay_opacity * 100.0)

    def _save_rule(self) -> None:
        airport_code = self._selected_airport_code()
        stored_rule = self.zone_tuning_repository.upsert_rule(
            ZoneTuningRule(
                airport_code=airport_code,
                sector_name=str(self.sector_edit.currentData()),
                buffer_m=self.buffer_spin.value(),
                overlay_opacity=self.opacity_spin.value() / 100.0,
            )
        )
        self._selected_sector_name = stored_rule.sector_name
        self._refresh_airport_rules()
        self.zone_polish_updated.emit(airport_code)

    def _reset_airport(self) -> None:
        airport_code = self._selected_airport_code()
        response = QMessageBox.question(
            self,
            "Reset Zone Polish",
            f"Reset all zone polish values for {airport_code} to defaults?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        self.zone_tuning_repository.reset_airport_defaults(airport_code)
        self._selected_sector_name = None
        self._refresh_airport_rules()
        self.zone_polish_updated.emit(airport_code)


class RadarPage(QWidget):
    back_requested = Signal()
    criteria_requested = Signal(str)
    zone_polish_requested = Signal(str)

    def __init__(self, criteria_repository: SafetyCriteriaRepository, zone_tuning_repository: ZoneTuningRepository) -> None:
        super().__init__()
        self.airport: AirportConfig | None = None
        self.geo: GeoReference | None = None
        self.mapper: WebMercatorMapper | None = None
        self.gis_manager: GISManager | None = None
        self.gis_context: GISContext | None = None
        self.worker: OpenSkyTelemetryWorker | None = None
        self.radar_viewmodel: RadarViewModel | None = None
        self.support_traffic = SupportTrafficService(seed=21)
        self.latest_status = "Idle"
        self.latest_update_utc = "--"
        self.selected_actor_id: str | None = None
        self.follow_mode_enabled = False
        self._auto_framed_live_traffic = False
        self.alert_phase = 0
        self._latest_view_state: RadarViewState | None = None
        self._last_support_sync_monotonic = 0.0
        self._last_detail_panel_refresh_monotonic = 0.0
        self._vehicle_color_hex_by_id: dict[str, str] = {}
        self.criteria_repository = criteria_repository
        self.zone_tuning_repository = zone_tuning_repository
        self.viewport_state_machine = ViewportStateMachine()

        self.scene = QGraphicsScene(self)
        self.radar_view = SurfaceRadarView(self.scene)
        self.scene_controller = RadarSceneController(self.scene, self.radar_view)
        self.scene_controller.aircraft_selected.connect(self._select_aircraft)
        self.scene_controller.tooltip_requested.connect(self._show_scene_tooltip)
        self.scene_controller.tooltip_hidden.connect(self._hide_scene_tooltip)

        self.timer = QTimer(self)
        self.timer.setInterval(int(1000 / RENDER_FPS))
        self.timer.timeout.connect(self._on_animation_tick)
        self._build_layout()
        self._build_alert_overlay()
        self._build_hover_tooltip()

    def _build_layout(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(14)
        self.radar_view.setMinimumSize(1080, 800)
        layout.addWidget(self.radar_view, stretch=5)

        panel = QFrame(self)
        panel.setStyleSheet(
            "QFrame { background-color: #171b1f; border: 1px solid #2e3640; border-radius: 10px; }"
            "QLabel { color: #d8dee9; }"
            "QTextEdit { background-color: #101418; color: #d8dee9; border: 1px solid #2e3640; border-radius: 8px; padding: 6px; font-family: Consolas; font-size: 10.5pt; }"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(10)

        button_style = (
            "QPushButton { background-color: #0f1418; color: #e6fbff; border: 1px solid #355160; border-radius: 10px; font-size: 11pt; padding: 10px 14px; }"
            "QPushButton:hover { background-color: #152029; border-color: #52d6ff; }"
            "QPushButton:disabled { color: #6f8794; border-color: #2c3942; }"
        )
        self.back_button = QPushButton("Back to Menu")
        self.back_button.setStyleSheet(button_style)
        self.back_button.clicked.connect(self._on_back_pressed)
        self.criteria_button = QPushButton("Open Safety Criteria")
        self.criteria_button.setStyleSheet(button_style)
        self.criteria_button.clicked.connect(self._open_criteria_window)
        self.zone_polish_button = QPushButton("Open Zone Polish")
        self.zone_polish_button.setStyleSheet(button_style)
        self.zone_polish_button.clicked.connect(self._open_zone_polish_window)
        self.unlock_camera_button = QPushButton("Unlock Camera")
        self.unlock_camera_button.setStyleSheet(button_style)
        self.unlock_camera_button.setEnabled(False)
        self.unlock_camera_button.clicked.connect(self._unlock_camera)
        panel_layout.addWidget(self.back_button)
        panel_layout.addWidget(self.criteria_button)
        panel_layout.addWidget(self.zone_polish_button)
        panel_layout.addWidget(self.unlock_camera_button)

        self.title_label = QLabel("Surface Fusion Monitor")
        self.title_label.setStyleSheet("font-size: 18pt; font-weight: 700; color: #7ce3ff;")
        self.subtitle_label = QLabel("Select an airport from the menu")
        self.subtitle_label.setStyleSheet("font-size: 10.5pt; color: #9fb8c6;")
        self.subtitle_label.setWordWrap(True)
        panel_layout.addWidget(self.title_label)
        panel_layout.addWidget(self.subtitle_label)

        self.status_label = QLabel("System Status: Idle")
        self.map_label = QLabel("Map Status: Not loaded")
        self.gis_label = QLabel("GIS Status: Not loaded")
        self.scale_label = QLabel("Pixels / meter: --")
        self.contacts_label = QLabel("ADS-B Contacts: 0")
        self.follow_label = QLabel("Follow Mode: OFF")
        self.alerts_label = QLabel("Collision Alerts: 0")
        self.legend_label = QLabel("Visual IDs: Aircraft sprite / yellow | GSE icon by type | FOD silver marker | Animal paw warning marker | Conflict red")
        for label in (
            self.status_label,
            self.map_label,
            self.gis_label,
            self.scale_label,
            self.contacts_label,
            self.follow_label,
            self.alerts_label,
            self.legend_label,
        ):
            label.setWordWrap(True)
            label.setStyleSheet("font-size: 11pt; color: #d8dee9;")
            panel_layout.addWidget(label)

        self.contacts_text = QTextEdit()
        self.contacts_text.setReadOnly(True)
        self.alerts_text = QTextEdit()
        self.alerts_text.setReadOnly(True)
        panel_layout.addWidget(QLabel("Ground Traffic and GSE"))
        panel_layout.addWidget(self.contacts_text, stretch=3)
        panel_layout.addWidget(QLabel("Predictive Collision Log"))
        panel_layout.addWidget(self.alerts_text, stretch=2)
        panel_layout.addStretch(1)
        layout.addWidget(panel, stretch=2)

    def _build_alert_overlay(self) -> None:
        self.alert_overlay = QLabel("COLLISION ALERT", self.radar_view.viewport())
        self.alert_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.alert_overlay.setStyleSheet(
            "QLabel { background-color: rgba(255, 59, 48, 165); color: white; border: 4px solid rgba(255,255,255,220); font-family: Bahnschrift; font-size: 34pt; font-weight: 800; letter-spacing: 4px; }"
        )
        self.alert_overlay.hide()

    def _build_hover_tooltip(self) -> None:
        self.hover_tooltip = QLabel(self.radar_view.viewport())
        self.hover_tooltip.setTextFormat(Qt.TextFormat.RichText)
        self.hover_tooltip.setWordWrap(True)
        self.hover_tooltip.setStyleSheet("QLabel { background: transparent; border: none; }")
        self.hover_tooltip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hover_tooltip.hide()

    def _build_criteria_editor(self, panel_layout: QVBoxLayout) -> None:
        section_title = QLabel("Safety Criteria Database")
        section_title.setStyleSheet("font-size: 12pt; font-weight: 700; color: #7ce3ff;")
        panel_layout.addWidget(section_title)

        self.criteria_info_label = QLabel("Rules are stored in SQLite and applied live on the next logic tick.")
        self.criteria_info_label.setWordWrap(True)
        self.criteria_info_label.setStyleSheet("font-size: 10pt; color: #9fb8c6;")
        panel_layout.addWidget(self.criteria_info_label)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        self.criteria_sector_filter = QComboBox()
        self.criteria_sector_filter.addItem("All sectors", "all")
        for sector_name in KNOWN_SECTORS:
            self.criteria_sector_filter.addItem(sector_name.title(), sector_name)
        self.criteria_primary_filter = QComboBox()
        self.criteria_primary_filter.addItem("All origins", "all")
        for primary_kind in KNOWN_PRIMARY_KINDS:
            self.criteria_primary_filter.addItem(primary_kind.title(), primary_kind)
        self.criteria_other_filter = QComboBox()
        self.criteria_other_filter.addItem("All targets", "all")
        for other_kind in KNOWN_OTHER_KINDS:
            self.criteria_other_filter.addItem(other_kind.title(), other_kind)
        for widget in (self.criteria_sector_filter, self.criteria_primary_filter, self.criteria_other_filter):
            widget.currentIndexChanged.connect(self._refresh_criteria_table)
            filter_row.addWidget(widget)
        panel_layout.addLayout(filter_row)

        self.criteria_table = QTableWidget(0, 8)
        self.criteria_table.setHorizontalHeaderLabels(["Sector", "Origin", "Target", "V Min", "V Max", "Green", "Yellow", "Red"])
        self.criteria_table.verticalHeader().setVisible(False)
        self.criteria_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.criteria_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.criteria_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.criteria_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.criteria_table.itemSelectionChanged.connect(self._load_selected_criteria_rule)
        self.criteria_table.setMinimumHeight(180)
        panel_layout.addWidget(self.criteria_table)

        form_grid = QGridLayout()
        form_grid.setHorizontalSpacing(8)
        form_grid.setVerticalSpacing(6)
        self.criteria_sector_edit = QComboBox()
        for sector_name in KNOWN_SECTORS:
            self.criteria_sector_edit.addItem(sector_name.title(), sector_name)
        self.criteria_primary_edit = QComboBox()
        for primary_kind in KNOWN_PRIMARY_KINDS:
            self.criteria_primary_edit.addItem(primary_kind.title(), primary_kind)
        self.criteria_other_edit = QComboBox()
        for other_kind in KNOWN_OTHER_KINDS:
            self.criteria_other_edit.addItem(other_kind.title(), other_kind)
        self.criteria_speed_min_spin = self._build_criteria_spinbox(0.0, 80.0, 1)
        self.criteria_speed_max_spin = self._build_criteria_spinbox(0.1, 120.0, 1)
        self.criteria_green_spin = self._build_criteria_spinbox(0.0, 500.0, 1)
        self.criteria_yellow_spin = self._build_criteria_spinbox(0.0, 500.0, 1)
        self.criteria_red_spin = self._build_criteria_spinbox(0.0, 500.0, 1)
        form_grid.addWidget(QLabel("Sector"), 0, 0)
        form_grid.addWidget(self.criteria_sector_edit, 0, 1)
        form_grid.addWidget(QLabel("Origin"), 0, 2)
        form_grid.addWidget(self.criteria_primary_edit, 0, 3)
        form_grid.addWidget(QLabel("Target"), 1, 0)
        form_grid.addWidget(self.criteria_other_edit, 1, 1)
        form_grid.addWidget(QLabel("V Min"), 1, 2)
        form_grid.addWidget(self.criteria_speed_min_spin, 1, 3)
        form_grid.addWidget(QLabel("V Max"), 2, 0)
        form_grid.addWidget(self.criteria_speed_max_spin, 2, 1)
        form_grid.addWidget(QLabel("Green"), 2, 2)
        form_grid.addWidget(self.criteria_green_spin, 2, 3)
        form_grid.addWidget(QLabel("Yellow"), 3, 0)
        form_grid.addWidget(self.criteria_yellow_spin, 3, 1)
        form_grid.addWidget(QLabel("Red"), 3, 2)
        form_grid.addWidget(self.criteria_red_spin, 3, 3)
        panel_layout.addLayout(form_grid)

        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        self.criteria_new_button = QPushButton("New Rule")
        self.criteria_new_button.clicked.connect(self._prepare_new_criteria_rule)
        self.criteria_save_button = QPushButton("Save Rule")
        self.criteria_save_button.clicked.connect(self._save_criteria_rule)
        self.criteria_delete_button = QPushButton("Delete Rule")
        self.criteria_delete_button.clicked.connect(self._delete_criteria_rule)
        self.criteria_reset_button = QPushButton("Reset Airport")
        self.criteria_reset_button.clicked.connect(self._reset_criteria_rules)
        for button in (self.criteria_new_button, self.criteria_save_button, self.criteria_delete_button, self.criteria_reset_button):
            button_row.addWidget(button)
        panel_layout.addLayout(button_row)
        self._set_criteria_editor_enabled(False)

    def _build_criteria_spinbox(self, minimum: float, maximum: float, decimals: int) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox()
        spinbox.setRange(minimum, maximum)
        spinbox.setDecimals(decimals)
        spinbox.setSingleStep(0.5 if decimals > 0 else 1.0)
        spinbox.setSuffix(" m/s" if maximum <= 120.0 else " m")
        return spinbox

    def _set_criteria_editor_enabled(self, enabled: bool) -> None:
        for widget in (
            self.criteria_sector_filter,
            self.criteria_primary_filter,
            self.criteria_other_filter,
            self.criteria_table,
            self.criteria_sector_edit,
            self.criteria_primary_edit,
            self.criteria_other_edit,
            self.criteria_speed_min_spin,
            self.criteria_speed_max_spin,
            self.criteria_green_spin,
            self.criteria_yellow_spin,
            self.criteria_red_spin,
            self.criteria_new_button,
            self.criteria_save_button,
            self.criteria_delete_button,
            self.criteria_reset_button,
        ):
            widget.setEnabled(enabled)

    def _refresh_criteria_table(self) -> None:
        if self.airport is None:
            self.criteria_table.setRowCount(0)
            self._visible_criteria_rules = ()
            return
        self.criteria_repository.ensure_airport_defaults(self.airport.code)
        sector_name = self.criteria_sector_filter.currentData()
        primary_kind = self.criteria_primary_filter.currentData()
        other_kind = self.criteria_other_filter.currentData()
        self._visible_criteria_rules = self.criteria_repository.list_rules(
            self.airport.code,
            sector_name=None if sector_name == "all" else str(sector_name),
            primary_kind=None if primary_kind == "all" else str(primary_kind),
            other_kind=None if other_kind == "all" else str(other_kind),
        )
        self.criteria_table.blockSignals(True)
        self.criteria_table.setRowCount(len(self._visible_criteria_rules))
        selected_row = 0
        for row_index, rule in enumerate(self._visible_criteria_rules):
            values = (
                rule.sector_name,
                rule.primary_kind,
                rule.other_kind,
                f"{rule.speed_min_mps:.1f}",
                f"{rule.speed_max_mps:.1f}",
                f"{rule.green_m:.1f}",
                f"{rule.yellow_m:.1f}",
                f"{rule.red_m:.1f}",
            )
            for column_index, value in enumerate(values):
                self.criteria_table.setItem(row_index, column_index, QTableWidgetItem(value))
            if rule.rule_id == self._selected_criteria_rule_id:
                selected_row = row_index
        self.criteria_table.blockSignals(False)
        if self._visible_criteria_rules:
            self.criteria_table.selectRow(selected_row)
            self._load_rule_into_form(self._visible_criteria_rules[selected_row])
        else:
            self._selected_criteria_rule_id = None
            self._prepare_new_criteria_rule()
        self.criteria_info_label.setText(
            f"SQLite: {self.criteria_repository.db_path.name} | Airport {self.airport.code} | Changes apply automatically."
        )

    def _load_selected_criteria_rule(self) -> None:
        selected_rows = self.criteria_table.selectionModel().selectedRows()
        if not selected_rows:
            return
        row_index = selected_rows[0].row()
        if 0 <= row_index < len(self._visible_criteria_rules):
            self._load_rule_into_form(self._visible_criteria_rules[row_index])

    def _load_rule_into_form(self, rule: SafetyCriteriaRule) -> None:
        self._criteria_form_loading = True
        self._selected_criteria_rule_id = rule.rule_id
        self.criteria_sector_edit.setCurrentIndex(max(0, self.criteria_sector_edit.findData(rule.sector_name)))
        self.criteria_primary_edit.setCurrentIndex(max(0, self.criteria_primary_edit.findData(rule.primary_kind)))
        self.criteria_other_edit.setCurrentIndex(max(0, self.criteria_other_edit.findData(rule.other_kind)))
        self.criteria_speed_min_spin.setValue(rule.speed_min_mps)
        self.criteria_speed_max_spin.setValue(rule.speed_max_mps)
        self.criteria_green_spin.setValue(rule.green_m)
        self.criteria_yellow_spin.setValue(rule.yellow_m)
        self.criteria_red_spin.setValue(rule.red_m)
        self._criteria_form_loading = False

    def _prepare_new_criteria_rule(self) -> None:
        self._selected_criteria_rule_id = None
        if self.airport is None:
            return
        sector_name = self.criteria_sector_filter.currentData()
        primary_kind = self.criteria_primary_filter.currentData()
        other_kind = self.criteria_other_filter.currentData()
        self._criteria_form_loading = True
        self.criteria_sector_edit.setCurrentIndex(max(0, self.criteria_sector_edit.findData("global" if sector_name == "all" else sector_name)))
        self.criteria_primary_edit.setCurrentIndex(max(0, self.criteria_primary_edit.findData("aircraft" if primary_kind == "all" else primary_kind)))
        self.criteria_other_edit.setCurrentIndex(max(0, self.criteria_other_edit.findData("vehicle" if other_kind == "all" else other_kind)))
        self.criteria_speed_min_spin.setValue(0.0)
        self.criteria_speed_max_spin.setValue(8.0)
        self.criteria_green_spin.setValue(30.0)
        self.criteria_yellow_spin.setValue(18.0)
        self.criteria_red_spin.setValue(9.0)
        self._criteria_form_loading = False
        self.criteria_table.clearSelection()

    def _save_criteria_rule(self) -> None:
        if self.airport is None:
            return
        green_m = self.criteria_green_spin.value()
        yellow_m = min(self.criteria_yellow_spin.value(), green_m)
        red_m = min(self.criteria_red_spin.value(), yellow_m)
        rule = SafetyCriteriaRule(
            rule_id=self._selected_criteria_rule_id,
            airport_code=self.airport.code,
            sector_name=str(self.criteria_sector_edit.currentData()),
            primary_kind=str(self.criteria_primary_edit.currentData()),
            other_kind=str(self.criteria_other_edit.currentData()),
            speed_min_mps=self.criteria_speed_min_spin.value(),
            speed_max_mps=max(self.criteria_speed_max_spin.value(), self.criteria_speed_min_spin.value() + 0.1),
            green_m=green_m,
            yellow_m=yellow_m,
            red_m=red_m,
        )
        stored_rule = self.criteria_repository.upsert_rule(rule)
        self._selected_criteria_rule_id = stored_rule.rule_id
        self._refresh_criteria_table()

    def _delete_criteria_rule(self) -> None:
        if self.airport is None or self._selected_criteria_rule_id is None:
            return
        self.criteria_repository.delete_rule(self.airport.code, self._selected_criteria_rule_id)
        self._selected_criteria_rule_id = None
        self._refresh_criteria_table()

    def _reset_criteria_rules(self) -> None:
        if self.airport is None:
            return
        response = QMessageBox.question(
            self,
            "Reset Criteria",
            f"Reset all safety criteria rules for {self.airport.code} to defaults?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        self.criteria_repository.reset_airport_defaults(self.airport.code)
        self._selected_criteria_rule_id = None
        self._refresh_criteria_table()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        rect = self.radar_view.viewport().rect()
        width = max(360, int(rect.width() * 0.72))
        height = 120
        self.alert_overlay.setGeometry(int((rect.width() - width) / 2), int((rect.height() - height) / 2), width, height)

    def activate_airport(self, airport: AirportConfig) -> None:
        self.shutdown_worker()
        self.airport = airport
        self.geo = GeoReference(airport.center_lat, airport.center_lon)
        self.mapper = WebMercatorMapper(airport)
        self.gis_manager = GISManager(airport, self.geo, self.mapper, self.zone_tuning_repository)
        self.gis_context = self.gis_manager.load_context()
        self.radar_viewmodel = build_radar_viewmodel(
            self.geo,
            self.gis_manager,
            self.gis_context,
            airport.code,
            self.criteria_repository,
        )
        self.radar_viewmodel.view_state_ready.connect(self._on_view_state_ready)
        self.radar_viewmodel.physics_failed.connect(self._on_physics_failed)
        self.latest_status = f"{airport.code} loading telemetry"
        self.latest_update_utc = "--"
        self.title_label.setText(f"{airport.code} Spatial Surface Monitor")
        self.subtitle_label.setText(f"{airport.display_name} | {airport.city}")
        self.map_label.setText(f"Map Status: {self.gis_context.map_status}")
        self.gis_label.setText(f"GIS Status: {self.gis_context.gis_status}")
        self.scale_label.setText(
            f"Pixels / meter: X {self.gis_context.pixels_per_meter_x:.3f} | Y {self.gis_context.pixels_per_meter_y:.3f}"
        )
        self.support_traffic.activate_airport(self.geo, self.gis_manager, self.gis_context)
        self._vehicle_color_hex_by_id = {vehicle.actor_id: vehicle.color_hex for vehicle in self.support_traffic.vehicles}
        self._hide_scene_tooltip()
        self.selected_actor_id = None
        self.follow_mode_enabled = False
        self._auto_framed_live_traffic = False
        self._latest_view_state = None
        self._last_support_sync_monotonic = 0.0
        self._last_detail_panel_refresh_monotonic = 0.0
        self.radar_view.reset_camera()
        self.scene_controller.rebuild_scene(self.airport.code, self.gis_context, self.geo, self.mapper)
        self.scene_controller.frame_global_scene()
        self.radar_viewmodel.update_support_actors(self.support_traffic.vehicles, self.support_traffic.obstacles)
        self.worker = OpenSkyTelemetryWorker(airport)
        self.worker.telemetry_updated.connect(self.on_telemetry_updated)
        self.worker.start()
        self.timer.start()

    def shutdown_worker(self) -> None:
        self.timer.stop()
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(5000)
            self.worker.deleteLater()
            self.worker = None
        if self.radar_viewmodel is not None:
            self.radar_viewmodel.shutdown()
            self.radar_viewmodel.deleteLater()
            self.radar_viewmodel = None
        self.support_traffic.clear()
        self._vehicle_color_hex_by_id = {}
        self._hide_scene_tooltip()

    def on_telemetry_updated(self, snapshot: TelemetrySnapshot) -> None:
        if self.airport is None or snapshot.airport_code != self.airport.code or self.radar_viewmodel is None:
            return
        self.latest_status = snapshot.status
        self.latest_update_utc = snapshot.updated_at_utc
        self.radar_viewmodel.ingest_telemetry_snapshot(snapshot)
        if snapshot.aircraft and not self._auto_framed_live_traffic:
            assert self.mapper is not None
            self.scene_controller.frame_aircraft_cluster(snapshot.aircraft, self.mapper)
            self._auto_framed_live_traffic = True

    def _on_animation_tick(self) -> None:
        if self.geo is None or self.mapper is None or self.gis_context is None or self.gis_manager is None or self.radar_viewmodel is None:
            return
        now_monotonic = time.monotonic()
        self.support_traffic.update(1.0 / RENDER_FPS)
        if (now_monotonic - self._last_support_sync_monotonic) >= (1.0 / SUPPORT_SYNC_HZ):
            self.radar_viewmodel.update_support_actors(self.support_traffic.vehicles, self.support_traffic.obstacles)
            self._last_support_sync_monotonic = now_monotonic
        if self._latest_view_state is not None:
            self._render_view_state(self._latest_view_state, now_monotonic)

    def _on_view_state_ready(self, view_state: RadarViewState) -> None:
        self._latest_view_state = view_state

    def _render_view_state(self, view_state: RadarViewState, now_monotonic: float | None = None) -> None:
        if self.geo is None or self.mapper is None or self.gis_context is None:
            return
        if now_monotonic is None:
            now_monotonic = time.monotonic()
        self.latest_status = view_state.telemetry_status
        self.latest_update_utc = view_state.telemetry_updated_at_utc
        aircraft_states = {actor_id: track.to_render_state() for actor_id, track in view_state.tracks.items()}
        vehicle_states = {vehicle.actor_id: vehicle.as_render_state() for vehicle in self.support_traffic.vehicles}
        viewport_frame = self.viewport_state_machine.build_frame_state(aircraft_states, view_state.sensor_envelopes)
        visible_alerts = self._visible_alerts(tuple(view_state.alerts), viewport_frame, aircraft_states, vehicle_states)
        conflict_messages = [self._format_alert_message(alert) for alert in visible_alerts]
        self._sync_obstacle_conflicts(tuple(view_state.alerts))
        self.scene_controller.render_obstacles(self.support_traffic.obstacles, self.gis_context, self.mapper, viewport_frame, aircraft_states.get(viewport_frame.focus_actor_id or ""), self.geo)
        self.scene_controller.render_actors(
            self.geo,
            self.mapper,
            self.gis_context.pixels_per_meter_mean,
            aircraft_states,
            view_state.predictions,
            set(view_state.aircraft_conflicts),
            vehicle_states,
            view_state.predictions,
            set(),
            set(view_state.branch_conflicts),
            view_state.sensor_envelopes,
            viewport_frame,
            self._vehicle_color_hex_by_id,
        )
        self._update_status_labels(aircraft_states, vehicle_states, len(conflict_messages))
        if (now_monotonic - self._last_detail_panel_refresh_monotonic) >= (1.0 / DETAIL_PANEL_REFRESH_HZ):
            self._update_detail_panels(aircraft_states, vehicle_states, conflict_messages)
            self._last_detail_panel_refresh_monotonic = now_monotonic
        self._update_follow_camera(aircraft_states, viewport_frame)
        self._update_alert_overlay(self._critical_overlay_text(visible_alerts))

    def _on_physics_failed(self, error_text: str) -> None:
        self.latest_status = f"Physics worker degraded: {error_text}"
        self.status_label.setText(f"System Status: {self.latest_status} | Last update {self.latest_update_utc}")

    def _update_status_labels(
        self,
        aircraft_states: dict[str, RenderState],
        vehicle_states: dict[str, RenderState],
        conflict_count: int,
    ) -> None:
        self.status_label.setText(f"System Status: {self.latest_status} | Last update {self.latest_update_utc}")
        self.contacts_label.setText(f"ADS-B Contacts: {len(aircraft_states)} ground aircraft | GSE: {len(vehicle_states)}")
        if self.follow_mode_enabled and self.selected_actor_id in aircraft_states:
            self.follow_label.setText(f"Viewport: COCKPIT AMMD | Track-Up {aircraft_states[self.selected_actor_id].callsign}")
        else:
            self.follow_label.setText("Viewport: TOWER | North-Up")
        self.alerts_label.setText(f"Collision Alerts: {conflict_count}")

    def _update_detail_panels(
        self,
        aircraft_states: dict[str, RenderState],
        vehicle_states: dict[str, RenderState],
        conflict_messages: list[str],
    ) -> None:
        contact_blocks: list[str] = []
        for state in sorted(aircraft_states.values(), key=lambda item: item.callsign):
            contact_blocks.append(
                f"{state.callsign:<8} ACFT | HDG {state.heading_deg:6.1f}° | GS {state.speed_mps:5.1f} m/s\n"
                f"POS {state.latitude:+.5f}, {state.longitude:+.5f}\n"
                f"PROFILE {state.profile_label} {state.length_m:.1f}m x {state.width_m:.1f}m"
            )
        for state in sorted(vehicle_states.values(), key=lambda item: item.callsign):
            contact_blocks.append(
                f"{state.callsign:<8} GSE  | HDG {state.heading_deg:6.1f}° | GS {state.speed_mps:5.1f} m/s\n"
                f"POS {state.latitude:+.5f}, {state.longitude:+.5f}\n"
                f"PROFILE {state.profile_label} {state.length_m:.1f}m x {state.width_m:.1f}m"
            )
        self.contacts_text.setPlainText("\n\n".join(contact_blocks) if contact_blocks else "No ground aircraft available in the selected airport box.")
        self.alerts_text.setPlainText("\n".join(conflict_messages) if conflict_messages else "No predictive conflicts detected.")

    def _update_follow_camera(self, aircraft_states: dict[str, RenderState], viewport_frame) -> None:
        if self.mapper is None:
            return
        focus_state = aircraft_states.get(viewport_frame.focus_actor_id or "")
        self.scene_controller.apply_viewport(viewport_frame, focus_state, self.mapper)

    def _update_alert_overlay(self, overlay_text: str | None) -> None:
        if overlay_text is None:
            self.alert_overlay.hide()
            self.alert_phase = 0
            return
        self.alert_overlay.setText(overlay_text)
        self.alert_phase += 1
        if (self.alert_phase // 6) % 2 == 0:
            self.alert_overlay.show()
        else:
            self.alert_overlay.hide()

    def _select_aircraft(self, actor_id: str) -> None:
        self.selected_actor_id = actor_id
        self.follow_mode_enabled = True
        self.viewport_state_machine.enter_cockpit_mode(actor_id)
        self.unlock_camera_button.setEnabled(True)
        self._on_animation_tick()

    def _unlock_camera(self) -> None:
        self.selected_actor_id = None
        self.follow_mode_enabled = False
        self.viewport_state_machine.enter_tower_mode()
        self.unlock_camera_button.setEnabled(False)
        self.scene_controller.frame_global_scene()

    def _open_criteria_window(self) -> None:
        self.criteria_requested.emit(self.airport.code if self.airport is not None else "")

    def _open_zone_polish_window(self) -> None:
        self.zone_polish_requested.emit(self.airport.code if self.airport is not None else "")

    def _on_back_pressed(self) -> None:
        self.shutdown_worker()
        self._latest_view_state = None
        self.selected_actor_id = None
        self.follow_mode_enabled = False
        self.viewport_state_machine.enter_tower_mode()
        self.scene_controller.reset_dynamic_items()
        self.alert_overlay.hide()
        self.status_label.setText("System Status: Idle")
        self._hide_scene_tooltip()
        self._vehicle_color_hex_by_id = {}
        self.map_label.setText("Map Status: Not loaded")
        self.gis_label.setText("GIS Status: Not loaded")
        self.scale_label.setText("Pixels / meter: --")
        self.contacts_label.setText("ADS-B Contacts: 0")
        self.follow_label.setText("Follow Mode: OFF")
        self.alerts_label.setText("Collision Alerts: 0")
        self.contacts_text.setPlainText("")
        self.alerts_text.setPlainText("")
        self.back_requested.emit()

    def _visible_alerts(self, alerts: tuple[object, ...], viewport_frame, aircraft_states: dict[str, RenderState], vehicle_states: dict[str, RenderState]) -> tuple[object, ...]:
        if viewport_frame.mode != "cockpit" or viewport_frame.focus_actor_id is None or self.geo is None:
            return alerts
        focus_state = aircraft_states.get(viewport_frame.focus_actor_id)
        if focus_state is None:
            return alerts
        visible_alerts: list[object] = []
        all_states = dict(aircraft_states)
        all_states.update(vehicle_states)
        for alert in alerts:
            actor_id = getattr(alert, "actor_id", "")
            other_id = getattr(alert, "other_id", "")
            if actor_id == viewport_frame.focus_actor_id or other_id == viewport_frame.focus_actor_id:
                visible_alerts.append(alert)
                continue
            actor_state = all_states.get(actor_id)
            other_state = all_states.get(other_id)
            if actor_state is not None and self.scene_controller._actor_opacity(actor_state, viewport_frame, focus_state, self.geo) >= 1.0:
                visible_alerts.append(alert)
                continue
            if other_state is not None and self.scene_controller._actor_opacity(other_state, viewport_frame, focus_state, self.geo) >= 1.0:
                visible_alerts.append(alert)
        return tuple(visible_alerts)

    def _format_alert_message(self, alert: object) -> str:
        summary = str(getattr(alert, "summary", alert))
        severity = str(getattr(alert, "severity", "")).upper()
        ttc_seconds = getattr(alert, "ttc_seconds", None)
        if ttc_seconds is None:
            return f"[{severity}] {summary}" if severity else summary
        return f"[{severity}] {summary} | TTC {ttc_seconds:.1f}s"

    def _critical_overlay_text(self, alerts: tuple[object, ...] | list[object]) -> str | None:
        critical_alerts = [alert for alert in alerts if str(getattr(alert, "severity", "")) == "critical"]
        if not critical_alerts:
            return None
        if any(
            "ANIMAL EN PISTA" in str(getattr(alert, "summary", ""))
            or str(getattr(alert, "other_id", "")).startswith("ANM-")
            for alert in critical_alerts
        ):
            return "ANIMAL EN PISTA"
        return "COLLISION ALERT"

    def _sync_obstacle_conflicts(self, alerts: tuple[object, ...]) -> None:
        obstacle_alerts: dict[str, tuple[int, str]] = {}
        for alert in alerts:
            other_id = str(getattr(alert, "other_id", ""))
            if not other_id:
                continue
            severity = str(getattr(alert, "severity", ""))
            rank = {"critical": 3, "warning": 2, "advisory": 1}.get(severity, 0)
            actor_callsign = str(getattr(alert, "actor_callsign", ""))
            existing = obstacle_alerts.get(other_id)
            if existing is None or rank > existing[0]:
                obstacle_alerts[other_id] = (rank, actor_callsign)
        for obstacle in self.support_traffic.obstacles:
            rank, actor_callsign = obstacle_alerts.get(obstacle.obstacle_id, (0, ""))
            obstacle.in_conflict = rank >= 2
            obstacle.conflicting_actor = actor_callsign if rank > 0 else ""

    def _show_scene_tooltip(self, scene_pos, tooltip_html: str) -> None:
        if not tooltip_html or not isinstance(scene_pos, QPointF):
            self._hide_scene_tooltip()
            return
        viewport_point = self.radar_view.mapFromScene(scene_pos)
        self.hover_tooltip.setText(tooltip_html)
        self.hover_tooltip.adjustSize()
        self._reposition_hover_tooltip(viewport_point + QPoint(18, -18))
        self.hover_tooltip.show()
        self.hover_tooltip.raise_()

    def _reposition_hover_tooltip(self, viewport_point: QPoint) -> None:
        margin = 12
        tooltip_width = self.hover_tooltip.width()
        tooltip_height = self.hover_tooltip.height()
        viewport_rect = self.radar_view.viewport().rect()
        x_pos = min(max(viewport_point.x(), margin), max(margin, viewport_rect.width() - tooltip_width - margin))
        y_pos = min(max(viewport_point.y(), margin), max(margin, viewport_rect.height() - tooltip_height - margin))
        self.hover_tooltip.move(x_pos, y_pos)

    def _hide_scene_tooltip(self) -> None:
        self.hover_tooltip.hide()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Spatially Aware A-SMGCS HMI")
        self.resize(1700, 1000)
        self.criteria_repository = SafetyCriteriaRepository(Path(__file__).resolve().parents[2] / "data" / DEFAULT_DB_FILENAME)
        self.zone_tuning_repository = ZoneTuningRepository(Path(__file__).resolve().parents[2] / "data" / "zone_tuning.json")
        self.criteria_window = SafetyCriteriaWindow(self.criteria_repository)
        self.zone_polish_window = ZonePolishWindow(self.zone_tuning_repository)
        self.stack = QStackedWidget(self)
        self.setCentralWidget(self.stack)
        self.menu_page = StartMenuPage()
        self.radar_page = RadarPage(self.criteria_repository, self.zone_tuning_repository)
        self.stack.addWidget(self.menu_page)
        self.stack.addWidget(self.radar_page)
        self.menu_page.airport_selected.connect(self.open_airport)
        self.menu_page.criteria_requested.connect(self.open_criteria_window)
        self.menu_page.zone_polish_requested.connect(self.open_zone_polish_window)
        self.radar_page.back_requested.connect(self.show_menu)
        self.radar_page.criteria_requested.connect(self.open_criteria_window)
        self.radar_page.zone_polish_requested.connect(self.open_zone_polish_window)
        self.zone_polish_window.zone_polish_updated.connect(self._reload_airport_zone_polish)
        self.show_menu()

    def open_airport(self, airport_code: str) -> None:
        self.radar_page.activate_airport(AIRPORTS[airport_code])
        self.stack.setCurrentWidget(self.radar_page)
        self.setWindowTitle(f"Spatially Aware A-SMGCS HMI - {airport_code}")

    def show_menu(self) -> None:
        self.stack.setCurrentWidget(self.menu_page)
        self.setWindowTitle("Spatially Aware A-SMGCS HMI")

    def open_criteria_window(self, airport_code: str = "") -> None:
        self.criteria_window.focus_airport(airport_code or None)

    def open_zone_polish_window(self, airport_code: str = "") -> None:
        self.zone_polish_window.focus_airport(airport_code or None)

    def _reload_airport_zone_polish(self, airport_code: str) -> None:
        if self.radar_page.airport is None or self.radar_page.airport.code != airport_code:
            return
        self.radar_page.activate_airport(AIRPORTS[airport_code])

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.radar_page.shutdown_worker()
        self.criteria_window.close()
        self.zone_polish_window.close()
        super().closeEvent(event)


__all__ = ["MainWindow", "RadarPage", "StartMenuPage"]