"""FNIRSI DPS-150 programmable DC power supply - USB control library.

Protocol (reverse-engineered; VERIFIED LIVE on the bench unit 2026-11-06,
model "DPS-150", HW V1.0, FW V1.2, fed ~12 V on its DC input):

- USB CDC virtual COM port (Artery AT32 VCP, VID:PID 2e3c:5740),
  115200 baud 8N1. (The sibling IPS3608 uses 9600 baud; same framing.)
- Frame:  F1 | Cmd | Register | Length | Data... | Checksum   (request)
          F0 | Cmd | Register | Length | Data... | Checksum   (response)
- Checksum = (Register + Length + sum(Data bytes)) & 0xFF.
  Header and Cmd are EXCLUDED - including them breaks every command.
- Cmd: 0xA1 READ, 0xB1 WRITE, 0xC1 CONNECT(01)/DISCONNECT(00) on reg 0x00,
       0xB0 baud select (DPS-150, not used here).
- All multi-byte numbers are IEEE-754 float32 little-endian.
- After CONNECT the device STREAMS telemetry bursts at 2 Hz:
  registers 0xC0 (input V), 0xC3 (out V/A/W), 0xC4 (temperature),
  0xE2 (max V), 0xE3 (max A). This library runs a reader thread that
  parses the stream continuously; command responses are matched by
  register from the same stream.
- Float WRITES to 0xC1/0xC2 are NOT echoed - verify them by reading the
  0xFF dump back (setpoints at offsets 4/8). Byte writes to 0xDB ARE
  echoed. DISCONNECT gets no response.
- Output behaviour (measured): soft-start ramp ~2.2 V/s up and down;
  max output voltage is input-dependent (at 11.96 V in, max was 11.76 V),
  so always clamp to the live 0xE2/0xE3 limits, not the 30 V/5 A spec.
- Keep >= 50 ms between commands.

Register map (R=read, W=write):
  0xC0 input voltage R | 0xC1 set voltage W float | 0xC2 set current W float
  0xC3 live out V/A/W R (12 B) | 0xC4 temperature R | 0xC5-0xD0 presets 1-6
  (V/A pairs) | 0xD1-0xD5 OVP/OCP/OPP/OTP/LVP limits W float |
  0xD6 brightness W byte 0-15 (4-bit field; larger writes wrap mod 16) | 0xD7 volume W byte | 0xD8 output-voltage
  metering enable W byte (polarity unverified - unused here) |
  0xD9 Ah R | 0xDA Wh R | 0xDB output state R/W byte | 0xDC protection R
  (0=OK,1=OVP,2=OCP,3=OPP,4=OTP,5=LVP,6=REP) | 0xDD mode R (DPS-150: 1=CV,
  0=CC) | 0xDE model R ASCII | 0xDF hw/serial R ASCII | 0xE0 firmware R
  ASCII | 0xE1 device id R | 0xE2 max V R | 0xE3 max A R | 0xFF all-params
  dump R (139 B; head offsets: 0 inV, 4 setV, 8 setA, 12 outV, 16 outA,
  20 outW, 24 temp, 28..75 preset pairs, 76/80/84/88/92 OVP/OCP/OPP/OTP/LVP)

Sources: cho45/fnirsi-dps-150 (JS), alexgavs/newfnrs controller.py
(decompiled official app), hamsternz/fnirsi_psu (C) - cross-checked in
~/workspace/research_notes/fnirsi-psu-protocol-deep-dive-20261019-1208,
then verified against the physical unit (see probe/ in this repo).
"""
import glob
import struct
import threading
import time

import serial

CMD_READ = 0xA1
CMD_WRITE = 0xB1
CMD_SESSION = 0xC1

