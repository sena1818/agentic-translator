"""
双栏对照阅读器 HTML 模板（单文件、离线可用）。

交互：
- 点击任一段 → 另一栏滚动到对应段并高亮（双向）；悬停时两栏同时浅色高亮；
- 跟随滚动：滚动一栏时另一栏按"段锚点"对齐（两栏长度不同，按像素比例同步会错位）；
- 目录：由译后标题生成，点击两栏同时定位；
- 脚注：点击上标弹出浮层，不打断阅读；
- 问题段：质检未通过的段以波浪线标出，可用 n / p 逐个跳转；
- 窄屏：单栏显示，点击段落切换到另一语言并定位到对应段。
"""

from __future__ import annotations

import html

_CSS = """
:root {
  --bg: #f7f5f0; --surface: #fffdf8; --surface-2: #f0ece3; --text: #1f1d1a; --muted: #6b665d;
  --line: #e2ddd1; --accent: #2f5d8a; --accent-soft: rgba(47, 93, 138, .14); --hover-soft: rgba(47, 93, 138, .07);
  --warn: #b7791f; --warn-soft: rgba(183, 121, 31, .14); --danger: #b23a3a; --danger-soft: rgba(178, 58, 58, .12);
  --shadow: 0 8px 30px rgba(31, 29, 26, .14);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161514; --surface: #1d1c1a; --surface-2: #262421; --text: #e9e5dc; --muted: #a09a8f;
    --line: #34312c; --accent: #8db4dc; --accent-soft: rgba(141, 180, 220, .18); --hover-soft: rgba(141, 180, 220, .08);
    --warn: #e0a84f; --warn-soft: rgba(224, 168, 79, .16); --danger: #e07a7a; --danger-soft: rgba(224, 122, 122, .14);
    --shadow: 0 8px 30px rgba(0, 0, 0, .5);
  }
}
:root[data-theme="dark"] {
  --bg: #161514; --surface: #1d1c1a; --surface-2: #262421; --text: #e9e5dc; --muted: #a09a8f;
  --line: #34312c; --accent: #8db4dc; --accent-soft: rgba(141, 180, 220, .18); --hover-soft: rgba(141, 180, 220, .08);
  --warn: #e0a84f; --warn-soft: rgba(224, 168, 79, .16); --danger: #e07a7a; --danger-soft: rgba(224, 122, 122, .14);
  --shadow: 0 8px 30px rgba(0, 0, 0, .5);
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background: var(--bg); color: var(--text); display: flex; flex-direction: column;
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
button { font: inherit; color: inherit; }
.topbar {
  display: flex; align-items: center; gap: 12px; padding: 10px 16px; background: var(--surface);
  border-bottom: 1px solid var(--line); flex-wrap: wrap;
}
.topbar h1 { font-size: 16px; margin: 0; font-weight: 600; flex: 1 1 200px; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chip { font-size: 12px; color: var(--muted); background: var(--surface-2); border-radius: 999px; padding: 2px 10px; }
.chip.warn { color: var(--warn); background: var(--warn-soft); }
.chip.danger { color: var(--danger); background: var(--danger-soft); }
.controls { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.btn {
  border: 1px solid var(--line); background: var(--surface); border-radius: 8px; padding: 4px 10px;
  cursor: pointer; font-size: 13px; line-height: 1.5;
}
.btn:hover { border-color: var(--accent); }
.btn[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.seg-switch { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.seg-switch .btn { border: 0; border-radius: 0; }
.layout { flex: 1; display: flex; min-height: 0; }
.toc {
  width: 260px; flex: none; overflow: auto; border-right: 1px solid var(--line); background: var(--surface);
  padding: 12px 8px; font-size: 13px;
}
body.toc-hidden .toc { display: none; }
.toc h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 4px 8px 8px; }
.toc button {
  display: block; width: 100%; text-align: left; border: 0; background: none; cursor: pointer;
  padding: 4px 8px; border-radius: 6px; color: var(--text);
}
.toc button:hover { background: var(--hover-soft); }
.toc button.active { background: var(--accent-soft); color: var(--accent); }
.toc small { display: block; color: var(--muted); font-size: 11px; line-height: 1.3; }
.panes { flex: 1; display: grid; grid-template-columns: 1fr 1fr; min-width: 0; }
.pane { overflow: auto; position: relative; padding: 24px clamp(16px, 4vw, 48px) 40vh; min-width: 0; }
.pane + .pane { border-left: 1px solid var(--line); }
.pane-label {
  position: sticky; top: -24px; margin: -24px calc(-1 * clamp(16px, 4vw, 48px)) 16px; padding: 6px clamp(16px, 4vw, 48px);
  font-size: 12px; color: var(--muted); background: var(--bg); border-bottom: 1px solid var(--line); z-index: 2;
}
#pane-src { font-family: Charter, "Iowan Old Style", Georgia, serif; font-size: 16px; }
#pane-tgt { font-family: "Noto Serif SC", "Source Han Serif SC", "Songti SC", serif; font-size: 16.5px; line-height: 1.85; }
body[data-view="src"] .panes, body[data-view="tgt"] .panes { grid-template-columns: 1fr; }
body[data-view="src"] #pane-tgt, body[data-view="tgt"] #pane-src { display: none; }
.pane img { max-width: 100%; height: auto; }
.pane table { border-collapse: collapse; margin: 1em 0; display: block; overflow-x: auto; }
.pane th, .pane td { border: 1px solid var(--line); padding: 4px 10px; vertical-align: top; }
.pane pre { background: var(--surface-2); padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 13px; }
.pane code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9em; }
.pane blockquote { margin: 1em 0; padding-left: 1em; border-left: 3px solid var(--line); color: var(--muted); }
.pane figure { margin: 1.2em 0; text-align: center; }
.pane figcaption { font-size: .9em; color: var(--muted); }
.pane a { color: var(--accent); }
.pane math[display="block"] { overflow-x: auto; max-width: 100%; }
.footnotes { font-size: .88em; color: var(--muted); }
.seg { border-radius: 3px; transition: background-color .15s; cursor: pointer;
  -webkit-box-decoration-break: clone; box-decoration-break: clone; }
.seg.is-hover { background: var(--hover-soft); }
.seg.is-selected { background: var(--accent-soft); box-shadow: 0 0 0 2px var(--accent-soft); }
.seg-issue { text-decoration: underline wavy var(--warn); text-underline-offset: 4px; text-decoration-thickness: 1px; }
.seg-failed { background: var(--danger-soft); }
.infobar {
  position: fixed; left: 50%; bottom: 16px; transform: translateX(-50%); max-width: min(720px, calc(100vw - 32px));
  background: var(--surface); border: 1px solid var(--line); box-shadow: var(--shadow); border-radius: 10px;
  padding: 8px 14px; font-size: 13px; display: none; gap: 10px; align-items: baseline; z-index: 20;
}
.infobar.show { display: flex; }
.infobar strong { color: var(--accent); white-space: nowrap; }
.infobar .issue { color: var(--warn); }
.popover {
  position: fixed; z-index: 30; max-width: min(420px, calc(100vw - 32px)); max-height: 50vh; overflow: auto;
  background: var(--surface); border: 1px solid var(--line); border-radius: 10px; box-shadow: var(--shadow);
  padding: 10px 14px; font-size: 14px; display: none;
}
.popover.show { display: block; }
.popover p { margin: .3em 0; }
.hint { font-size: 12px; color: var(--muted); }
kbd { font: 11px ui-monospace, monospace; border: 1px solid var(--line); border-bottom-width: 2px;
  border-radius: 4px; padding: 0 4px; background: var(--surface-2); }
@media (max-width: 860px) {
  .toc { position: fixed; inset: 0 30% 0 0; z-index: 25; box-shadow: var(--shadow); }
  body:not(.toc-open) .toc { display: none; }
  body.toc-open .toc { display: block; }
  .panes { grid-template-columns: 1fr; }
  body[data-view="split"] #pane-src { display: none; }
  .split-only, .hint { display: none !important; }
}
"""

