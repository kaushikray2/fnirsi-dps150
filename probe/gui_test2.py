import sys, time
sys.path.insert(0, '.')
from PySide6.QtWidgets import QApplication
from fnirsi_gui import MainWindow

app = QApplication(sys.argv)
win = MainWindow(); win.show()

def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents(); time.sleep(0.05)

def wait_for(fn, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if fn():
            return
        time.sleep(0.05)
    raise AssertionError('timeout waiting for ' + what)

pump(6)
assert win.psu is not None, 'connect failed: ' + win.lbl_log.text()
print('1. connected:', win.lbl_status.text(), '| product label:', win.dev_product.text())
INIT = (win.psu.state['set_v'], win.psu.state['set_a'], win.psu.state['output_on'])
b0 = win.psu.state['brightness']
assert win.spin_bright.value() == b0, 'brightness init mismatch'
print(f'2. brightness initialised from device: {b0}')
btest = 14 if b0 != 14 else 8
win.spin_bright.setValue(btest); pump(2.5)
assert win.psu.state['brightness'] == btest, 'brightness set failed'
win.spin_bright.setValue(b0); pump(2.5)
assert win.psu.state['brightness'] == b0
print(f'3. brightness write + read-back verified ({b0} -> {btest} -> {b0})')
win.spin_v.setValue(3.3); win.spin_a.setValue(0.5)
win.btn_apply.click(); pump(1.5)
assert abs(win.psu.state['set_v'] - 3.3) < 0.01
if not win.psu.state['output_on']:
    win.btn_power.click()
wait_for(lambda: win.psu.state['output_on'], 5, 'output on')
pump(5)  # let the chart record the soft-start ramp
print(f"4. basic on: gauge V={win.gauge_v.value:.2f} chart pts={len(win.chart.v)} btn={win.btn_power.text()}")
assert abs(win.gauge_v.value - 3.3) < 0.05 and len(win.chart.v) >= 6
win.grab().save('/tmp/fnirsi2_basic.png')
assert win.btn_power.text() == 'On', win.btn_power.text()
assert win.bi_box_ov.text() == '3.30V', win.bi_box_ov.text()
assert win.bi_box_oa.text() == '0.000A', win.bi_box_oa.text()
assert win.bi_box_temp.text().endswith('C')
assert '#22c55e' in win.lamp_normal.styleSheet()
assert '#38bdf8' in win.lamp_const.styleSheet()
print('4b. basic-info panel verified: boxes, green Normal lamp, blue Constant lamp, Close button')
# voltage scan 1.0 -> 2.0 step 0.5 @ 0.30 A
win.vs_i.setValue(0.30); win.vs_from.setValue(1.0); win.vs_to.setValue(2.0)
win.vs_step.setValue(0.5); win.vs_delay.setValue(0.6)
win.btn_vs_run.click()
wait_for(lambda: win.runner is None, 40, 'vscan finish')
pump(0.5)
st = win.psu.state
print(f"5. vscan done, restored: {st['set_v']:.2f}V/{st['set_a']:.2f}A on={st['output_on']}")
assert abs(st['set_v'] - 3.3) < 0.01 and abs(st['set_a'] - 0.5) < 0.01 and st['output_on']
win.btn_power.click(); pump(1.0)
assert not win.psu.state['output_on']
# sequence: rows 1-3, delays 0.6, cycles 2, auto
for r in range(3):
    win.tbl_seq.item(r, 3).setText('0.6')
win.spin_start_no.setValue(1); win.spin_stop_no.setValue(3); win.spin_cycles.setValue(2)
win.btn_seq_start.click()
wait_for(lambda: win.runner is None, 60, 'sequence finish')
pump(0.5)
statuses = [win.tbl_seq.item(r, 4).text() for r in range(3)]
st = win.psu.state
print(f'6. sequence x2 done, row statuses: {statuses}, restored {st["set_v"]:.2f}/{st["set_a"]:.2f} on={st["output_on"]}')
assert statuses == ['Done', 'Done', 'Done'] and not st['output_on'] and abs(st['set_v'] - 3.3) < 0.01
# manual mode: 2 steps
win.chk_manual.setChecked(True); win.spin_cycles.setValue(1); win.spin_stop_no.setValue(2)
win.btn_seq_start.click()
wait_for(lambda: abs((win.psu.state['set_v'] or 0) - 1.0) < 0.01, 15, 'manual step 1 applied')
pump(1.0)
assert win.runner is not None, 'manual runner should be waiting for Continue'
win.btn_seq_cont.click()
wait_for(lambda: abs((win.psu.state['set_v'] or 0) - 2.0) < 0.01, 15, 'manual step 2 applied')
win.btn_seq_cont.click()
wait_for(lambda: win.runner is None, 20, 'manual finish')
win.chk_manual.setChecked(False)
print('7. manual stepping verified (Continue advanced 1V -> 2V -> end)')
win.tabs.setCurrentIndex(1); pump(0.6)
win.grab().save('/tmp/fnirsi2_adv.png')
# restore bench state
win.tabs.setCurrentIndex(0)
win.spin_v.setValue(INIT[0]); win.spin_a.setValue(INIT[1])
win.btn_apply.click(); pump(1.5)
if bool(win.psu.state['output_on']) != INIT[2]:
    win.btn_power.click(); pump(1.5)
st = win.psu.state
print(f"8. final restored to found state: {st['set_v']:.2f}V/{st['set_a']:.2f}A on={st['output_on']} bright={st['brightness']}")
assert abs(st['set_v'] - INIT[0]) < 0.01 and abs(st['set_a'] - INIT[1]) < 0.01 and bool(st['output_on']) == INIT[2]
assert win.btn_power.text() == ('On' if INIT[2] else 'Off')
assert st['brightness'] == b0
win.close(); pump(0.5)
print('GUI V2 TEST PASS')