REG_INPUT_V = 0xC0
REG_SET_V = 0xC1
REG_SET_I = 0xC2
REG_LIVE = 0xC3
REG_TEMP = 0xC4
REG_PRESET_BASE = 0xC5          # preset n (1..6): V reg = base+2*(n-1), A = +1
REG_OVP, REG_OCP, REG_OPP, REG_OTP, REG_LVP = 0xD1, 0xD2, 0xD3, 0xD4, 0xD5
REG_BRIGHTNESS = 0xD6
REG_VOLUME = 0xD7
REG_METERING = 0xD8            # Ah/Wh metering enable (polarity unverified - unused)
REG_AH = 0xD9
REG_WH = 0xDA
REG_OUTPUT = 0xDB
REG_PROTECTION = 0xDC
REG_MODE = 0xDD
REG_MODEL = 0xDE
REG_HW = 0xDF
REG_FW = 0xE0
REG_DEVID = 0xE1
REG_MAX_V = 0xE2
REG_MAX_A = 0xE3
REG_ALL = 0xFF

SPEC_MAX_V = 30.0               # DPS-150 nameplate; live limit is lower (input-dependent)
SPEC_MAX_A = 5.0
PROTECTION_NAMES = {0: 'OK', 1: 'OVP', 2: 'OCP', 3: 'OPP', 4: 'OTP', 5: 'LVP', 6: 'REP'}
MODE_NAMES = {1: 'CV', 0: 'CC'}  # DPS-150 per cho45


class FnirsiError(Exception):
    pass


def _checksum(reg, data):
    return (reg + len(data) + sum(data)) & 0xFF


def _frame(cmd, reg, data=b''):
    return bytes([0xF1, cmd, reg, len(data)]) + data + bytes([_checksum(reg, data)])


def _f32(data, off=0):
    return struct.unpack_from('<f', data, off)[0]


def find_port():
    """Locate the DPS-150 serial port (cross-platform).

    Primary: pyserial's port list matched on the Artery AT32 VCP USB
    VID:PID (2e3c:5740) - yields a COMx device on Windows, ttyACMx on
    Linux. Fallback (Linux only): /dev/serial/by-id and ttyACM globs.
    """
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.vid == 0x2E3C and p.pid == 0x5740:
            return p.device
    for pat in ('/dev/serial/by-id/*Artery*AT32*', '/dev/serial/by-id/*AT32*',
                '/dev/ttyACM*'):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    raise FnirsiError('no FNIRSI serial port found (looked for Artery AT32 VCP)')


