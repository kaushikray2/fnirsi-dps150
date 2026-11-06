"""FNIRSI DPS-150 desktop GUI v2 (PySide6), built on fnirsi.py.

Dark instrument-style interface modelled on the layout of the official
FNIRSI upper-computer app: Basic tab (live V/I chart, dial gauges,
preset groups, Ah/Wh measurement, brightness, device information) and
an Advanced tab (voltage scan, current scan, programmable sequence
table with auto/manual modes and cycles).

Run on the machine the supply is plugged into:
    python3 fnirsi_gui.py [--port /dev/ttyACM0]

Safety/behaviour:
- Auto-connects on launch; NEVER enables the output by itself.
- Scans and sequences run on a worker thread. On finish, stop or error
  they restore the setpoints and output state that existed before the
  run. Pause holds the current step with the output as-is.
- Closing the window disconnects USB but leaves the output untouched.
"""
import argparse
import sys
import threading
import time
from collections import deque

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QSlider, QSpinBox,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from fnirsi import FnirsiDPS150, PROTECTION_NAMES

CYAN = QColor('#38bdf8')
GREEN = QColor('#4ade80')
DIM = QColor('#5b7a8a')
GRID = QColor('#1c3542')
TEXT = QColor('#d9f2ff')

QSS = """
QMainWindow, QWidget { background:#0b0f14; color:#cfe8f5; }
QGroupBox { border:1px solid #14586e; border-radius:6px; margin-top:14px;
            padding:8px; background:#0e141c; }
QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 4px;
                   color:#38bdf8; font-weight:bold; }
QPushButton { background:#12303f; border:1px solid #1f6f8b; border-radius:5px;
              padding:6px 10px; color:#d9f2ff; }
QPushButton:hover { background:#17455c; }
QPushButton:disabled { color:#44606e; border-color:#1a2f3a; background:#0d1a22; }
QPushButton:checked { background:#0e7490; color:white; }
QDoubleSpinBox, QSpinBox { background:#0a1219; border:1px solid #1f6f8b;
              border-radius:4px; padding:3px; color:#eaf7ff; }
QTabWidget::pane { border:1px solid #14586e; }
QTabBar::tab { background:#0e1a24; padding:8px 26px; color:#9fd8ef; font-size:14px; }
QTabBar::tab:selected { background:#0e7490; color:white; }
QTableWidget { background:#0a1219; gridline-color:#1c3542; color:#d9f2ff; }
QHeaderView::section { background:#12303f; color:#9fd8ef; padding:4px; }
QTableWidget::item:selected { background:#0e7490; }
QSlider::groove:horizontal { height:6px; background:#12303f; border-radius:3px; }
QSlider::handle:horizontal { width:16px; background:#38bdf8; border-radius:8px; margin:-5px 0; }
QCheckBox { color:#cfe8f5; spacing:6px; }
"""


class ChartWidget(QWidget):
    """Rolling dual-axis trend of output volts (left, cyan) and amps
    (right, green), fed from the device telemetry stream."""
    def __init__(self, max_points=240):
        super().__init__()
        self.setMinimumHeight(210)
        self.v = deque(maxlen=max_points)
        self.a = deque(maxlen=max_points)
        self.max_v = 12.0
        self.max_a = 5.0
        self.show_v = True
        self.show_a = True
        self.recording = True

    def push(self, volts, amps):
        if self.recording:
            self.v.append(volts); self.a.append(amps)
        self.update()

    def clear(self):
        self.v.clear(); self.a.clear(); self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        ml, mr, mt, mb = 44, 44, 10, 22
        iw, ih = w - ml - mr, h - mt - mb
        p.setPen(QPen(GRID, 1))
        p.setFont(QFont('Sans', 8))
        for i in range(6):
            y = mt + ih * i / 5
            p.drawLine(ml, int(y), ml + iw, int(y))
            p.setPen(TEXT)
            p.drawText(0, int(y) - 3, 40, 12, Qt.AlignRight, f'{self.max_v * (5 - i) / 5:.0f}')
            p.setPen(QColor('#4ade80'))
            p.drawText(ml + iw + 4, int(y) - 3, 40, 12, Qt.AlignLeft, f'{self.max_a * (5 - i) / 5:.1f}')
            p.setPen(QPen(GRID, 1))
        p.drawRect(ml, mt, iw, ih)
        n = max(len(self.v), len(self.a), 2)
        def trace(data, mx, color):
            if len(data) < 2:
                return
            p.setPen(QPen(color, 2))
            pts = list(data)
            for i in range(1, len(pts)):
                x1 = ml + iw * (i - 1) / (n - 1); x2 = ml + iw * i / (n - 1)
                y1 = mt + ih * (1 - min(pts[i - 1], mx) / mx)
                y2 = mt + ih * (1 - min(pts[i], mx) / mx)
                p.drawLine(int(x1), int(y1), int(x2), int(y2))
        if self.show_v:
            trace(self.v, self.max_v, CYAN)
        if self.show_a:
            trace(self.a, self.max_a, GREEN)
        p.setPen(DIM)
        p.drawText(ml, h - 6, 'sample count →')


