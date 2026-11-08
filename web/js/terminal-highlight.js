/* Display-only highlighting: never rewrite the terminal's byte stream. */
(() => {
  'use strict';
  const RULES = [
    ['error', /\b(?:error|fatal|panic|failed|failure|denied|unhealthy|CrashLoopBackOff|ImagePullBackOff|ErrImagePull|OOMKilled|NotReady)\b|错误|失败|异常|拒绝/gi],
    ['warning', /\b(?:warn(?:ing)?|deprecated|pending|terminating|unknown|timeout|timed\s+out)\b|警告|超时/gi],
    ['success', /\b(?:success(?:ful(?:ly)?)?|succeeded|passed|healthy|running|ready|active|completed|ok)\b|成功|完成/gi],
    ['address', /https?:\/\/[^\s<>"'）]+|\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?::\d{1,5})?\b/gi],
  ];
  const COLORS = {
    dark: { error: '#ff7b86', warning: '#f0c674', success: '#77d6a0', address: '#74b9ff' },
    light: { error: '#bd2339', warning: '#926000', success: '#167443', address: '#1466b8' },
  };

  function matches(text, enabled = {}) {
    const result = [];
    const occupied = new Uint8Array(text.length);
    for (const [kind, pattern] of RULES) {
      if (enabled[kind] === false) continue;
      pattern.lastIndex = 0;
      for (const match of text.matchAll(pattern)) {
        const start = match.index;
        const end = start + match[0].length;
        if (occupied.subarray(start, end).some(Boolean)) continue;
        occupied.fill(1, start, end);
        result.push({ kind, start, end });
      }
    }
    return result;
  }

  // Limit parsed-write/scroll bursts without delaying explicit settings/visibility refreshes.
  const MIN_RENDER_INTERVAL = 32;

  class Highlighter {
    constructor(term, settings) {
      this.term = term;
      this.settings = settings;
      this.marks = [];
      this.frame = null;
      this.timer = null;
      this.lastRender = -Infinity;
      this.listeners = [
        term.onWriteParsed(() => this.schedule()),
        term.onScroll(() => this.schedule()),
        term.onResize(() => this.schedule()),
        term.buffer.onBufferChange(() => this.refresh()),
      ];
      this.refresh();
    }

    visible() {
      const element = this.term.element;
      if (!element) return false;
      const bounds = element.getBoundingClientRect();
      return bounds.width > 0 && bounds.height > 0;
    }

    schedule(immediate = false) {
      if (this.disposed || this.frame !== null) return;
      if (this.timer !== null) {
        if (!immediate) return;
        clearTimeout(this.timer);
        this.timer = null;
      }
      const delay = immediate ? 0 : MIN_RENDER_INTERVAL - (performance.now() - this.lastRender);
      if (delay > 0) {
        this.timer = setTimeout(() => {
          this.timer = null;
          this.schedule();
        }, delay);
        return;
      }
      this.frame = requestAnimationFrame(() => {
        this.frame = null;
        this.lastRender = performance.now();
        // Hidden splits need no buffer scan. The owner calls refresh() on visibility resume.
        if (this.visible()) this.render();
      });
    }

    refresh() {
      this.schedule(true);
    }

    clear() {
      for (const { marker, decoration } of this.marks) {
        decoration.dispose();
        marker.dispose();
      }
      this.marks = [];
    }

    render() {
      if (!this.visible()) return;
      const settings = this.settings();
      const buffer = this.term.buffer.active;
      if (settings.terminalHighlight === false || buffer.type !== 'normal') {
        this.clear();
        return;
      }
      // Markers track scrollback trimming themselves. Compare their *current* lines,
      // not the original absolute rows, when reusing unchanged decoration spans.
      const previous = new Map();
      for (const mark of this.marks) {
        const { marker, decoration } = mark;
        const options = decoration.options;
        if (!marker.isDisposed && !decoration.isDisposed) {
          const key = JSON.stringify([marker.line, options.x, options.width, options.foregroundColor]);
          previous.set(key, mark);
        } else {
          decoration.dispose();
          marker.dispose();
        }
      }
      const marks = [];
      const colors = COLORS[settings.theme === 'light' ? 'light' : 'dark'];
      const first = buffer.viewportY;
      const last = Math.min(buffer.length, first + this.term.rows);
      let row = first;
      // Include a wrapped prefix so that words split by the viewport still match.
      while (row > 0 && first - row < 32 && buffer.getLine(row)?.isWrapped) row -= 1;
      for (; row < last;) {
        let text = '';
        const cells = [];
        let next = row;
        do {
          const line = buffer.getLine(next);
          if (!line) break;
          for (let x = 0; x < this.term.cols; x += 1) {
            const cell = line.getCell(x);
            if (!cell || cell.getWidth() === 0) continue;
            const chars = cell.getChars() || ' ';
            const start = text.length;
            text += chars;
            cells.push({ row: next, x, width: cell.getWidth(), start, end: text.length,
              plain: cell.isFgDefault() && cell.isBgDefault() && !cell.isInverse() && !cell.isInvisible() });
          }
          next += 1;
        } while (next < buffer.length && next - row < 64 && buffer.getLine(next)?.isWrapped);
        for (const match of matches(text, settings.terminalHighlightRules)) {
          let span = null;
          const flush = () => {
            if (!span) return;
            const key = JSON.stringify([span.row, span.x, span.width, colors[match.kind]]);
            const existing = previous.get(key);
            if (existing) {
              previous.delete(key);
              marks.push(existing);
            } else {
              const marker = this.term.registerMarker(span.row - buffer.baseY - buffer.cursorY);
              if (marker) {
                const decoration = this.term.registerDecoration({ marker, x: span.x, width: span.width,
                  foregroundColor: colors[match.kind], layer: 'bottom' });
                if (decoration) marks.push({ marker, decoration });
                else marker.dispose();
              }
            }
            span = null;
          };
          for (const cell of cells) {
            if (cell.end <= match.start || cell.start >= match.end) continue;
            if (!cell.plain || cell.row < first || cell.row >= last) { flush(); continue; }
            if (span && span.row === cell.row && span.x + span.width === cell.x) span.width += cell.width;
            else { flush(); span = { row: cell.row, x: cell.x, width: cell.width }; }
          }
          flush();
        }
        row = Math.max(row + 1, next);
      }
      for (const { marker, decoration } of previous.values()) {
        decoration.dispose();
        marker.dispose();
      }
      this.marks = marks;
    }

    dispose() {
      this.disposed = true;
      if (this.frame !== null) cancelAnimationFrame(this.frame);
      if (this.timer !== null) clearTimeout(this.timer);
      this.frame = null;
      this.timer = null;
      this.listeners.forEach((listener) => listener.dispose());
      this.clear();
    }
  }
  window.TerminalHighlight = { Highlighter, matches };
})();
