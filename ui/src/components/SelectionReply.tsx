import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useChatState } from "../ChatRuntimeProvider";
import type { ChatStore } from "../store";
import { MAX_REPLY_COMMENT_CHARS, type ReplyQuote } from "../replyQuotes";
import { CWYC_DISMISS_EVENT } from "../hooks/useDismiss";
import { EDIT_ANNOTATION_EVENT } from "./AnnotationSummary";

interface Passage { messageId: string; text: string; range: Range }
interface Editor { index: number; isNew: boolean; comment: string; passage: Passage }
interface Marker { index: number; left: number; top: number }
const quoteKey = (q: ReplyQuote) => JSON.stringify([q.source_turn, q.text]);

/** Keep the real DOM range: markdown selections can cross inline elements. */
function selectedPassage(): Passage | null {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return null;
  const range = selection.getRangeAt(0);
  const parent = (node: Node) => node instanceof Element ? node : node.parentElement;
  const part = parent(range.startContainer)?.closest<HTMLElement>("[data-reply-text]");
  if (!part || !part.contains(range.endContainer)) return null;
  const row = part.closest<HTMLElement>("[data-reply-message]");
  if (!row || row.closest("[inert]")) return null;
  const text = selection.toString();
  return text.trim() ? { messageId: row.dataset.replyMessage!, text, range: range.cloneRange() } : null;
}

function passageBounds(passage: Passage) {
  if (!passage.range.startContainer.isConnected) return null;
  const row = [...document.querySelectorAll<HTMLElement>("[data-reply-message]")]
    .find((el) => el.dataset.replyMessage === passage.messageId);
  const viewport = row?.closest<HTMLElement>(".cwyc-viewport");
  if (!viewport) return null;
  const bounds = viewport.getBoundingClientRect();
  const rect = passage.range.getBoundingClientRect();
  if (!rect.height || rect.bottom < bounds.top || rect.top > bounds.bottom) return null;
  return { rect, bounds };
}