_JS = r"""
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const body = document.body;
  const panes = { src: $('#pane-src'), tgt: $('#pane-tgt') };
  const narrow = window.matchMedia('(max-width: 860px)');
  const lockUntil = new WeakMap();
  let selected = null;
  let follow = true;

  const otherPane = (pane) => (pane === panes.src ? panes.tgt : panes.src);
  const find = (pane, id) => pane.querySelector(`.seg[data-seg="${id}"]`);
  const isShown = (pane) => getComputedStyle(pane).display !== 'none';
  const lock = (pane, ms = 700) => lockUntil.set(pane, performance.now() + ms);
  const locked = (pane) => (lockUntil.get(pane) || 0) > performance.now();

  function scrollToSeg(pane, el, offset) {
    const pr = pane.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    const top = pane.scrollTop + er.top - pr.top - (offset ?? pr.height * 0.28);
    lock(pane);
    pane.scrollTo({ top, behavior: 'smooth' });
  }

  function setView(view) {
    body.dataset.view = view;
    $$('[data-set-view]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.setView === view)));
  }

  function showInfo(id) {
    const bar = $('#infobar');
    const el = find(panes.tgt, id) || find(panes.src, id);
    const issue = el && el.dataset.issue;
    $('#info-seg').textContent = `段 ${Number(id) + 1}`;
    $('#info-text').textContent = issue ? `⚠ ${issue}` : '点击另一栏任意段落可反向跳转 · Esc 关闭';
    $('#info-text').className = issue ? 'issue' : 'hint-text';
    bar.classList.add('show');
  }

  function select(id, from) {
    $$('.seg.is-selected').forEach((e) => e.classList.remove('is-selected'));
    selected = String(id);
    const pairs = [[panes.src, find(panes.src, id)], [panes.tgt, find(panes.tgt, id)]];
    pairs.forEach(([, el]) => el && el.classList.add('is-selected'));
    if (narrow.matches && from) {
      const target = otherPane(from);
      setView(target === panes.src ? 'src' : 'tgt');
    }
    requestAnimationFrame(() => {
      for (const [pane, el] of pairs) {
        if (!el || !isShown(pane) || pane === from) continue;
        if (from && from !== pane) {
          const fromEl = find(from, id);
          const offset = fromEl ? fromEl.getBoundingClientRect().top - from.getBoundingClientRect().top : undefined;
          scrollToSeg(pane, el, isShown(from) ? offset : undefined);
        } else {
          scrollToSeg(pane, el);
        }
      }
    });
    showInfo(id);
    markToc(id);
    try { history.replaceState(null, '', `#seg-${id}`); } catch (e) { /* file:// 下可能受限 */ }
  }

  // ---- 点击与悬停联动 ----
  document.addEventListener('click', (event) => {
    const ref = event.target.closest('a.footnote-ref');
    if (ref) { event.preventDefault(); openNote(ref); return; }
    if (!event.target.closest('.popover')) closeNote();
    if (event.target.closest('a')) return;
    const seg = event.target.closest('.seg');
    if (!seg) return;
    const pane = seg.closest('.pane');
    select(seg.dataset.seg, pane);
  });
  for (const pane of Object.values(panes)) {
    pane.addEventListener('mouseover', (event) => {
      const seg = event.target.closest('.seg');
      if (!seg) return;
      $$(`.seg[data-seg="${seg.dataset.seg}"]`).forEach((e) => e.classList.add('is-hover'));
    });
    pane.addEventListener('mouseout', (event) => {
      const seg = event.target.closest('.seg');
      if (!seg) return;
      $$(`.seg[data-seg="${seg.dataset.seg}"]`).forEach((e) => e.classList.remove('is-hover'));
    });
  }

  // ---- 跟随滚动：按段锚点对齐另一栏 ----
  function anchorSeg(pane) {
    const segs = pane.__segs || (pane.__segs = $$('.seg', pane).filter((e) => !e.closest('.footnotes')));
    const top = pane.getBoundingClientRect().top + 48;
    let lo = 0;
    let hi = segs.length - 1;
    let found = segs[0];
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const rect = segs[mid].getBoundingClientRect();
      if (rect.bottom < top) { lo = mid + 1; } else { found = segs[mid]; hi = mid - 1; }
    }
    return found;
  }
  for (const pane of Object.values(panes)) {
    let frame = 0;
    pane.addEventListener('scroll', () => {
      if (!follow || locked(pane) || frame || body.dataset.view !== 'split' || narrow.matches) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const el = anchorSeg(pane);
        const other = otherPane(pane);
        const twin = el && find(other, el.dataset.seg);
        if (!twin) return;
        const delta = el.getBoundingClientRect().top - pane.getBoundingClientRect().top;
        const twinDelta = twin.getBoundingClientRect().top - other.getBoundingClientRect().top;
        lock(other, 120);
        other.scrollTop += twinDelta - delta;
      });
    }, { passive: true });
  }

  // ---- 脚注浮层 ----
  const popover = $('#popover');
  function openNote(ref) {
    const note = document.getElementById(decodeURIComponent(ref.getAttribute('href').slice(1)));
    if (!note) return;
    const clone = note.cloneNode(true);
    clone.querySelectorAll('.footnote-back').forEach((e) => e.remove());
    popover.innerHTML = clone.innerHTML;
    popover.classList.add('show');
    const rect = ref.getBoundingClientRect();
    const width = popover.offsetWidth;
    popover.style.left = `${Math.max(16, Math.min(rect.left - 20, window.innerWidth - width - 16))}px`;
    const below = rect.bottom + 8;
    popover.style.top = below + popover.offsetHeight > window.innerHeight - 16
      ? `${Math.max(16, rect.top - popover.offsetHeight - 8)}px` : `${below}px`;
  }
  function closeNote() { popover.classList.remove('show'); }

  // ---- 目录 ----
  const tocButtons = $$('.toc button[data-seg]');
  function markToc(id) {
    let current = null;
    for (const b of tocButtons) { if (Number(b.dataset.seg) <= Number(id)) current = b; }
    tocButtons.forEach((b) => b.classList.toggle('active', b === current));
  }
  tocButtons.forEach((b) => b.addEventListener('click', () => {
    body.classList.remove('toc-open');
    select(b.dataset.seg, null);
  }));
  $('#toggle-toc').addEventListener('click', () => {
    if (narrow.matches) body.classList.toggle('toc-open');
    else body.classList.toggle('toc-hidden');
  });

  // ---- 视图、跟随、问题段 ----
  $$('[data-set-view]').forEach((b) => b.addEventListener('click', () => setView(b.dataset.setView)));
  const followBtn = $('#toggle-follow');
  followBtn.addEventListener('click', () => {
    follow = !follow;
    followBtn.setAttribute('aria-pressed', String(follow));
  });
  const order = () => $$('.seg', panes.tgt).map((e) => e.dataset.seg);
  const issues = () => $$('.seg-issue, .seg-failed', panes.tgt).map((e) => e.dataset.seg);
  function step(list, dir) {
    if (!list.length) return;
    const ids = list.map(Number);
    const cur = selected === null ? (dir > 0 ? -1 : Infinity) : Number(selected);
    const next = dir > 0 ? ids.find((i) => i > cur) : [...ids].reverse().find((i) => i < cur);
    select(next ?? (dir > 0 ? ids[0] : ids[ids.length - 1]), null);
  }
  $('#next-issue').addEventListener('click', () => step(issues(), 1));
  $('#prev-issue').addEventListener('click', () => step(issues(), -1));

  document.addEventListener('keydown', (event) => {
    if (event.target.closest('input, textarea') || event.metaKey || event.ctrlKey || event.altKey) return;
    const keys = {
      j: () => step(order(), 1), k: () => step(order(), -1),
      n: () => step(issues(), 1), p: () => step(issues(), -1),
      f: () => followBtn.click(), t: () => $('#toggle-toc').click(),
      Escape: () => { closeNote(); $('#infobar').classList.remove('show'); },
    };
    if (keys[event.key]) { event.preventDefault(); keys[event.key](); }
  });

  // ---- 主题 ----
  $('#toggle-theme').addEventListener('click', () => {
    const root = document.documentElement;
    const dark = root.dataset.theme
      ? root.dataset.theme === 'dark'
      : window.matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
  });

  setView(narrow.matches ? 'tgt' : 'split');
  const initial = /^#seg-(\d+)$/.exec(location.hash);
  if (initial) select(initial[1], null);
})();
"""


