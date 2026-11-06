// WikiHub editor-surface ProseMirror plugin (wikihub-k7k5).
//
// One small plugin that gives the page editor + suggest-edit flow what the
// presets do not:
//
//   * Sync on real document changes (D8). The host's onUpdate callback fires
//     only for transactions that changed the doc AFTER the editor finished
//     mounting, so toolbar clicks, slash-menu picks, drag/IME edits all sync,
//     while the load-time parse never rewrites the canonical Markdown.
//   * Trigger detection for the `[[` wikilink menu and the `/` slash menu.
//     State changes are emitted as DOM CustomEvents on the editor container
//     (`wikihub:trigger` with {kind, query, from, to, coords} or kind=null when
//     closed) so the host page owns the menu UI.
//   * Keyboard capture while a host menu is open (container.dataset.menuOpen
//     === '1'): ArrowUp/ArrowDown/Enter/Tab/Escape are forwarded as
//     `wikihub:menukey` events instead of editing the document.
//   * `wikihub:selection` events on every view update so the toolbar can
//     reflect active marks/blocks (aria-pressed).

import { $prose } from '@milkdown/utils';
import { Plugin, PluginKey } from '@milkdown/prose/state';

export const surfaceKey = new PluginKey('wikihub-surface');

const WIKILINK_TRIGGER = /\[\[([^\]\n|]*)$/;
// `/` at the start of a block or after whitespace, followed by an optional
// short filter word. Kept narrow so URLs (`a/b`) never open the menu.
// A space right after `/` ("1 / 2") closes it.
const SLASH_TRIGGER = /(?:^|\s)\/((?:[a-z0-9][a-z0-9 ]{0,23})?)$/i;

function detectTrigger(state) {
  const sel = state.selection;
  if (!sel.empty) return null;
  const $from = sel.$from;
  if (!$from.parent.isTextblock) return null;
  if ($from.parent.type.spec.code) return null;
  const blockStart = $from.start();
  const textBefore = $from.parent.textBetween(0, $from.parentOffset, '\n', '￼');
  let m = textBefore.match(WIKILINK_TRIGGER);
  if (m) {
    return {
      kind: 'wikilink',
      query: m[1],
      from: blockStart + $from.parentOffset - m[0].length,
      to: $from.pos,
    };
  }
  m = textBefore.match(SLASH_TRIGGER);
  if (m) {
    const len = m[1].length + 1; // "/" + filter
    return {
      kind: 'slash',
      query: m[1],
      from: $from.pos - len,
      to: $from.pos,
    };
  }
  return null;
}

// options: { container, onDocChanged(view), isReady() }
export function createSurfacePlugin(options) {
  return $prose(() => {
    let lastTriggerSig = '';
    return new Plugin({
      key: surfaceKey,
      props: {
        handleKeyDown(view, event) {
          const container = options.container;
          if (!container || container.dataset.menuOpen !== '1') return false;
          if (!['ArrowUp', 'ArrowDown', 'Enter', 'Tab', 'Escape'].includes(event.key)) return false;
          container.dispatchEvent(new CustomEvent('wikihub:menukey', { detail: { key: event.key } }));
          event.preventDefault();
          return true;
        },
      },
      view() {
        return {
          update(view, prevState) {
            const docChanged = !view.state.doc.eq(prevState.doc);
            if (docChanged && options.isReady()) options.onDocChanged(view);
            const container = options.container;
            if (!container) return;
            const trig = view.hasFocus() ? detectTrigger(view.state) : null;
            const sig = trig ? `${trig.kind}:${trig.query}:${trig.from}` : '';
            if (sig !== lastTriggerSig) {
              lastTriggerSig = sig;
              let detail = { kind: null };
              if (trig) {
                let coords = null;
                try { coords = view.coordsAtPos(trig.to); } catch (_e) {}
                detail = { ...trig, coords };
              }
              container.dispatchEvent(new CustomEvent('wikihub:trigger', { detail }));
            }
            if (docChanged || !view.state.selection.eq(prevState.selection)) {
              container.dispatchEvent(new CustomEvent('wikihub:selection'));
            }
          },
          destroy() {},
        };
      },
    });
  });
}