class GaugeWidget(QWidget):
    """270-degree arc gauge: output value vs live device ceiling, with a
    setpoint tick. Center shows the digital value."""
    def __init__(self, title, unit, color):
        super().__init__()
        self.setMinimumSize(210, 185)
        self.title, self.unit, self.color = title, unit, color
        self.value = None; self.limit = None; self.setpoint = None

    def set(self, value, limit, setpoint):
        self.value, self.limit, self.setpoint = value, limit, setpoint
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy, r = w / 2, h / 2 + 8, min(w, h) / 2 - 16
        rect_x, rect_y, rect_d = cx - r, cy - r, 2 * r
        START, SWEEP = 225 * 16, -270 * 16  # 225 deg, clockwise 270 deg
        p.setPen(QPen(QColor('#17303d'), 13, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(int(rect_x), int(rect_y), int(rect_d), int(rect_d), START, SWEEP)
        frac = 0.0
        if self.value is not None and self.limit:
            frac = max(0.0, min(1.0, self.value / self.limit))
        if frac > 0:
            p.setPen(QPen(self.color, 13, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(int(rect_x), int(rect_y), int(rect_d), int(rect_d), START, int(SWEEP * frac))
        if self.setpoint is not None and self.limit:
            import math
            ang = math.radians(225 - 270 * max(0.0, min(1.0, self.setpoint / self.limit)))
            x1 = cx + (r - 12) * math.cos(ang); y1 = cy - (r - 12) * math.sin(ang)
            x2 = cx + (r + 2) * math.cos(ang); y2 = cy - (r + 2) * math.sin(ang)
            p.setPen(QPen(QColor('#facc15'), 3))
            p.drawLine(int(x1), int(y1), int(x2), int(y2))
        p.setPen(TEXT)
        big = QFont('Sans', 25); big.setBold(True)
        p.setFont(big)
        txt = '—' if self.value is None else f'{self.value:.2f}'
        p.drawText(0, int(cy) - 34, w, 40, Qt.AlignHCenter, txt)
        p.setFont(QFont('Sans', 11))
        p.setPen(self.color)
        p.drawText(0, int(cy) + 4, w, 20, Qt.AlignHCenter, f'{self.title} · {self.unit}')
        p.setFont(QFont('Sans', 9)); p.setPen(DIM)
        lim = '—' if not self.limit else f'{self.limit:.2f}{self.unit}'
        stp = '—' if self.setpoint is None else f'{self.setpoint:.2f}{self.unit}'
        p.drawText(0, int(cy) + 24, w, 16, Qt.AlignHCenter, f'Upper limit {lim}')
        p.drawText(0, int(cy) + 40, w, 16, Qt.AlignHCenter, f'Set {stp}')


class StatusWorker(QThread):
    """Polls output state / protection / metering and (every 4th cycle)
    the full dump, so front-panel changes on the device also appear."""
    updated = Signal()
    failed = Signal(str)

    def __init__(self, gui):
        super().__init__(gui)
        self.gui = gui
        self.running = True

    def run(self):
        n = 0
        while self.running:
            time.sleep(1.5)
            if not self.running or not self.gui.psu:
                continue
            try:
                with self.gui.io_lock:
                    self.gui.psu.refresh_output_state()
                    n += 1
                    if n % 4 == 0:
                        self.gui.psu.read_all()
                self.updated.emit()
            except Exception as e:
                self.failed.emit(str(e))
                return


class Runner(QThread):
    """Executes a scan or a sequence.

    Job dict:
      kind: 'sequence' | 'vscan' | 'iscan'
      steps: [(volts, amps), ...]         (already range-expanded)
      dwell: seconds per step (auto mode; scans: the delay spin)
      cycles: int (sequence only)
      manual: bool (sequence only - wait for Continue between steps)
      first_index: table row index of steps[0] (for highlight), or None
    On any exit path the pre-run setpoints and output state are restored.
    """
    log = Signal(str)
    step = Signal(int)          # index into steps (-1 = none)
    done = Signal(str)

    def __init__(self, gui, job):
        super().__init__(gui)
        self.gui = gui
        self.job = job
        self.stop_flag = threading.Event()
        self.paused = threading.Event()
        self.advance = threading.Event()

    def request_stop(self):
        self.stop_flag.set(); self.advance.set(); self.paused.clear()

    def set_paused(self, on):
        if on:
            self.paused.set()
        else:
            self.paused.clear()

    def _apply(self, v, a):
        psu = self.gui.psu
        with self.gui.io_lock:
            if abs((psu.state['set_v'] or 0) - v) > 0.001:
                psu.set_voltage(v)
            if abs((psu.state['set_a'] or 0) - a) > 0.001:
                psu.set_current(a)
            if not psu.state['output_on']:
                psu.output_on()

    def _dwell(self, seconds):
        remaining = seconds
        while remaining > 0:
            if self.stop_flag.is_set():
                return False
            if self.paused.is_set():
                time.sleep(0.05); continue
            time.sleep(0.05); remaining -= 0.05
        return True

    def run(self):
        gui, job = self.gui, self.job
        psu = gui.psu
        prev_v, prev_a = psu.state['set_v'], psu.state['set_a']
        with gui.io_lock:
            prev_on = psu.refresh_output_state()
        outcome = 'finished'
        try:
            steps = job['steps']
            total_cycles = job.get('cycles', 1) if job['kind'] == 'sequence' else 1
            for cycle in range(total_cycles):
                if self.stop_flag.is_set():
                    outcome = 'stopped'; break
                for i, stp in enumerate(steps):
                    v, a = stp[0], stp[1]
                    dwell = stp[2] if len(stp) > 2 else job['dwell']
                    if self.stop_flag.is_set():
                        outcome = 'stopped'; break
                    while self.paused.is_set() and not self.stop_flag.is_set():
                        time.sleep(0.05)
                    if self.stop_flag.is_set():
                        outcome = 'stopped'; break
                    self._apply(v, a)
                    base = job.get('first_index')
                    self.step.emit((base + i) if base is not None else i)
                    self.log.emit(f"cycle {cycle + 1}/{total_cycles} · step {i + 1}/{len(steps)}: {v:.2f} V / {a:.2f} A")
                    if job.get('manual'):
                        self.advance.clear()
                        while not self.advance.wait(0.1):
                            if self.stop_flag.is_set():
                                break
                    else:
                        if not self._dwell(dwell):
                            outcome = 'stopped'; break
                if outcome == 'stopped':
                    break
        except Exception as e:
            outcome = f'error: {e}'
        finally:
            # restore pre-run setpoints + output state, whatever happened
            try:
                with self.gui.io_lock:
                    if prev_v is not None:
                        psu.set_voltage(prev_v)
                    if prev_a is not None:
                        psu.set_current(prev_a)
                    if not prev_on and psu.state['output_on']:
                        psu.output_off()
            except Exception as e:
                outcome += f' (restore failed: {e})'
            self.step.emit(-1)
            self.done.emit(outcome)


def _kv_row(parent_layout, key):
    row = QHBoxLayout()
    k = QLabel(key); k.setStyleSheet('color:#5b7a8a;')
    val = QLabel('—'); val.setStyleSheet('font-weight:bold;')
    row.addWidget(k); row.addWidget(val, 1)
    parent_layout.addLayout(row)
    return val


def _value_box():
    """One dark bordered value box, like the official app's readout boxes."""
    from PySide6.QtWidgets import QFrame
    frame = QFrame()
    frame.setFrameShape(QFrame.StyledPanel)
    frame.setStyleSheet('QFrame { background:#0a1219; border:1px solid #1f6f8b; border-radius:4px; }')
    lay = QVBoxLayout(frame); lay.setContentsMargins(4, 8, 4, 8)
    lab = QLabel('—'); lab.setAlignment(Qt.AlignCenter)
    lab.setStyleSheet('font-size:15px; font-weight:bold; border:none;')
    lay.addWidget(lab)
    return frame, lab


def _power_icon(color='white'):
    """Power-symbol icon painted by hand (no font-glyph dependency)."""
    from PySide6.QtGui import QIcon, QPixmap
    pm = QPixmap(64, 64); pm.fill(Qt.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 5, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(17, 19, 30, 30, 140 * 16, 260 * 16)
    p.drawLine(32, 11, 32, 33)
    p.end()
    return QIcon(pm)


def _lamp():
    dot = QLabel()
    dot.setFixedSize(34, 34)
    dot.setAlignment(Qt.AlignCenter)
    return dot


def _set_lamp(dot, on, color):
    dot.setStyleSheet(
        f'background:{color}; border:2px solid {color}; border-radius:17px;' if on else
        'background:#182530; border:2px solid #2b414e; border-radius:17px;')


class MainWindow(QMainWindow):
    def __init__(self, port=None):
        super().__init__()
        self.setWindowTitle('FNIRSI DPS-150 — Power Supply')
        self.setStyleSheet(QSS)
        self.port = port
        self.psu = None
        self.io_lock = threading.Lock()
        self.worker = None
        self.runner = None
        self._bright_guard = False
        self._build_ui()
        self._set_connected(False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_labels)
        self.timer.start(500)
        QTimer.singleShot(300, self.connect_device)

    # ---------------- construction ----------------
    def _build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        v = QVBoxLayout(root)
        top = QHBoxLayout()
        title = QLabel('FNIRSI  DPS-150')
        title.setStyleSheet('font-size:19px; font-weight:bold; color:#38bdf8;')
        self.lbl_status = QLabel('● Disconnected')
        self.lbl_status.setStyleSheet('font-weight:bold; color:#e05252;')
        self.btn_connect = QPushButton('Connect')
        self.btn_connect.clicked.connect(self.toggle_connect)
        top.addWidget(title); top.addWidget(self.lbl_status, 1); top.addWidget(self.btn_connect)
        v.addLayout(top)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_basic(), 'Basic')
        self.tabs.addTab(self._build_advanced(), 'Advanced')
        v.addWidget(self.tabs, 1)
        self.lbl_log = QLabel(' ')
        self.lbl_log.setStyleSheet('color:#7fa6ba;')
        v.addWidget(self.lbl_log)
        self.resize(1240, 780)

    def _build_basic(self):
        page = QWidget()
        cols = QHBoxLayout(page)
        # ---- column 1: chart + gauges ----
        c1 = QVBoxLayout()
        g_chart = QGroupBox('Output trend')
        cv = QVBoxLayout(g_chart)
        bar = QHBoxLayout()
        self.btn_rec = QPushButton('Pause'); self.btn_rec.setCheckable(True)
        self.btn_rec.clicked.connect(self._toggle_rec)
        btn_clear = QPushButton('Clear'); btn_clear.clicked.connect(lambda: self.chart.clear())
        self.chk_cv = QCheckBox('Output Voltage'); self.chk_cv.setChecked(True)
        self.chk_cv.toggled.connect(lambda on: setattr(self.chart, 'show_v', on))
        self.chk_ca = QCheckBox('Output Current'); self.chk_ca.setChecked(True)
        self.chk_ca.toggled.connect(lambda on: setattr(self.chart, 'show_a', on))
        bar.addWidget(self.btn_rec); bar.addWidget(btn_clear)
        bar.addWidget(self.chk_cv); bar.addWidget(self.chk_ca); bar.addStretch(1)
        cv.addLayout(bar)
        self.chart = ChartWidget()
        cv.addWidget(self.chart)
        c1.addWidget(g_chart)
        g_gauge = QGroupBox('Output')
        gv = QHBoxLayout(g_gauge)
        self.gauge_v = GaugeWidget('Voltage', 'V', CYAN)
        self.gauge_a = GaugeWidget('Current', 'A', GREEN)
        gv.addWidget(self.gauge_v); gv.addWidget(self.gauge_a)
        c1.addWidget(g_gauge)
        cols.addLayout(c1, 5)
        # ---- column 2: setpoints, presets, measurement, brightness ----
        c2 = QVBoxLayout()
        g_set = QGroupBox('Setpoints')
        sv = QGridLayout(g_set)
        self.spin_v = QDoubleSpinBox(); self.spin_v.setRange(0, 30); self.spin_v.setDecimals(2)
        self.spin_v.setSingleStep(0.1); self.spin_v.setSuffix(' V')
        self.spin_a = QDoubleSpinBox(); self.spin_a.setRange(0, 5.1); self.spin_a.setDecimals(2)
        self.spin_a.setSingleStep(0.05); self.spin_a.setSuffix(' A')
        self.btn_apply = QPushButton('Apply')
        self.btn_apply.clicked.connect(self.apply_setpoints)
        self.lbl_max = QLabel('')
        sv.addWidget(QLabel('Voltage'), 0, 0); sv.addWidget(self.spin_v, 0, 1)
        sv.addWidget(QLabel('Current'), 1, 0); sv.addWidget(self.spin_a, 1, 1)
        self.btn_lock = QPushButton('Lock')
        self.btn_lock.setCheckable(True)
        self.btn_lock.setToolTip('Lock: disable the Apply button so setpoints cannot be changed')
        self.btn_lock.toggled.connect(self._lock_toggled)
        row_btns = QWidget(); hb = QHBoxLayout(row_btns); hb.setContentsMargins(0, 0, 0, 0); hb.setSpacing(6)
        hb.addWidget(self.btn_apply, 2); hb.addWidget(self.btn_lock, 1)
        sv.addWidget(row_btns, 2, 0, 1, 2); sv.addWidget(self.lbl_max, 3, 0, 1, 2)
        c2.addWidget(g_set); self.grp_set = g_set
        g_pre = QGroupBox('Presets - fixed')
        pv = QGridLayout(g_pre)
        self.PRESETS = [(3.3, 1.0), (3.3, 5.0), (5.0, 1.0), (5.0, 3.0)]
        self.preset_btns = []
        for i, (vv, aa) in enumerate(self.PRESETS):
            b = QPushButton(f'{vv:g} V / {aa:g} A')
            b.clicked.connect(lambda _=False, n=i: self.preset_clicked(n))
            self.preset_btns.append(b); pv.addWidget(b, i // 2, i % 2)
        pv.addWidget(QLabel('Fixed presets - cannot be edited or saved.'), 2, 0, 1, 2)
        c2.addWidget(g_pre); self.grp_pre = g_pre
        g_meas = QGroupBox('Measurement')
        mv = QVBoxLayout(g_meas)
        self.lbl_ah = _kv_row(mv, 'Output capacity')
        self.lbl_wh = _kv_row(mv, 'Output energy')
        self.lbl_metering = _kv_row(mv, 'Metering')
        c2.addWidget(g_meas); self.grp_meas = g_meas
        g_br = QGroupBox('Brightness')
        bv = QHBoxLayout(g_br)
        self.sld_bright = QSlider(Qt.Horizontal); self.sld_bright.setRange(0, 15)
        self.sld_bright.valueChanged.connect(self._bright_slider)
        self.sld_bright.sliderReleased.connect(self._bright_commit)
        self.spin_bright = QSpinBox(); self.spin_bright.setRange(0, 15)
        self.spin_bright.valueChanged.connect(self._bright_spin)
        bv.addWidget(self.sld_bright, 1); bv.addWidget(self.spin_bright)
        c2.addWidget(g_br); self.grp_br = g_br
        c2.addStretch(1)
        cols.addLayout(c2, 3)
        # ---- column 3: device + basic information + output ----
        c3 = QVBoxLayout()
        g_dev = QGroupBox('USB / Device')
        dv = QVBoxLayout(g_dev)
        self.dev_product = _kv_row(dv, 'Product')
        self.dev_fw = _kv_row(dv, 'Firmware')
        self.dev_hw = _kv_row(dv, 'Hardware')
        self.dev_port = _kv_row(dv, 'Port')
        self.dev_baud = _kv_row(dv, 'Baud')
        c3.addWidget(g_dev)
        g_bi = QGroupBox('ⓘ Basic information')
        bv2 = QVBoxLayout(g_bi)
        grid = QGridLayout()
        def hdr(text, row, col):
            l = QLabel(text); l.setAlignment(Qt.AlignCenter)
            l.setStyleSheet('color:#5b7a8a;')
            grid.addWidget(l, row, col)
        hdr('Input Voltage', 0, 0); hdr('Output', 0, 1); hdr('Output', 0, 2)
        hdr('Output Power', 2, 0); hdr('System', 2, 1)
        boxes = {}
        for key, (r, c) in {'input': (1, 0), 'ov': (1, 1), 'oa': (1, 2),
                            'ow': (3, 0), 'temp': (3, 1), 'spare': (3, 2)}.items():
            frame, lab = _value_box()
            grid.addWidget(frame, r, c)
            boxes[key] = lab
        self.bi_box_input, self.bi_box_ov, self.bi_box_oa = boxes['input'], boxes['ov'], boxes['oa']
        self.bi_box_ow, self.bi_box_temp = boxes['ow'], boxes['temp']
        bv2.addLayout(grid)
        lamps = QHBoxLayout()
        def lamp_col(name):
            col = QVBoxLayout()
            dot = _lamp(); col.addWidget(dot, 0, Qt.AlignCenter)
            txt = QLabel(name); txt.setAlignment(Qt.AlignCenter)
            col.addWidget(txt)
            lamps.addLayout(col)
            return dot
        self.lamp_normal = lamp_col('Normal')
        self.lamp_const = lamp_col('Constant')
        self.lamp_const.setToolTip('Constant-voltage mode (CV). Behaviour in constant-current mode is unverified.')
        self.btn_power = QToolButton()
        self.ico_on = _power_icon('#22c55e'); self.ico_off = _power_icon('#ef4444')
        self.btn_power.setIcon(self.ico_off)
        self.btn_power.setIconSize(QSize(38, 38))
        self.btn_power.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.btn_power.setText('Open')
        self.btn_power.setMinimumSize(96, 96)
        self.btn_power.clicked.connect(self.toggle_output)
        lamps.addWidget(self.btn_power)
        bv2.addLayout(lamps)
        c3.addWidget(g_bi)
        c3.addStretch(1)
        cols.addLayout(c3, 3)
        return page

    def _build_advanced(self):
        page = QWidget()
        cols = QHBoxLayout(page)
        # ---- sequence ----
        g_seq = QGroupBox('Sequence output')
        q = QVBoxLayout(g_seq)
        self.tbl_seq = QTableWidget(10, 5)
        self.tbl_seq.setHorizontalHeaderLabels(['#', 'V-SET (V)', 'I-SET (A)', 'Delay (s)', 'Status'])
        self.tbl_seq.verticalHeader().setVisible(False)
        for r in range(10):
            self.tbl_seq.setItem(r, 0, QTableWidgetItem(str(r + 1)))
            for c in range(1, 5):
                self.tbl_seq.setItem(r, c, QTableWidgetItem(''))
            self.tbl_seq.item(r, 0).setFlags(Qt.ItemIsEnabled)
        for r, vv in enumerate((1.0, 2.0, 3.0, 4.0, 5.0, 6.0)):
            self.tbl_seq.item(r, 1).setText(f'{vv:.2f}')
            self.tbl_seq.item(r, 2).setText('1.00')
            self.tbl_seq.item(r, 3).setText('1')
        self.tbl_seq.setColumnWidth(0, 36)
        q.addWidget(self.tbl_seq)
        rowbtn = QHBoxLayout()
        b_add = QPushButton('+ Row'); b_add.clicked.connect(self._seq_add_row)
        b_del = QPushButton('− Row'); b_del.clicked.connect(self._seq_del_row)
        rowbtn.addWidget(b_add); rowbtn.addWidget(b_del); rowbtn.addStretch(1)
        q.addLayout(rowbtn)
        grid = QGridLayout()
        self.chk_manual = QCheckBox('Manual mode (step with Continue)')
        self.spin_cycles = QSpinBox(); self.spin_cycles.setRange(1, 999); self.spin_cycles.setValue(1)
        self.spin_start_no = QSpinBox(); self.spin_start_no.setRange(1, 99); self.spin_start_no.setValue(1)
        self.spin_stop_no = QSpinBox(); self.spin_stop_no.setRange(1, 99); self.spin_stop_no.setValue(6)
        grid.addWidget(self.chk_manual, 0, 0, 1, 4)
        grid.addWidget(QLabel('Cycles'), 1, 0); grid.addWidget(self.spin_cycles, 1, 1)
        grid.addWidget(QLabel('Start #'), 1, 2); grid.addWidget(self.spin_start_no, 1, 3)
        grid.addWidget(QLabel('Stop #'), 2, 0); grid.addWidget(self.spin_stop_no, 2, 1)
        self.lbl_seq_cur = QLabel('Current step: —')
        self.lbl_seq_cycle = QLabel('Current cycle: —')
        grid.addWidget(self.lbl_seq_cur, 2, 2); grid.addWidget(self.lbl_seq_cycle, 2, 3)
        q.addLayout(grid)
        trans = QHBoxLayout()
        self.btn_seq_start = QPushButton('Start'); self.btn_seq_start.clicked.connect(self.start_sequence)
        self.btn_seq_pause = QPushButton('Pause'); self.btn_seq_pause.setCheckable(True)
        self.btn_seq_pause.clicked.connect(self._seq_pause)
        self.btn_seq_cont = QPushButton('Continue'); self.btn_seq_cont.clicked.connect(self._seq_continue)
        self.btn_seq_stop = QPushButton('Stop'); self.btn_seq_stop.clicked.connect(self._seq_stop)
        for b in (self.btn_seq_start, self.btn_seq_pause, self.btn_seq_cont, self.btn_seq_stop):
            trans.addWidget(b)
        q.addLayout(trans)
        cols.addWidget(g_seq, 5)
        self.grp_seq = g_seq
        # ---- scans ----
        right = QVBoxLayout()
        def scan_group(title, fixed_label, fixed_unit, btn_slot):
            g = QGroupBox(title)
            gl = QGridLayout(g)
            def dsp(rng, dec, step, val, suffix):
                s = QDoubleSpinBox(); s.setRange(*rng); s.setDecimals(dec)
                s.setSingleStep(step); s.setValue(val); s.setSuffix(suffix)
                return s
            fixed = dsp((0, 30), 3 if fixed_unit == ' A' else 2, 0.1, 1.0, fixed_unit)
            start = dsp((0, 30), 2, 0.5, 1.0, '')
            stop = dsp((0, 30), 2, 0.5, 5.0, '')
            stepw = dsp((0.01, 30), 2, 0.1, 1.0, '')
            delay = dsp((0.5, 3600), 1, 0.5, 1.0, ' s')
            gl.addWidget(QLabel(fixed_label), 0, 0); gl.addWidget(fixed, 0, 1)
            gl.addWidget(QLabel('Start value'), 1, 0); gl.addWidget(start, 1, 1)
            gl.addWidget(QLabel('Stop value'), 2, 0); gl.addWidget(stop, 2, 1)
            gl.addWidget(QLabel('Step value'), 3, 0); gl.addWidget(stepw, 3, 1)
            gl.addWidget(QLabel('Delay'), 4, 0); gl.addWidget(delay, 4, 1)
            btn = QPushButton('Run'); btn.clicked.connect(btn_slot)
            gl.addWidget(btn, 5, 0, 1, 2)
            return g, fixed, start, stop, stepw, delay, btn
        g_vs, self.vs_i, self.vs_from, self.vs_to, self.vs_step, self.vs_delay, self.btn_vs_run = \
            scan_group('Voltage scan', 'Output current', ' A', self.run_vscan)
        g_is, self.is_v, self.is_from, self.is_to, self.is_step, self.is_delay, self.btn_is_run = \
            scan_group('Current scan', 'Output voltage', ' V', self.run_iscan)
        self.is_from.setDecimals(3); self.is_to.setDecimals(3); self.is_step.setDecimals(3)
        self.is_from.setValue(0.1); self.is_to.setValue(0.5); self.is_step.setValue(0.1)
        right.addWidget(g_vs); right.addWidget(g_is); right.addStretch(1)
        cols.addLayout(right, 4)
        self.grp_scans = (g_vs, g_is)
        return page

    # ---------------- helpers ----------------
    def _log(self, msg):
        self.lbl_log.setText(msg)

    def _set_connected(self, on, label=''):
        for g in (self.grp_set, self.grp_pre, self.grp_br, self.grp_seq) + self.grp_scans:
            g.setEnabled(on)
        self.btn_power.setEnabled(on)
        self.btn_connect.setText('Disconnect' if on else 'Connect')
        if on:
            self.lbl_status.setText(f'● Connected — {label}')
            self.lbl_status.setStyleSheet('font-weight:bold; color:#4ade80;')
        else:
            self.lbl_status.setText('● Disconnected')
            self.lbl_status.setStyleSheet('font-weight:bold; color:#e05252;')
            self._paint_output(False)

    def _paint_output(self, on):
        # State display (owner's choice): the text names the current state
        # (On / Off) and the power glyph carries the colour - green while
        # the output is live, red while it is off. The button face stays
        # neutral so the glyph colour reads clearly.
        self.btn_power.setText('On' if on else 'Off')
        self.btn_power.setIcon(self.ico_on if on else self.ico_off)
        self.btn_power.setStyleSheet(
            'QToolButton { background-color:#16202b; color:white; border:1px solid #22c55e;'
            ' border-radius:8px; font-weight:bold; padding:6px; }' if on else
            'QToolButton { background-color:#16202b; color:white; border:1px solid #ef4444;'
            ' border-radius:8px; font-weight:bold; padding:6px; }')

    # ---------------- connection ----------------
    def toggle_connect(self):
        if self.psu:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self):
        if self.psu:
            return
        try:
            psu = FnirsiDPS150(self.port)
            with self.io_lock:
                psu.connect()
        except Exception as e:
            self._log(f'connect failed: {e}')
            return
        self.psu = psu
        import os
        short = os.path.basename(os.path.realpath(psu.port)) if 'by-id' in psu.port else psu.port
        i = psu.identity
        self._set_connected(True, f"{i.get('model')} · FW {i.get('firmware')} · {short}")
        self.dev_product.setText(i.get('model', '—'))
        self.dev_fw.setText(i.get('firmware', '—'))
        self.dev_hw.setText(i.get('hardware', '—'))
        self.dev_port.setText(short)
        self.dev_baud.setText(str(psu.baud))
        self.spin_v.setValue(psu.state['set_v'] or 0.0)
        self.spin_a.setValue(psu.state['set_a'] or 0.0)
        if psu.state['brightness'] is not None:
            self._bright_guard = True
            self.sld_bright.setValue(psu.state['brightness'])
            self.spin_bright.setValue(psu.state['brightness'])
            self._bright_guard = False
        self._paint_output(bool(psu.state['output_on']))
        self.worker = StatusWorker(self)
        self.worker.updated.connect(self._refresh_labels)
        self.worker.failed.connect(lambda m: self._log(f'link problem: {m}'))
        self.worker.start()
        self._log('connected')

    def disconnect_device(self):
        self._stop_runner(wait=True)
        if self.worker:
            self.worker.running = False
            self.worker.wait(2500)
            self.worker = None
        if self.psu:
            try:
                self.psu.close()
            except Exception:
                pass
            self.psu = None
        self._set_connected(False)
        self._log('disconnected (output state left as-is on the device)')

    # ---------------- live refresh ----------------
    def _refresh_labels(self):
        if not self.psu:
            return
        st = self.psu.state
        if st['out_v'] is None:
            return
        self.chart.max_v = st['max_v'] or 12.0
        self.chart.max_a = st['max_a'] or 5.0
        self.chart.push(st['out_v'], st['out_a'])
        self.gauge_v.set(st['out_v'], st['max_v'], st['set_v'])
        self.gauge_a.set(st['out_a'], st['max_a'], st['set_a'])
        self.bi_box_input.setText(f"{st['input_v']:.2f}V")
        self.bi_box_ov.setText(f"{st['out_v']:.2f}V")
        self.bi_box_oa.setText(f"{st['out_a']:.3f}A")
        self.bi_box_ow.setText(f"{st['out_w']:.2f}W")
        self.bi_box_temp.setText(f"{st['temp_c']:.0f}°C")
        prot = st['protection']
        _set_lamp(self.lamp_normal, True, '#ef4444' if prot else '#22c55e')
        self.lamp_normal.setToolTip(
            f"Protection tripped: {PROTECTION_NAMES.get(prot, prot)}" if prot else 'Protection: Normal')
        _set_lamp(self.lamp_const, st['mode'] == 1, '#38bdf8')
        self.lbl_ah.setText(f"{st['ah'] or 0:.3f} Ah")
        self.lbl_wh.setText(f"{st['wh'] or 0:.3f} Wh")
        self.lbl_metering.setText('On' if st['metering'] else ('Off' if st['metering'] == 0 else '—'))
        self.lbl_max.setText(f"device max now: {st['max_v']:.2f} V / {st['max_a']:.2f} A" if st['max_v'] else '')
        self._paint_output(bool(st['output_on']))

    def _toggle_rec(self):
        paused = self.btn_rec.isChecked()
        self.chart.recording = not paused
        self.btn_rec.setText('Resume' if paused else 'Pause')

    # ---------------- basic actions ----------------
    def _lock_toggled(self, locked):
        self.btn_apply.setEnabled(not locked)
        self.btn_lock.setText('Unlock' if locked else 'Lock')
        self.btn_lock.setStyleSheet('background:#7f1d1d;color:#ffffff;font-weight:bold' if locked else '')
        self._log('setpoints LOCKED - Apply disabled' if locked else 'setpoints unlocked - Apply enabled')

    def apply_setpoints(self):
        if not self.psu or self.runner:
            return
        try:
            with self.io_lock:
                self.psu.set_voltage(self.spin_v.value())
                self.psu.set_current(self.spin_a.value())
            self._log(f"setpoints applied: {self.spin_v.value():.2f} V / {self.spin_a.value():.2f} A (read-back verified)")
        except Exception as e:
            self._log(f'apply failed: {e}')

    def toggle_output(self):
        if not self.psu or self.runner:
            return
        try:
            with self.io_lock:
                if self.psu.state['output_on']:
                    self.psu.output_off()
                else:
                    self.psu.output_on()
            self._paint_output(bool(self.psu.state['output_on']))
            self._log('output ON' if self.psu.state['output_on'] else 'output OFF')
        except Exception as e:
            self._log(f'output toggle failed: {e}')

    def preset_clicked(self, idx):
        if not self.psu or self.runner:
            return
        vv, aa = self.PRESETS[idx]
        warn = QMessageBox(self)
        warn.setIcon(QMessageBox.Warning)
        warn.setWindowTitle('Apply preset?')
        live = ('The output is ON - the new setpoints take effect immediately.'
                if self.psu.state['output_on'] else
                'The output is off - the setpoints change now and apply when you turn it on.')
        warn.setText(f'Apply preset {vv:g} V / {aa:g} A?')
        warn.setInformativeText(live)
        apply_btn = warn.addButton('Apply', QMessageBox.YesRole)
        cancel_btn = warn.addButton('Cancel', QMessageBox.NoRole)
        warn.setDefaultButton(cancel_btn)
        warn.exec()
        if warn.clickedButton() is not apply_btn:
            self._log(f'preset {vv:g} V / {aa:g} A cancelled')
            return
        try:
            with self.io_lock:
                self.psu.set_voltage(vv)
                self.psu.set_current(aa)
            self.spin_v.setValue(vv); self.spin_a.setValue(aa)
            self._log(f'preset applied: {vv:g} V / {aa:g} A (read-back verified)')
        except Exception as e:
            self._log(f'preset failed: {e}')

    # ---------------- brightness ----------------
    def _bright_slider(self, v):
        if self._bright_guard:
            return
        self._bright_guard = True; self.spin_bright.setValue(v); self._bright_guard = False

    def _bright_spin(self, v):
        if self._bright_guard:
            return
        self._bright_guard = True; self.sld_bright.setValue(v); self._bright_guard = False
        self._bright_commit()

    def _bright_commit(self):
        if not self.psu or self.runner:
            return
        v = self.spin_bright.value()
        try:
            with self.io_lock:
                self.psu.set_brightness(v)
                self.psu.read_all()
            got = self.psu.state['brightness']
            self._log(f'brightness set to {v}' + ('' if got == v else f' (device reports {got})'))
        except Exception as e:
            self._log(f'brightness failed: {e}')

    # ---------------- runner management ----------------
    def _start_runner(self, job):
        if not self.psu:
            return
        if self.runner:
            self._log('a run is already active'); return
        r = Runner(self, job)
        r.log.connect(self._on_runner_log)
        r.step.connect(self._on_runner_step)
        r.done.connect(self._on_runner_done)
        self.runner = r
        self._refresh_run_buttons()
        r.start()
        self._log(f"{job['kind']} started ({len(job['steps'])} steps)")

    def _stop_runner(self, wait=False):
        if self.runner:
            self.runner.request_stop()
            if wait:
                self.runner.wait(6000)

    def _on_runner_log(self, msg):
        self._log(msg)
        if msg.startswith('cycle '):
            try:
                cyc = msg.split('·')[0].replace('cycle', '').strip()
                self.lbl_seq_cycle.setText(f'Current cycle: {cyc}')
            except Exception:
                pass

    def _on_runner_step(self, idx):
        if self.runner and self.runner.job['kind'] == 'sequence' and idx >= 0:
            for r in range(self.tbl_seq.rowCount()):
                item = self.tbl_seq.item(r, 4)
                if item and item.text() == 'Running':
                    item.setText('Done')
            if idx < self.tbl_seq.rowCount():
                self.tbl_seq.item(idx, 4).setText('Running')
                self.tbl_seq.selectRow(idx)
            self.lbl_seq_cur.setText(f'Current step: {idx + 1}')

    def _on_runner_done(self, outcome):
        kind = self.runner.job['kind'] if self.runner else '?'
        self.runner = None
        for r in range(self.tbl_seq.rowCount()):
            item = self.tbl_seq.item(r, 4)
            if item and item.text() == 'Running':
                item.setText('Done' if outcome == 'finished' else outcome.split(':')[0].capitalize())
        self.btn_seq_pause.setChecked(False)
        self._refresh_run_buttons()
        self._log(f'{kind} {outcome} — setpoints/output restored to pre-run state')
        self.lbl_seq_cur.setText('Current step: —')

    def _refresh_run_buttons(self):
        active = self.runner is not None
        kind = self.runner.job['kind'] if active else None
        self.btn_vs_run.setText('Stop' if kind == 'vscan' else 'Run')
        self.btn_is_run.setText('Stop' if kind == 'iscan' else 'Run')
        self.btn_vs_run.setEnabled(not active or kind == 'vscan')
        self.btn_is_run.setEnabled(not active or kind == 'iscan')
        self.btn_seq_start.setEnabled(not active)
        self.btn_seq_stop.setEnabled(active and kind == 'sequence')
        self.btn_seq_pause.setEnabled(active and kind == 'sequence')
        self.btn_seq_cont.setEnabled(active and kind == 'sequence')
        for g in (self.grp_set, self.grp_pre, self.grp_br):
            g.setEnabled(not active and self.psu is not None)
        self.btn_power.setEnabled(not active and self.psu is not None)

    # ---------------- scans ----------------
    @staticmethod
    def _expand(start, stop, step):
        vals = []
        direction = 1 if stop >= start else -1
        x = start
        while (x - stop) * direction <= 1e-9 and len(vals) < 200:
            vals.append(round(x, 4))
            x += direction * abs(step)
        if not vals or abs(vals[-1] - stop) > 1e-6:
            vals.append(stop)
        return vals

    def run_vscan(self):
        if self.runner:
            if self.runner.job['kind'] == 'vscan':
                self._stop_runner()
            return
        steps = [(v, self.vs_i.value()) for v in self._expand(self.vs_from.value(), self.vs_to.value(), self.vs_step.value())]
        self._start_runner({'kind': 'vscan', 'steps': steps, 'dwell': self.vs_delay.value()})

    def run_iscan(self):
        if self.runner:
            if self.runner.job['kind'] == 'iscan':
                self._stop_runner()
            return
        steps = [(self.is_v.value(), a) for a in self._expand(self.is_from.value(), self.is_to.value(), self.is_step.value())]
        self._start_runner({'kind': 'iscan', 'steps': steps, 'dwell': self.is_delay.value()})

    # ---------------- sequence ----------------
    def _seq_add_row(self):
        r = self.tbl_seq.rowCount()
        self.tbl_seq.insertRow(r)
        self.tbl_seq.setItem(r, 0, QTableWidgetItem(str(r + 1)))
        self.tbl_seq.item(r, 0).setFlags(Qt.ItemIsEnabled)
        for c in range(1, 5):
            self.tbl_seq.setItem(r, c, QTableWidgetItem(''))

    def _seq_del_row(self):
        r = self.tbl_seq.rowCount()
        if r > 1:
            self.tbl_seq.removeRow(r - 1)

    def start_sequence(self):
        if self.runner or not self.psu:
            return
        lo = self.spin_start_no.value() - 1
        hi = self.spin_stop_no.value() - 1
        if hi < lo or hi >= self.tbl_seq.rowCount():
            self._log('sequence: check start/stop numbers'); return
        steps = []
        for r in range(lo, hi + 1):
            def cell(c):
                t = self.tbl_seq.item(r, c).text().strip()
                return float(t) if t else None
            try:
                vv, aa, dd = cell(1), cell(2), cell(3)
            except ValueError:
                self._log(f'sequence: row {r + 1} has a non-numeric value'); return
            if vv is None or aa is None:
                self._log(f'sequence: row {r + 1} is missing V or I'); return
            steps.append((vv, aa, dd if dd is not None else 1.0))
            self.tbl_seq.item(r, 4).setText('')
        self._start_runner({'kind': 'sequence', 'steps': steps, 'dwell': 1.0,
                            'cycles': self.spin_cycles.value(),
                            'manual': self.chk_manual.isChecked(),
                            'first_index': lo})

    def _seq_pause(self):
        if self.runner:
            self.runner.set_paused(self.btn_seq_pause.isChecked())
            self._log('sequence paused' if self.btn_seq_pause.isChecked() else 'sequence resumed')

    def _seq_continue(self):
        if self.runner:
            if self.btn_seq_pause.isChecked():
                self.btn_seq_pause.setChecked(False)
                self.runner.set_paused(False)
            self.runner.advance.set()

    def _seq_stop(self):
        self._stop_runner()

    def closeEvent(self, event):
        self._stop_runner(wait=True)
        self.disconnect_device()
        super().closeEvent(event)


def main():
    ap = argparse.ArgumentParser(description='FNIRSI DPS-150 GUI')
    ap.add_argument('--port', default=None, help='serial port (default: auto-detect)')
    args = ap.parse_args()
    app = QApplication(sys.argv)
    win = MainWindow(args.port)
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