class FnirsiDPS150:
    """Control one FNIRSI DPS-150 over its USB virtual COM port.

    Usage:
        with FnirsiDPS150() as psu:
            print(psu.snapshot())
            psu.set_voltage(3.3); psu.set_current(0.5)
            psu.output_on()
    """

    def __init__(self, port=None, baud=115200):
        self.port = port or find_port()
        self.baud = baud
        self.ser = None
        self._reader = None
        self._stop = threading.Event()
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._waiters = {}            # reg -> [Event, holder dict]
        self._cmd_gap = 0.055         # >=50 ms between commands
        self._last_cmd = 0.0
        # live state, maintained by the reader thread
        self.state = {
            'input_v': None, 'out_v': None, 'out_a': None, 'out_w': None,
            'temp_c': None, 'max_v': None, 'max_a': None,
            'set_v': None, 'set_a': None, 'output_on': None,
            'protection': None, 'mode': None, 'ah': None, 'wh': None,
            'brightness': None, 'volume': None, 'metering': None,
        }
        self.identity = {}
        self.presets = []
        self.limits = {}

    # ---------- connection lifecycle ----------
    def connect(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=0.25)
        time.sleep(0.5)
        self._stop.clear()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self.ser.reset_input_buffer()
        self._raw_write(_frame(CMD_SESSION, 0x00, b'\x01'))
        time.sleep(0.06)
        self._raw_write(_frame(0xB0, 0x00, b'\x05'))  # baud select: index 5 = 115200
        time.sleep(0.5)
        # identity + static-ish values (transact reads)
        self.identity = {
            'model': self._read_ascii(REG_MODEL),
            'hardware': self._read_ascii(REG_HW),
            'firmware': self._read_ascii(REG_FW),
        }
        self.refresh_setpoints()
        self.refresh_output_state()
        return self

    def close(self):
        if self.ser is None:
            return
        try:
            self._raw_write(_frame(CMD_SESSION, 0x00, b'\x00'))
            time.sleep(0.1)
        except Exception:
            pass
        self._stop.set()
        try:
            self.ser.close()
        finally:
            self.ser = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # ---------- low level ----------
    def _raw_write(self, data):
        wait = self._cmd_gap - (time.monotonic() - self._last_cmd)
        if wait > 0:
            time.sleep(wait)
        self.ser.write(data)
        self._last_cmd = time.monotonic()

    def _reader_loop(self):
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(512)
            except Exception:
                break
            if not chunk:
                continue
            self._buf += chunk
            self._drain_frames()

    def _drain_frames(self):
        buf = self._buf
        while True:
            i = buf.find(0xF0)
            if i < 0:
                buf.clear()
                return
            if i > 0:
                del buf[:i]
            if len(buf) < 5:
                return
            ln = buf[3]
            total = 5 + ln
            if len(buf) < total:
                return
            frame = bytes(buf[:total])
            del buf[:total]
            reg, data, cs = frame[2], frame[4:4 + ln], frame[4 + ln]
            if _checksum(reg, data) != cs:
                continue  # misaligned/corrupt - resync on next 0xF0
            self._handle_frame(frame[1], reg, data)

    def _handle_frame(self, cmd, reg, data):
        st = self.state
        try:
            if reg == REG_LIVE and len(data) == 12:
                st['out_v'], st['out_a'], st['out_w'] = _f32(data, 0), _f32(data, 4), _f32(data, 8)
            elif reg == REG_INPUT_V and len(data) == 4:
                st['input_v'] = _f32(data)
            elif reg == REG_TEMP and len(data) == 4:
                st['temp_c'] = _f32(data)
            elif reg == REG_MAX_V and len(data) == 4:
                st['max_v'] = _f32(data)
            elif reg == REG_MAX_A and len(data) == 4:
                st['max_a'] = _f32(data)
            elif reg == REG_OUTPUT and len(data) == 1:
                st['output_on'] = bool(data[0])
        except struct.error:
            pass
        with self._lock:
            w = self._waiters.get(reg)
        if w is not None:
            w[1]['cmd'], w[1]['data'] = cmd, data
            w[0].set()

    def _transact(self, cmd, reg, data=b'', timeout=1.5):
        """Send a command and return the data of the response for `reg`.

        Streamed telemetry frames for the same register also satisfy reads
        (identical content). Returns None on timeout.
        """
        ev = threading.Event()
        holder = {}
        with self._lock:
            self._waiters[reg] = [ev, holder]
        try:
            self._raw_write(_frame(cmd, reg, data))
            if ev.wait(timeout):
                return holder.get('data')
            return None
        finally:
            with self._lock:
                self._waiters.pop(reg, None)

    def _read(self, reg, timeout=1.5):
        data = self._transact(CMD_READ, reg, b'\x00', timeout)
        if data is None:
            raise FnirsiError(f'no response reading register 0x{reg:02X}')
        return data

    def _read_float(self, reg):
        return _f32(self._read(reg))

    def _read_ascii(self, reg):
        return self._read(reg).decode('ascii', errors='replace').strip('\x00').strip()

    # ---------- state reads ----------
    def read_all(self):
        """Read the 0xFF full dump and refresh setpoints/presets/limits."""
        d = self._read(REG_ALL, timeout=2.0)
        if len(d) < 110:
            raise FnirsiError(f'0xFF dump too short: {len(d)} bytes')
        self.state['set_v'] = _f32(d, 4)
        self.state['set_a'] = _f32(d, 8)
        self.presets = [(_f32(d, 28 + 8 * i), _f32(d, 32 + 8 * i)) for i in range(6)]
        self.limits = {'ovp': _f32(d, 76), 'ocp': _f32(d, 80), 'opp': _f32(d, 84),
                       'otp': _f32(d, 88), 'lvp': _f32(d, 92)}
        # tail fields (cho45 map, corroborated by BenchWeave/OLICCA captures):
        # @96 brightness, @97 volume, @98 metering flag, @99 Ah, @103 Wh,
        # @107 output on, @108 protection, @109 mode, @111/@115 live ceilings
        # (the dump ceilings are the firmware-robust source for max V/A).
        if len(d) >= 119:
            st = self.state
            st['brightness'] = d[96]; st['volume'] = d[97]; st['metering'] = d[98]
            st['ah'] = _f32(d, 99); st['wh'] = _f32(d, 103)
            st['output_on'] = bool(d[107]); st['protection'] = d[108]; st['mode'] = d[109]
            st['max_v'] = _f32(d, 111); st['max_a'] = _f32(d, 115)
        return d

    def refresh_setpoints(self):
        self.read_all()
        return self.state['set_v'], self.state['set_a']

    def refresh_output_state(self):
        self.state['output_on'] = bool(self._read(REG_OUTPUT)[0])
        self.state['protection'] = self._read(REG_PROTECTION)[0]
        self.state['mode'] = self._read(REG_MODE)[0]
        self.state['ah'] = self._read_float(REG_AH)
        self.state['wh'] = self._read_float(REG_WH)
        return self.state['output_on']

    def snapshot(self):
        """Current best state: streamed telemetry + on-demand status reads."""
        self.refresh_output_state()
        st = dict(self.state)
        st['mode_name'] = MODE_NAMES.get(st['mode'], f"?{st['mode']}")
        st['protection_name'] = PROTECTION_NAMES.get(st['protection'], f"?{st['protection']}")
        st['identity'] = dict(self.identity)
        return st

    # ---------- control ----------
    def _clamp(self, v, a):
        max_v = self.state['max_v'] or SPEC_MAX_V
        max_a = self.state['max_a'] or SPEC_MAX_A
        if not (0.0 <= v <= min(max_v, SPEC_MAX_V)):
            raise ValueError(f'voltage {v} outside 0..{min(max_v, SPEC_MAX_V):.2f} V (live device limit)')
        if not (0.0 <= a <= min(max_a, SPEC_MAX_A + 0.1)):
            raise ValueError(f'current {a} outside 0..{min(max_a, SPEC_MAX_A):.2f} A (live device limit)')

    def _write_float_verified(self, reg, value, dump_off):
        self._raw_write(_frame(CMD_WRITE, reg, struct.pack('<f', float(value))))
        time.sleep(0.08)
        d = self.read_all()
        got = _f32(d, dump_off)
        if abs(got - value) > 0.011:
            raise FnirsiError(f'write to 0x{reg:02X} did not land: wanted {value}, read back {got}')

    def set_voltage(self, volts):
        self._clamp(volts, 0.0)
        self._write_float_verified(REG_SET_V, volts, 4)
        self.state['set_v'] = float(volts)

    def set_current(self, amps):
        self._clamp(0.0, amps)
        self._write_float_verified(REG_SET_I, amps, 8)
        self.state['set_a'] = float(amps)

    def set_output(self, on):
        data = self._transact(CMD_WRITE, REG_OUTPUT, b'\x01' if on else b'\x00')
        if data is None:
            raise FnirsiError('no echo from output write')
        self.state['output_on'] = bool(data[0])
        return self.state['output_on']

    def output_on(self):
        prot = self._read(REG_PROTECTION)[0]
        if prot != 0:
            raise FnirsiError(f'refusing to enable output: protection active ({PROTECTION_NAMES.get(prot, prot)})')
        return self.set_output(True)

    def output_off(self):
        return self.set_output(False)

    def set_preset(self, n, volts, amps):
        """Store a preset (1..6). Verified by dump read-back."""
        if not 1 <= n <= 6:
            raise ValueError('preset must be 1..6')
        self._clamp(volts, amps)
        vreg = REG_PRESET_BASE + 2 * (n - 1)
        self._write_float_verified(vreg, volts, 28 + 8 * (n - 1))
        self._write_float_verified(vreg + 1, amps, 32 + 8 * (n - 1))
        self.presets[n - 1] = (float(volts), float(amps))

    def set_protection_limits(self, ovp=None, ocp=None, opp=None, otp=None, lvp=None):
        """Write protection limits (volts/amps/watts/degC/volts). Dump-verified."""
        jobs = [(REG_OVP, ovp, 76, 'ovp'), (REG_OCP, ocp, 80, 'ocp'),
                (REG_OPP, opp, 84, 'opp'), (REG_OTP, otp, 88, 'otp'),
                (REG_LVP, lvp, 92, 'lvp')]
        for reg, val, off, key in jobs:
            if val is None:
                continue
            self._write_float_verified(reg, float(val), off)
            self.limits[key] = float(val)

    def set_brightness(self, level):
        """Screen brightness 0..15 (byte write to 0xD6, verified via dump @96).

        The field is 4 bits: writes above 15 wrap modulo 16 (writing 20
        leaves the device at 4). The often-quoted 0..20 range is from the
        IPS3608 documentation and is wrong for the DPS-150 - verified live
        on the bench unit 2026-11-06.
        """
        if not 0 <= int(level) <= 15:
            raise ValueError('brightness must be 0..15')
        self._raw_write(_frame(CMD_WRITE, REG_BRIGHTNESS, bytes([int(level)])))

    # NOTE: 0xD8 is the Ah/Wh metering enable (cho45 method names + Studio Pro
    # agree; one cho45 constant misleadingly calls it a voltage fine-tune).
    # Its polarity is not hardware-verified (probably 1=on), so this library
    # deliberately does not write it - the counters at 0xD9/0xDA read fine.