/** Rebuild a range after a DOM remount without rewriting the rendered answer. */
function findPassage(store: ChatStore, quote: ReplyQuote): Passage | null {
  let turn = 0;
  const message = store.getSnapshot().messages.find((m) => {
    if (m.role === "user") turn++;
    return m.role === "assistant" && turn === quote.source_turn;
  });
  const row = [...document.querySelectorAll<HTMLElement>("[data-reply-message]")]
    .find((el) => el.dataset.replyMessage === message?.id);
  if (!row || !message?.id) return null;
  for (const part of row.querySelectorAll<HTMLElement>("[data-reply-text]")) {
    const offset = part.textContent?.indexOf(quote.text) ?? -1;
    if (offset < 0) continue;
    const walker = document.createTreeWalker(part, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    let node: Node | null, count = 0, started = false;
    while ((node = walker.nextNode())) {
      const end = count + (node.textContent?.length ?? 0);
      if (!started && offset < end) { range.setStart(node, offset - count); started = true; }
      if (started && offset + quote.text.length <= end) {
        range.setEnd(node, offset + quote.text.length - count);
        return { messageId: message.id, text: quote.text, range };
      }
      count = end;
    }
  }
  return null;
}

export function SelectionReply({ store }: { store: ChatStore }) {
  const { ui, messages } = useChatState(store);
  const [selection, setSelection] = useState<Passage | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [markers, setMarkers] = useState<Marker[]>([]);
  const [position, setPosition] = useState({ left: 0, top: 0, width: 300, maxHeight: 300 });
  const actionRef = useRef<HTMLButtonElement>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const anchors = useRef(new Map<string, Passage>());
  const editorState = useRef(editor);
  editorState.current = editor;

  const close = useCallback((cancel = true) => {
    const current = editorState.current;
    if (cancel && current?.isNew) store.removeReplyQuote(current.index);
    editorState.current = null;
    setEditor(null);
    store.setReplyEditorOpen(false);
    setSelection(null);
  }, [store]);

  const open = useCallback((index: number, isNew: boolean, passage: Passage) => {
    const quote = store.getSnapshot().ui.replyQuotes[index];
    if (!quote) return;
    anchors.current.set(quoteKey(quote), passage);
    setSelection(null);
    window.getSelection()?.removeAllRanges();
    const next = { index, isNew, passage, comment: quote.comment ?? "" };
    editorState.current = next;
    setEditor(next);
    store.setReplyEditorOpen(true);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [store]);

  const add = useCallback((passage: Passage) => {
    const before = store.getSnapshot().ui.replyQuotes.length;
    const index = store.addReplyQuote(passage.messageId, passage.text);
    if (index !== null) open(index, index === before, passage);
    else setSelection(null);
  }, [open, store]);

  const save = useCallback(() => {
    const current = editorState.current;
    if (!current) return;
    store.editReplyComment(current.index, current.comment);
    close(false);
    window.chatUI?.focusComposer();
  }, [store, close]);

  const repaint = useCallback(() => {
    const quotes = store.getSnapshot().ui.replyQuotes;
    const keep = new Set(quotes.map(quoteKey));
    for (const key of anchors.current.keys()) if (!keep.has(key)) anchors.current.delete(key);
    const next: Marker[] = [];
    const ranges: Range[] = [];
    quotes.forEach((quote, index) => {
      let passage = anchors.current.get(quoteKey(quote));
      if (!passage?.range.startContainer.isConnected || passage.range.collapsed) passage = findPassage(store, quote) ?? undefined;
      if (!passage) return;
      anchors.current.set(quoteKey(quote), passage);
      ranges.push(passage.range);
      const box = passageBounds(passage);
      if (!box) return;
      let top = Math.max(box.bounds.top + 3, box.rect.top - 4);
      for (const previous of next) if (Math.abs(top - previous.top) < 26) top = previous.top + 28;
      if (top + 24 <= box.bounds.bottom) next.push({ index, left: box.bounds.right - 28, top });
    });
    setMarkers(next);
    // CSS Highlights leave markdown/React's text nodes intact, including code.
    const css = CSS as typeof CSS & { highlights?: Map<string, unknown> };
    const HighlightType = (window as unknown as { Highlight?: new (...ranges: Range[]) => unknown }).Highlight;
    if (css.highlights && HighlightType) {
      css.highlights.set("cwyc-annotations", new HighlightType(...ranges));
      css.highlights.set("cwyc-active-annotation", new HighlightType(
        ...(editorState.current ? [editorState.current.passage.range] : [])
      ));
    }
    const current = editorState.current;
    if (!current) return;
    const box = passageBounds(current.passage);
    const panel = editorRef.current;
    if (!box || !panel) return;
    const width = Math.min(360, box.bounds.width - 16);
    const maxHeight = box.bounds.height - 12;
    const height = Math.min(panel.getBoundingClientRect().height, maxHeight);
    const above = box.rect.top - height - 10;
    const top = above >= box.bounds.top + 6 ? above : box.rect.bottom + 8;
    setPosition({ width, maxHeight,
      left: Math.max(box.bounds.left + 8, Math.min(box.rect.left, box.bounds.right - width - 8)),
      top: Math.max(box.bounds.top + 6, Math.min(top, box.bounds.bottom - height - 6)),
    });
  }, [store]);

  useLayoutEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = "auto";
      inputRef.current.style.height = `${Math.min(inputRef.current.scrollHeight, 190)}px`;
    }
    repaint();
  }, [editor, ui.replyQuotes, position.width, repaint]);

  useEffect(() => {
    const current = editorState.current;
    if (current && (!ui.replyEditorOpen || !messages.some((m) => m.id === current.passage.messageId))) {
      close(false);
    }
  }, [ui.replyEditorOpen, messages, close]);

  useEffect(() => {
    let frame = 0, dragging = false;
    const clearSelection = () => { cancelAnimationFrame(frame); setSelection(null); };
    const update = () => {
      cancelAnimationFrame(frame);
      if (!dragging && !editorState.current) frame = requestAnimationFrame(() => setSelection(selectedPassage()));
    };
    const down = (event: PointerEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest(".cwyc-annotation-editor, .cwyc-annotation-marker") || actionRef.current?.contains(target)) return;
      if (editorState.current && target.closest("[data-testid=send]")) return;
      if (editorState.current) close();
      dragging = true;
      clearSelection();
    };
    const up = () => { dragging = false; update(); };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (editorState.current) { event.preventDefault(); event.stopPropagation(); close(); }
        clearSelection();
      }
      if (event.key === "Enter" && event.altKey && !event.ctrlKey && !event.metaKey && !editorState.current) {
        const passage = selectedPassage();
        if (passage) { event.preventDefault(); event.stopPropagation(); add(passage); }
      }
    };
    const edit = (event: Event) => {
      const index = (event as CustomEvent<number>).detail;
      const quote = store.getSnapshot().ui.replyQuotes[index];
      if (!quote) return;
      if (editorState.current?.index === index) { inputRef.current?.focus(); return; }
      if (editorState.current) close();
      const currentIndex = store.getSnapshot().ui.replyQuotes.findIndex((q) => quoteKey(q) === quoteKey(quote));
      if (currentIndex < 0) return;
      const passage = anchors.current.get(quoteKey(quote)) ?? findPassage(store, quote);
      if (!passage) return;
      const element = passage.range.startContainer.parentElement;
      element?.scrollIntoView({ block: "center" });
      open(currentIndex, false, passage);
    };
    const scroll = (event: Event) => {
      if ((event.target as HTMLElement)?.closest?.(".cwyc-annotation-editor")) return;
      clearSelection();
      repaint();
    };
    const dismiss = () => { clearSelection(); close(); };
    document.addEventListener("selectionchange", update);
    document.addEventListener("pointerdown", down);
    document.addEventListener("pointerup", up);
    document.addEventListener("keydown", key, true);
    document.addEventListener("scroll", scroll, true);
    document.addEventListener(CWYC_DISMISS_EVENT, dismiss);
    document.addEventListener(EDIT_ANNOTATION_EVENT, edit);
    window.addEventListener("blur", clearSelection);
    window.addEventListener("resize", repaint);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("selectionchange", update);
      document.removeEventListener("pointerdown", down);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("scroll", scroll, true);
      document.removeEventListener(CWYC_DISMISS_EVENT, dismiss);
      document.removeEventListener(EDIT_ANNOTATION_EVENT, edit);
      window.removeEventListener("blur", clearSelection);
      window.removeEventListener("resize", repaint);
      const css = CSS as typeof CSS & { highlights?: Map<string, unknown> };
      css.highlights?.delete("cwyc-annotations");
      css.highlights?.delete("cwyc-active-annotation");
    };
  }, [add, close, open, repaint, store]);

  if (ui.pane !== "chat" || ui.dock?.expanded === false) return null;
  const actionBox = selection && passageBounds(selection);
  return <>
    {!editor && actionBox && <button ref={actionRef} type="button" className="cwyc-selection-reply"
      data-testid="reply-to-selection" aria-label="Add selection to chat" title="Add to chat (Alt+Enter)"
      style={{ left: Math.max(actionBox.bounds.left + 6, Math.min(actionBox.rect.left, actionBox.bounds.right - 112)),
        top: Math.max(actionBox.bounds.top + 4, actionBox.rect.top - 36) }}
      onPointerDown={(event) => event.preventDefault()} onClick={() => add(selection!)}>Add to chat</button>}
    {markers.map((marker) => <button key={marker.index} type="button"
      className="cwyc-annotation-marker" data-testid="annotation-marker"
      aria-label={`Edit annotation ${marker.index + 1}`} title={`Edit annotation ${marker.index + 1}`}
      style={{ left: marker.left, top: marker.top }}
      onClick={() => document.dispatchEvent(new CustomEvent(EDIT_ANNOTATION_EVENT, { detail: marker.index }))}>
      {marker.index + 1}</button>)}
    {editor && <div ref={editorRef} role="dialog" aria-label={editor.isNew ? "Add annotation" : "Edit annotation"}
      className="cwyc-annotation-editor cwyc-panel" data-testid="annotation-editor"
      style={position} onKeyDown={(event) => {
        if (event.key === "Enter" && !event.nativeEvent.isComposing &&
          (event.metaKey || event.ctrlKey || (editor.isNew && !event.shiftKey))) {
          event.preventDefault(); event.stopPropagation(); save();
        }
      }}>
      <div className="cwyc-annotation-input-row">
        <textarea ref={inputRef} className="cwyc-annotation-input cwyc-reply-comment"
          data-testid="reply-comment" aria-label={`Comment for annotation ${editor.index + 1}`}
          placeholder="Add an optional comment…" rows={1} maxLength={MAX_REPLY_COMMENT_CHARS}
          value={editor.comment} onChange={(event) => setEditor({ ...editor, comment: event.target.value })} />
        {editor.isNew && <button type="button" className="cwyc-annotation-add" data-testid="save-annotation"
          aria-label="Add annotation" title="Add annotation" onClick={save}>↑</button>}
      </div>
      {!editor.isNew && <div className="cwyc-annotation-actions">
        <button type="button" className="cwyc-annotation-delete" data-testid="delete-annotation"
          aria-label="Delete annotation" title="Delete annotation"
          onClick={() => { store.removeReplyQuote(editor.index); close(false); window.chatUI?.focusComposer(); }}>
          <svg width="17" height="17" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M3 5h14M8 5V3h4v2M5 5l1 12h8l1-12M8 8v6m4-6v6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </button>
        <button type="button" className="cwyc-annotation-cancel" data-testid="cancel-annotation"
          onClick={() => { close(); window.chatUI?.focusComposer(); }}>Cancel</button>
        <button type="button" className="cwyc-annotation-save" data-testid="save-annotation" onClick={save}>Save</button>
      </div>}
    </div>}
  </>;
}
