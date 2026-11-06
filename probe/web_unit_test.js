// Unit test for fnirsi_web.html's protocol code, driven by REAL wire bytes.
// probe/web_unit_capture.bin was captured from a live DPS-150 (session connect,
// a 0xFF dump read, identity reads, ~3.5 s of the 2 Hz telemetry stream).
// probe/web_unit_expected.json holds the state the verified Python library
// (fnirsi.py) decoded from those same bytes. This test extracts the web
// page's own protocol segment, runs it against a device simulator that
// answers from the captured frames, and asserts the decoded state matches.
// Run:  node probe/web_unit_test.js        (exit 0 = PASS)
'use strict';
const fs = require('fs'), os = require('os'), path = require('path'), cp = require('child_process');
const here = __dirname;
const html = fs.readFileSync(path.join(here, '..', 'fnirsi_web.html'), 'utf8');
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];
const seg = js.slice(js.indexOf('const CMD_READ'), js.indexOf('/* ---------- connection lifecycle'));
const body = `
global.document = { getElementById: () => ({ textContent: '', classList: { add(){}, remove(){}, toggle(){} }, style: {} }) };
var __samples = 0;
var pushSample = function() { __samples++; };
var refreshLive = function() {};
` + seg + `
const fs2 = require('fs');
const cap = fs2.readFileSync(${JSON.stringify(path.join(here, 'web_unit_capture.bin'))});
const exp = JSON.parse(fs2.readFileSync(${JSON.stringify(path.join(here, 'web_unit_expected.json'))}, 'utf8'));
log = function(m) {};
const catalog = new Map();
const __orig = onFrame;
onFrame = function(reg, data) { if (!catalog.has(reg)) catalog.set(reg, []); catalog.get(reg).push(Uint8Array.from(data)); __orig(reg, data); };
parseLoop(new Uint8Array(cap));
onFrame = __orig;
S.buf = new Uint8Array(0); __samples = 0;
Object.assign(S.st, { input_v:0, set_v:0, set_a:0, out_v:0, out_a:0, out_w:0, temp_c:0, ah:0, wh:0, metering:0, brightness:0, output_on:false, protection:0, mode:0, max_v:30, max_a:5.1 });
function respFrame(reg, data) { const out = [0xF0, 0xA1, reg, data.length, ...data]; let cs = (reg + data.length) & 0xFF; for (const b of data) cs = (cs + b) & 0xFF; out.push(cs); return new Uint8Array(out); }
function feedCycle() { for (const r of [0xC3, 0xC0, 0xE2, 0xE3, 0xC4]) { const p = catalog.get(r); if (p) parseLoop(respFrame(r, p[p.length - 1])); } }
S.writer = { write: async (bytes) => { await sleep(5);
  if (bytes[1] === 0xA1) { const reg = bytes[2]; const p = catalog.get(reg); if (p) setTimeout(() => parseLoop(respFrame(reg, p[0])), 20); }
} };
const timer = setInterval(feedCycle, 400);
const near = (a, b, tol) => Math.abs(a - b) <= tol;
let fails = 0;
const check = (name, ok) => { console.log((ok ? 'PASS' : 'FAIL'), name); if (!ok) fails++; };
(async () => {
  const model = await readAscii(REG.MODEL);
  check('readAscii model == ' + exp.model, model === exp.model);
  await readAll();
  check('dump set_v ' + S.st.set_v.toFixed(2), near(S.st.set_v, exp.dump.set_v, 0.011));
  check('dump set_a ' + S.st.set_a.toFixed(2), near(S.st.set_a, exp.dump.set_a, 0.011));
  check('dump brightness ' + S.st.brightness, S.st.brightness === exp.dump.brightness);
  check('dump output_on ' + S.st.output_on, S.st.output_on === exp.dump.output_on);
  check('dump mode ' + S.st.mode, S.st.mode === exp.dump.mode);
  check('dump metering ' + S.st.metering, S.st.metering === exp.dump.metering);
  await sleep(1500);
  check('stream input_v ' + S.st.input_v.toFixed(2), near(S.st.input_v, exp.stream_state.input_v, 0.05));
  check('stream out_v ' + S.st.out_v.toFixed(2), near(S.st.out_v, exp.stream_state.out_v, 0.05));
  check('stream out_a ' + S.st.out_a.toFixed(2), near(S.st.out_a, exp.stream_state.out_a, 0.05));
  check('stream temp ' + S.st.temp_c.toFixed(1), near(S.st.temp_c, exp.stream_state.temp_c, 0.6));
  check('chart samples ' + __samples + ' >= 3', __samples >= 3);
  clearInterval(timer);
  console.log(fails === 0 ? 'REAL-CAPTURE UNIT TEST: PASS' : 'REAL-CAPTURE UNIT TEST: FAIL (' + fails + ')');
  process.exit(fails === 0 ? 0 : 1);
})().catch(e => { console.log('ERROR', e.message); process.exit(2); });
`;
const tmp = path.join(os.tmpdir(), 'web_unit_harness_run.js');
fs.writeFileSync(tmp, body);
const r = cp.spawnSync(process.execPath, [tmp], { stdio: 'inherit' });
process.exit(r.status === null ? 2 : r.status);
