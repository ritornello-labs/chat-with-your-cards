import { useEffect, useRef } from "react";
import { EditorState, Prec } from "@codemirror/state";
import { EditorView, drawSelection, keymap, placeholder as cmPlaceholder } from "@codemirror/view";
import { history, historyKeymap } from "@codemirror/commands";
import { getCM, vim } from "@replit/codemirror-vim";

/**
 * A plain value/onChange textarea with vim keybindings — for editing note
 * FIELDS, not for sending messages.
 *
 * `vim_mode` used to reach only the message composer, so the one place you do
 * the most text work — a card's Text/Extra on an edit proposal — dropped back
 * to a bare textarea (user, 2026-07-27).
 *
 * The key contract deliberately differs from VimComposer's, because this is a
 * field and not a message:
 * - **Enter inserts a newline.** In the composer Enter sends; here there is
 *   nothing to send, and a field editor that swallowed Enter would be broken.
 * - **Escape in normal mode blurs and stops there.** The composer's Escape
 *   posts `focus_reviewer`, which would yank focus out of Anki's webview
 *   mid-edit; and letting it bubble reaches AnkiWebView's own Escape handler.
 *   Insert/visual Escape still goes to normal mode, which is vim's job.
 *
 * User mappings are NOT applied here: they are global to the vim engine and
 * VimComposer already applies them (it is mounted whenever vim mode is on, and
 * this editor renders under the same flag), so re-applying would only risk
 * fighting its mapclear/re-map cycle.
 */
export function VimTextArea({
  value,
  onChange,
  disabled,
  placeholder,
  testid,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
  testid?: string;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  // Refs so the mount-once CodeMirror closures always see fresh props.
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const fieldKeys = Prec.highest(
      keymap.of([
        {
          key: "Escape",
          run: (view) => {
            const cm = getCM(view) as unknown as {
              state?: { vim?: { insertMode?: boolean; visualMode?: boolean } };
            } | null;
            const vimState = cm?.state?.vim;
            if (vimState?.insertMode || vimState?.visualMode) return false; // vim's
            view.contentDOM.blur();
            return true; // and no further: never reaches Anki's own Escape
          },
        },
      ])
    );
    const view = new EditorView({
      state: EditorState.create({
        doc: value,
        extensions: [
          fieldKeys,
          vim(),
          // codemirror-vim draws visual-mode selections itself; without this
          // v/V select invisibly (same finding as the composer's).
          drawSelection(),
          history(),
          keymap.of(historyKeymap),
          ...(placeholder ? [cmPlaceholder(placeholder)] : []),
          EditorView.lineWrapping,
          EditorView.editable.of(!disabled),
          EditorView.updateListener.of((update) => {
            if (update.docChanged && !disabledRef.current) {
              onChangeRef.current(update.state.doc.toString());
            }
          }),
        ],
      }),
      parent: host,
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // Mount once: `value` flows in through the sync effect below, and
    // rebuilding the view on every keystroke would lose vim's mode and the
    // cursor with it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // External writes (a revision arriving, the card re-seeding) flow into CM.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (value !== current) {
      view.dispatch({ changes: { from: 0, to: current.length, insert: value } });
    }
  }, [value]);

  return (
    <div
      className="cwyc-vim-field"
      data-testid={testid}
      data-disabled={disabled ? "true" : undefined}
      ref={hostRef}
    />
  );
}