def _fmt_state(st):
    i = st['identity']
    lines = [
        f"{i.get('model')}  hw {i.get('hardware')}  fw {i.get('firmware')}",
        f"input      : {st['input_v']:.2f} V   (device max now: {st['max_v']:.2f} V / {st['max_a']:.2f} A)",
        f"setpoints  : {st['set_v']:.2f} V  {st['set_a']:.2f} A",
        f"output     : {'ON' if st['output_on'] else 'OFF'}  {st['out_v']:.3f} V  {st['out_a']:.3f} A  {st['out_w']:.3f} W  mode {st['mode_name']}",
        f"temp       : {st['temp_c']:.1f} C   protection: {st['protection_name']}   meter: {st['ah']:.3f} Ah / {st['wh']:.3f} Wh",
    ]
    return '\n'.join(lines)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(prog='fnirsi', description='FNIRSI DPS-150 control')
    p.add_argument('--port', default=None)
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('info')
    sub.add_parser('status')
    s = sub.add_parser('set'); s.add_argument('volts', type=float); s.add_argument('amps', type=float)
    sub.add_parser('on'); sub.add_parser('off')
    sub.add_parser('monitor')
    args = p.parse_args()
    with FnirsiDPS150(args.port) as psu:
        if args.cmd == 'info':
            print(psu.identity)
            print('presets:', psu.presets)
            print('limits :', psu.limits)
            print(_fmt_state(psu.snapshot()))
        elif args.cmd == 'status':
            print(_fmt_state(psu.snapshot()))
        elif args.cmd == 'set':
            psu.set_voltage(args.volts); psu.set_current(args.amps)
            print(f'setpoints now {psu.state["set_v"]:.2f} V / {psu.state["set_a"]:.2f} A (read-back verified)')
        elif args.cmd == 'on':
            psu.output_on(); print('output ON')
        elif args.cmd == 'off':
            psu.output_off(); print('output OFF')
        elif args.cmd == 'monitor':
            try:
                while True:
                    st = psu.state
                    print(f"{st['out_v']:7.3f} V  {st['out_a']:6.3f} A  {st['out_w']:7.3f} W  "
                          f"in {st['input_v']:.2f} V  {st['temp_c']:.0f} C", end='\r', flush=True)
                    time.sleep(0.5)
            except KeyboardInterrupt:
                print()
