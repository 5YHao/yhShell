import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Dependency-free deterministic scheduling and decoration-allocation regression tests.
let now = 0;
let id = 0;
const frames = new Map();
const timers = new Map();
const context = vm.createContext({
  window: {}, performance: { now: () => now },
  requestAnimationFrame: callback => { frames.set(++id, callback); return id; },
  cancelAnimationFrame: token => frames.delete(token),
  setTimeout: (callback, delay) => { timers.set(++id, { callback, due: now + delay }); return id; },
  clearTimeout: token => timers.delete(token),
});
vm.runInContext(fs.readFileSync(new URL('../web/js/terminal-highlight.js', import.meta.url), 'utf8'), context);
const { Highlighter } = context.window.TerminalHighlight;
const frame = (time = now + 16) => {
  now = time;
  for (const [token, timer] of [...timers]) {
    if (timer.due <= now) { timers.delete(token); timer.callback(); }
  }
  const pending = [...frames.values()];
  frames.clear();
  pending.forEach(callback => callback(now));
};

function fixture() {
  const handlers = {};
  const state = { visible: true, reads: 0, allocations: 0, disposed: 0, ansi: false, text: 'ERROR warning', settings: { theme: 'dark' } };
  const on = name => callback => { handlers[name] = callback; return { dispose() { delete handlers[name]; } }; };
  const line = {
    isWrapped: false,
    getCell(x) {
      const chars = state.text[x] || ' ';
      return { getChars: () => chars, getWidth: () => 1, isFgDefault: () => !state.ansi,
        isBgDefault: () => true, isInverse: () => false, isInvisible: () => false };
    },
  };
  const buffer = { type: 'normal', viewportY: 0, length: 1, baseY: 0, cursorY: 0,
    getLine() { state.reads++; return line; } };
  const term = {
    rows: 1, cols: 30,
    element: { getBoundingClientRect: () => ({ width: state.visible ? 300 : 0, height: state.visible ? 100 : 0 }) },
    buffer: { active: buffer, onBufferChange: on('buffer') },
    onWriteParsed: on('write'), onScroll: on('scroll'), onResize: on('resize'),
    registerMarker(offset) { return { line: buffer.baseY + buffer.cursorY + offset, isDisposed: false, dispose() { this.isDisposed = true; } }; },
    registerDecoration(options) {
      state.allocations++;
      return { options, isDisposed: false, dispose() { if (!this.isDisposed) state.disposed++; this.isDisposed = true; } };
    },
  };
  const highlighter = new Highlighter(term, () => state.settings);
  return { highlighter, term, state, handlers, buffer };
}

{
  const { highlighter, state, handlers, term, buffer } = fixture();
  frame();
  assert.equal(highlighter.marks.length, 2);
  const original = [...highlighter.marks];
  for (let i = 0; i < 1000; i++) handlers.write();
  assert.equal(frames.size + timers.size, 1, 'one trailing update per burst');
  frame(now + 16);
  assert.equal(state.allocations, 2);
  frame(now + 16);
  assert.equal(state.allocations, 2, 'unchanged writes reuse decorations');
  assert.equal(highlighter.marks[0], original[0]);
  state.text += ' plain changed';
  highlighter.refresh(); frame();
  assert.equal(state.allocations, 2, 'unrelated content changes retain matching spans');
  state.settings.theme = 'light';
  highlighter.refresh(); frame();
  assert.equal(state.allocations, 4, 'palette changes replace spans');
  assert.equal(state.disposed, 2);
  state.settings.terminalHighlightRules = { warning: false };
  highlighter.refresh(); frame();
  assert.equal(highlighter.marks.length, 1);
  assert.equal(state.allocations, 4, 'unchanged rule results retain matching spans');

  state.visible = false;
  const reads = state.reads;
  state.text = 'success';
  for (let i = 0; i < 100; i++) { handlers.write(); frame(); }
  assert.equal(state.reads, reads, 'hidden terminals never scan their buffers');
  assert.equal(state.allocations, 4);
  state.visible = true;
  highlighter.refresh(); frame();
  assert.equal(highlighter.marks.length, 1);
  assert.equal(highlighter.marks[0].decoration.options.foregroundColor, '#167443');
  state.ansi = true;
  highlighter.refresh(); frame();
  assert.equal(highlighter.marks.length, 0, 'ANSI styles remove previously plain highlights');
  state.ansi = false;
  highlighter.refresh(); frame();
  highlighter.marks[0].marker.isDisposed = true;
  const allocations = state.allocations;
  highlighter.refresh(); frame();
  assert.equal(state.allocations, allocations + 1, 'disposed scrollback markers cannot be reused');
  // A surviving marker shifted by scrollback trimming must match its new row.
  highlighter.marks[0].marker.line = -1;
  highlighter.refresh(); frame();
  assert.equal(state.allocations, allocations + 2);
  buffer.type = 'alternate';
  handlers.buffer(); frame();
  assert.equal(highlighter.marks.length, 0);
  buffer.type = 'normal';
  handlers.buffer(); frame();
  assert.equal(highlighter.marks.length, 1);
  state.settings.terminalHighlight = false;
  highlighter.refresh(); frame();
  assert.equal(highlighter.marks.length, 0);
  state.settings.terminalHighlight = true;
  highlighter.refresh(); frame();
  term.element = null;
  highlighter.refresh(); frame();
  assert.equal(highlighter.marks.length, 1, 'unopened terminals are skipped');
  handlers.write();
  assert.equal(timers.size, 1);
  highlighter.dispose();
  assert.equal(frames.size + timers.size, 0, 'dispose cancels pending timers and frames');
  assert.equal(highlighter.marks.length, 0);
  assert.equal(Object.keys(handlers).length, 0);
  highlighter.refresh();
  assert.equal(frames.size + timers.size, 0);
}

{
  const { highlighter, handlers, state } = fixture();
  let renders = 0;
  const render = highlighter.render.bind(highlighter);
  highlighter.render = () => { renders++; render(); };
  frame(0);
  for (let time = 1; time <= 1000; time++) {
    handlers.write(); handlers.scroll(); handlers.resize();
    frame(time);
  }
  assert.ok(renders <= 32, `automatic work bounded near 30Hz, saw ${renders}`);
  assert.equal(state.allocations, 2, 'continuous output does not churn unchanged marks');
  highlighter.refresh(); frame(now + 1);
  assert.ok(renders >= 2, 'explicit refresh bypasses the automatic throttle');
  highlighter.dispose();
}

{
  const { highlighter } = fixture();
  assert.equal(frames.size, 1);
  highlighter.dispose();
  assert.equal(frames.size, 0, 'dispose cancels the initial animation frame');
}
console.log('PASS: hidden skip, visibility resume, unchanged mark reuse, settings/ANSI/buffer changes, marker liveness, bounded scheduling, cleanup');
