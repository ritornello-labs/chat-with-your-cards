import { useEffect, useRef } from "react";
import { unstable_useComposerInput } from "@assistant-ui/react";
import { EditorState, Prec } from "@codemirror/state";
import { EditorView, keymap, placeholder } from "@codemirror/view";
import { history, historyKeymap } from "@codemirror/commands";
import { Vim, getCM, vim } from "@replit/codemirror-vim";
import { postCommand } from "../bridge";
import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore } from "../store";

/**
 * The composer input with vim keybindings (Settings > "Vim keys in composer",
 * off by default - user-requested 2026-07-13). A CodeMirror 6 editor running
 * @replit/codemirror-vim replaces the plain textarea; assistant-ui still owns
 * the message flow through its headless composer bridge
 * (unstable_useComposerInput): every CM edit mirrors into the composer via
 * setText (which keeps the Send button's enabled state correct), Enter sends
 * through the same runtime path, and external clears (post-send, new chat)
 * flow back into CM.
 *
 * User mappings come from the `vim_mappings` config (list of
 * [keys, mapped-to, mode] triples, vim `:map` semantics via Vim.map); the
 * defaults are adapted from the user's vimrc: `fd` leaves insert mode, j/k
 * move by visual line (gj/gk) in normal+visual, Y yanks to end of line, and
 * [<Space>/]<Space> add blank lines. Mappings are global to the vim engine,
 * so re-applying on re-mount is idempotent.
 *
 * Key contract (mirrors the textarea path):
 * - Enter sends (any mode; swallowed while a reply is streaming),
 *   Shift+Enter inserts a newline.
 * - Esc in insert/visual mode goes to normal mode (vim's own handling); Esc
 *   in normal mode keeps the textarea path's behavior - stop generation
 *   while streaming, else return focus to the reviewer.
 * - Shift+Tab cycles permission modes.
 */
export function VimComposer({ store }: { store: ChatStore }) {
  const composer = unstable_useComposerInput();
  const { isRunning, ui } = useChatState(store);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  // Refs so the (mount-once) CM keymap closures always see fresh values.
  const composerRef = useRef(composer);
  composerRef.current = composer;
  const runningRef = useRef(isRunning);
  runningRef.current = isRunning;
  const storeRef = useRef(store);
  storeRef.current = store;

  const mappings = ui.settings?.vimMappings ?? [];

  useEffect(() => {
    for (const m of mappings) {
      try {
        Vim.map(m[0], m[1], m[2]);
      } catch {
        // A malformed mapping must not break the composer; skip it.
      }
    }
  }, [mappings]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const chatKeys = Prec.highest(
      keymap.of([
        {
          key: "Enter",
          run: (view) => {
            if (runningRef.current) return true; // parity: send disabled while streaming
            const text = view.state.doc.toString();
            if (!text.trim()) return true;
            const c = composerRef.current;
            c.setText(text);
            c.send(); // the post-send clear flows back via the value-sync effect
            return true;
          },
        },
        {
          key: "Shift-Enter",
          run: (view) => {
            view.dispatch(view.state.replaceSelection("\n"));
            return true;
          },
        },
        {
          key: "Escape",
          run: (view) => {
            const cm = getCM(view) as unknown as {
              state?: { vim?: { insertMode?: boolean; visualMode?: boolean } };
            } | null;
            const vimState = cm?.state?.vim;
            if (vimState?.insertMode || vimState?.visualMode) {
              return false; // let vim take it back to normal mode
            }
            if (runningRef.current) storeRef.current.cancel();
            else postCommand({ type: "focus_reviewer" });
            return true;
          },
        },
        {
          key: "Shift-Tab",
          run: () => {
            storeRef.current.cyclePermissionMode();
            return true;
          },
        },
      ])
    );
    const view = new EditorView({
      state: EditorState.create({
        doc: composerRef.current.value,
        extensions: [
          chatKeys,
          vim(),
          history(),
          keymap.of(historyKeymap),
          placeholder("Ask about this card…"),
          EditorView.lineWrapping,
          EditorView.updateListener.of((update) => {
            if (update.docChanged) {
              composerRef.current.setText(update.state.doc.toString());
            }
          }),
        ],
      }),
      parent: host,
    });
    viewRef.current = view;
    if (import.meta.env.DEV) {
      // Dev-preview test hook (never in the production bundle): lets the
      // browser harness inspect vim mode state programmatically.
      (window as unknown as { cwycVimView?: EditorView }).cwycVimView = view;
    }
    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, []);

  // External composer writes (post-send clear, new chat) flow back into CM.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (composer.value !== current) {
      view.dispatch({ changes: { from: 0, to: current.length, insert: composer.value } });
    }
  }, [composer.value]);

  return <div className="cwyc-vim-editor" data-testid="composer-input-vim" ref={hostRef} />;
}
