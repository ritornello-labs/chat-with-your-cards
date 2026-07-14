import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

/**
 * A typeable, in-DOM-only autocomplete input. Anki's real webview renders
 * native <select>/<datalist> popups at broken screen positions (that is the
 * bug this replaces - see the pins panel rewrite), so this renders its own
 * absolutely-positioned suggestion list instead of ever touching native
 * popup UI.
 */
/**
 * Hard cap on rendered suggestions, shared with TagChips: real collections
 * have hundreds of decks and tens of thousands of tags (36k+ observed,
 * dogfood 2026-07-14), and rendering the unfiltered list froze the pins
 * panel solid. Filtering scans everything; only this many reach the DOM.
 */
export const MAX_SUGGESTIONS = 40;

export interface ComboBoxProps {
  readonly value: string;
  readonly onChange: (v: string) => void;
  readonly options: readonly string[];
  readonly placeholder?: string;
  readonly testid?: string;
  readonly allowFreeText?: boolean;
}

/** Case-insensitive substring match, returned with the match span highlighted. */
export function highlightMatch(text: string, query: string): ReactNode {
  const q = query.trim();
  if (!q) return text;
  const idx = text.toLowerCase().indexOf(q.toLowerCase());
  if (idx < 0) return text;
  return (
    <>
      {text.slice(0, idx)}
      <span className="cwyc-combo-match">{text.slice(idx, idx + q.length)}</span>
      {text.slice(idx + q.length)}
    </>
  );
}

export function ComboBox({ value, onChange, options, placeholder, testid, allowFreeText }: ComboBoxProps) {
  const [text, setText] = useState(value);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const lastValidRef = useRef(value);

  // Re-seed from the authoritative value whenever it changes from outside
  // (Clear button, reopening the panel with a fresh draft, etc.).
  useEffect(() => {
    setText(value);
    lastValidRef.current = value;
  }, [value]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const { filtered, overflow } = useMemo(() => {
    const q = text.trim().toLowerCase();
    const out: string[] = [];
    let extra = 0;
    for (const o of options) {
      if (q && !o.toLowerCase().includes(q)) continue;
      if (out.length < MAX_SUGGESTIONS) out.push(o);
      else extra += 1;
    }
    return { filtered: out, overflow: extra };
  }, [options, text]);

  const showClear = value.length > 0;
  const itemCount = (showClear ? 1 : 0) + filtered.length;

  const commit = (v: string) => {
    setText(v);
    lastValidRef.current = v;
    onChange(v);
    setOpen(false);
    setActiveIndex(-1);
  };

  const selectIndex = (index: number) => {
    if (showClear && index === 0) {
      commit("");
      return;
    }
    const option = filtered[index - (showClear ? 1 : 0)];
    if (option !== undefined) commit(option);
  };

  const onInputChange = (v: string) => {
    setText(v);
    onChange(v);
    setOpen(true);
    setActiveIndex(-1);
  };

  const onBlur = () => {
    if (allowFreeText) return; // free text is committed live as the user types
    const trimmed = text.trim();
    const match = options.find((o) => o.toLowerCase() === trimmed.toLowerCase());
    if (match !== undefined) {
      if (match !== text) setText(match);
      if (match !== value) onChange(match);
      lastValidRef.current = match;
    } else {
      setText(lastValidRef.current);
      if (lastValidRef.current !== value) onChange(lastValidRef.current);
    }
    setOpen(false);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      setActiveIndex((i) => Math.min(i + 1, itemCount - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) return;
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (open && activeIndex >= 0) {
        selectIndex(activeIndex);
      } else if (allowFreeText) {
        commit(text.trim());
      } else {
        const trimmed = text.trim();
        const match = options.find((o) => o.toLowerCase() === trimmed.toLowerCase());
        if (match !== undefined) commit(match);
        setOpen(false);
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
        setActiveIndex(-1);
      }
    }
  };

  return (
    <div className="cwyc-combo" ref={rootRef}>
      <input
        type="text"
        className="cwyc-combo-input"
        value={text}
        placeholder={placeholder}
        data-testid={testid}
        onFocus={() => setOpen(true)}
        onChange={(e) => onInputChange(e.target.value)}
        onBlur={onBlur}
        onKeyDown={onKeyDown}
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
      />
      {open && itemCount > 0 ? (
        <div className="cwyc-combo-list" role="listbox" onMouseDown={(e) => e.preventDefault()}>
          {showClear ? (
            <button
              type="button"
              className={"cwyc-combo-item cwyc-combo-clear" + (activeIndex === 0 ? " cwyc-combo-active" : "")}
              onClick={() => commit("")}
            >
              Clear
            </button>
          ) : null}
          {filtered.map((option, i) => {
            const index = i + (showClear ? 1 : 0);
            return (
              <button
                key={option}
                type="button"
                className={"cwyc-combo-item" + (index === activeIndex ? " cwyc-combo-active" : "")}
                onClick={() => commit(option)}
              >
                {highlightMatch(option, text)}
              </button>
            );
          })}
          {overflow > 0 ? (
            <div className="cwyc-combo-overflow">…{overflow} more — keep typing</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
