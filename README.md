# FNIRSI DPS-150 — USB control library + GUI

Python control for the FNIRSI DPS-150 programmable DC supply over its USB
virtual COM port. Protocol reverse-engineered from open-source
implementations and **verified end-to-end on a physical unit
(model `DPS-150`, HW V1.0, FW V1.2)**. Works on Linux and Windows.

## ⚠ Warning — use at your own risk

This software drives a real power supply that can deliver up to
30 V / 5 A / 150 W. A wrong setpoint, a bug, or a misunderstanding can
destroy connected equipment, start a fire, or injure you. Check the
setpoints before you connect a load, and never leave a powered setup
unattended.

**This software is provided as-is, with no warranty of any kind. You use
it entirely at your own risk, and the author takes no responsibility for
any damage, loss, or injury of any kind resulting from its use.**

## Files
- `fnirsi.py` — control library + CLI (`FnirsiDPS150`).
- `fnirsi_gui.py` — PySide6 desktop GUI built on the library. Dark
  instrument theme modelled on the
  official FNIRSI app's layout. **Basic tab:** live dual-axis V/I trend chart
  (from the 2 Hz stream), arc gauges vs the live ceiling with setpoint ticks,
  setpoints with a Lock button that disables Apply, four fixed presets (3.3 V/1 A, 3.3 V/5 A, 5 V/1 A, 5 V/3 A) behind a warning confirmation, Ah/Wh measurement + metering
  state, brightness slider, range 0-15 (written to the device and read-back-verified via
  the dump @96), a device panel, and a Basic information panel matching the official app:
  boxed readouts (input V, output V/A, power, system temp), green Normal and
  blue Constant lamps, and a square power button that shows the output
  state - **On** with a green power symbol while the output is live,
  **Off** with a red one while it is off.
  **Advanced tab:** voltage scan and current scan (fixed other-axis value,
  start/stop/step/delay) and a sequence table (per-step V/I/delay, row range,
  cycles, Auto or Manual mode stepped with Continue, Pause/Stop, per-row
  status). Runs execute on a worker thread; on finish, stop or error they
  restore the setpoints and output state from before the run.
- `probe/gui_test2.py` — the headless GUI regression suite (connect,
  brightness, scans, sequences, info panel) run under `xvfb-run`.

## Quick start
```bash
python3 fnirsi.py info        # identity, presets, limits, state
python3 fnirsi.py status
python3 fnirsi.py set 3.3 0.5 # setpoints (read-back verified)
python3 fnirsi.py on          # enable output
python3 fnirsi.py off
python3 fnirsi_gui.py         # GUI (auto-connects; never auto-enables output)
```
Windows: double-click `run_gui.bat` (installs the dependencies from
`requirements.txt` and launches the GUI), or do the same commands with
`python` instead of `python3`. The supply is found automatically by its
USB ID, whichever COM port Windows assigns it.
Library:
```python
from fnirsi import FnirsiDPS150
with FnirsiDPS150() as psu:            # auto-detects the port
    print(psu.snapshot())
    psu.set_voltage(3.3); psu.set_current(0.5)
    psu.output_on()
```

## Protocol (verified facts)
- USB CDC, **115200 8N1** (IPS3608 sibling: 9600). Frame `F1|Cmd|Reg|Len|Data|CS`,
  response header `F0`. **Checksum = (Reg + Len + sum(Data)) & 0xFF** — header and
  command bytes are excluded. Numbers are float32 little-endian.
- Cmd: `0xA1` read, `0xB1` write, `0xC1` connect/disconnect (reg 0x00, data 01/00).
- After CONNECT the device **streams telemetry at 2 Hz**: 0xC0 input V, 0xC3 out
  V/A/W, 0xC4 temp, 0xE2 max V, 0xE3 max A. The library parses this stream on a
  reader thread and matches command responses by register.
- Set V = write float to **0xC1**, set A = **0xC2** (NOT the 0xC0/0xDE pair in
  alexgavs' early files — 0xC0 is input voltage, 0xDE is the model string).
  Float writes are not echoed; the library verifies them by reading the 0xFF
  dump back (setpoints at offsets 4/8). Output = byte write to 0xDB (echoed).
- Full register map is in the `fnirsi.py` docstring (presets 0xC5-0xD0,
  protection limits 0xD1-0xD5, brightness 0xD6, Ah 0xD9, Wh 0xDA,
  protection 0xDC, mode 0xDD, model 0xDE, firmware 0xE0, dump 0xFF).

## License

Beerware — see [LICENSE](LICENSE). Use this for anything you want,
free of charge; the only condition is keeping the license notice. If we
ever meet and you found this useful, you can buy me a beer in return.