def _toc_html(entries: list[dict]) -> str:
    if not entries:
        return '<p class="hint" style="padding: 0 8px">文档没有标题</p>'
    base = min(entry["level"] for entry in entries)
    items = []
    for entry in entries:
        indent = (entry["level"] - base) * 14
        source = entry.get("source") or ""
        items.append(
            f'<button type="button" data-seg="{entry["seg"]}" style="padding-left: {8 + indent}px">'
            f"{html.escape(entry['title'])}"
            + (f"<small>{html.escape(source)}</small>" if source and source != entry["title"] else "")
            + "</button>"
        )
    return "\n".join(items)


def render_reader_html(title: str, source_html: str, target_html: str, toc: list[dict], stats: dict) -> str:
    issue_count = stats.get("segments_with_issues", 0)
    dead = len(stats.get("dead_links", []))
    chips = [f'<span class="chip">{stats.get("segments", 0)} 段</span>']
    if issue_count:
        chips.append(f'<span class="chip warn">{issue_count} 段需复核</span>')
    if stats.get("failed_chunks"):
        chips.append(f'<span class="chip danger">{stats["failed_chunks"]} 块翻译失败</span>')
    if dead:
        chips.append(f'<span class="chip danger">{dead} 个内部链接失效</span>')
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body data-view="split">
<header class="topbar">
  <button class="btn" id="toggle-toc" type="button" aria-label="目录">☰ 目录</button>
  <h1>{html.escape(title)}</h1>
  <div class="chips">{"".join(chips)}</div>
  <div class="controls">
    <div class="seg-switch" role="group" aria-label="视图">
      <button class="btn split-only" type="button" data-set-view="split" aria-pressed="true">对照</button>
      <button class="btn" type="button" data-set-view="src" aria-pressed="false">原文</button>
      <button class="btn" type="button" data-set-view="tgt" aria-pressed="false">译文</button>
    </div>
    <button class="btn split-only" id="toggle-follow" type="button" aria-pressed="true" title="快捷键 f">跟随滚动</button>
    <button class="btn" id="prev-issue" type="button" title="上一个问题段（p）"{" disabled" if not issue_count else ""}>↑ 问题</button>
    <button class="btn" id="next-issue" type="button" title="下一个问题段（n）"{" disabled" if not issue_count else ""}>↓ 问题</button>
    <button class="btn" id="toggle-theme" type="button" aria-label="切换深浅色">◐</button>
  </div>
  <span class="hint">点击段落双向跳转 · <kbd>j</kbd>/<kbd>k</kbd> 上下段 · <kbd>n</kbd>/<kbd>p</kbd> 问题段</span>
</header>
<div class="layout">
  <nav class="toc" aria-label="目录"><h2>目录</h2>{_toc_html(toc)}</nav>
  <main class="panes">
    <article class="pane" id="pane-src" lang="en"><div class="pane-label">原文</div>{source_html}</article>
    <article class="pane" id="pane-tgt" lang="zh-CN"><div class="pane-label">译文</div>{target_html}</article>
  </main>
</div>
<div class="infobar" id="infobar" role="status"><strong id="info-seg"></strong><span id="info-text"></span></div>
<div class="popover" id="popover" role="dialog" aria-label="脚注"></div>
<script>{_JS}</script>
</body>
</html>
"""
