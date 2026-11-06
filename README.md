# FNIRSI DPS-150 — USB control library + GUI

Python control for the FNIRSI DPS-150 programmable DC supply over its USB
virtual COM port. Written for the bench unit on the zorinbox
(model `DPS-150`, HW V1.0, FW V1.2). Protocol reverse-engineered from three
open-source implementations and **verified end-to-end on the physical unit
on 2026-11-06** (see `probe/`).

## Files
- `fnirsi.py` — control library + CLI (`FnirsiDPS150`).
- `fnirsi_gui.py` — PySide6 desktop GUI v2 built on the library (v1 kept as
  `fnirsi_gui.py.bak-2026-11-06`). Dark instrument theme modelled on the
  official FNIRSI app's layout. **Basic tab:** live dual-axis V/I trend chart
  (from the 2 Hz stream), arc gauges vs the live ceiling with setpoint ticks,
  setpoints, preset Groups 1-6 (+ store mode), Ah/Wh measurement + metering
  state, brightness slider (written to the device and read-back-verified via
  the dump @96), a device panel, and a Basic information panel matching the official app:
  boxed readouts (input V, output V/A, power, system temp), green Normal and
  blue Constant lamps, and a square power button that names its action -
  green **Open** while the output is off, red **Close** while it is live.
  **Advanced tab:** voltage scan and current scan (fixed other-axis value,
  start/stop/step/delay) and a sequence table (per-step V/I/delay, row range,
  cycles, Auto or Manual mode stepped with Continue, Pause/Stop, per-row
  status). Runs execute on a worker thread; on finish, stop or error they
  restore the setpoints and output state from before the run.
- `probe/` — the verification probes/tests used to prove the protocol
  (`probe1.py` register survey, `probe2.py` streaming + ramp characterisation,
  `test_library.py`, `gui_test.py`).
- `fnirsi.py.bak-2026-11-06` — the earlier IPS3608-based attempt (9600 baud,
  36 V/8.2 A limits) kept for reference; do not use it for the DPS-150.

## Quick start
```bash
python3 fnirsi.py info        # identity, presets, limits, state
python3 fnirsi.py status
python3 fnirsi.py set 3.3 0.5 # setpoints (read-back verified)
python3 fnirsi.py on          # enable output
python3 fnirsi.py off
python3 fnirsi_gui.py         # GUI (auto-connects; never auto-enables output)
```
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

## Bench behaviour worth knowing
- **Max output voltage follows the input**: fed 11.96 V, the unit reported a max
  of 11.76 V (nameplate 30 V needs a higher-voltage PD input). The library clamps
  setpoints to the live device-reported limits.
- **Soft-start ramp ~2.2 V/s** up and down (3.3 V reached ~1.5 s after ON; the
  October "wrong voltage" readings were this ramp plus the temp register 0xC4
  sitting next to the live register 0xC3).
- Keep >= 50 ms between commands. DISCONNECT gets no reply.
- The GUI disconnecting (or closing) leaves the output state untouched on the
  device — cutting power is always a deliberate button press.
