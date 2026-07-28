import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";

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

/**
 * One option, LEAF FIRST.
 *
 * Deck and tag names are `::`-separated paths, and the part that tells them
 * apart is the LAST component - but a single truncated line ellipsises from
 * the right, so every deep sibling rendered as the identical
 * "Decks::Geography…" and the choice was blind (user, 2026-07-27). The leaf
 * gets the readable line; the parent path sits under it, dimmed, and is the
 * only thing allowed to truncate.
 */
export function OptionLabel({ value, query }: { value: string; query: string }) {
  const cut = value.lastIndexOf("::");
  if (cut < 0) return <span className="cwyc-combo-leaf">{highlightMatch(value, query)}</span>;
  return (
    <>
      <span className="cwyc-combo-leaf">{highlightMatch(value.slice(cut + 2), query)}</span>
      <span className="cwyc-combo-path">{highlightMatch(value.slice(0, cut), query)}</span>
    </>
  );
}

/**
 * Position the dropdown in VIEWPORT coordinates instead of inside the input.
 *
 * The pins panel is `overflow-y: auto`, which clips any absolutely positioned
 * descendant - the tag list was sliced off mid-row at the panel edge (user,
 * 2026-07-27). `position: fixed` escapes that, and while we are measuring
 * anyway the list can be wider than the narrow input it hangs off, which is
 * what makes long paths readable at all. Recomputed on scroll/resize so it
 * cannot drift away from its input.
 */
export function useAnchoredList(open: boolean, anchor: React.RefObject<HTMLElement | null>) {
  const [style, setStyle] = useState<CSSProperties>();
  useEffect(() => {
    if (!open) return;
    const place = () => {
      const el = anchor.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const gap = 3;
      const below = window.innerHeight - r.bottom - gap - 8;
      const above = r.top - gap - 8;
      const dropUp = below < 140 && above > below;
      // Wider than the input when there is room, so a deep path has somewhere
      // to go; never wider than the window.
      const width = Math.min(Math.max(r.width, 260), window.innerWidth - 16);
      const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
      // BOTH edges, always. Leaving `top` to the stylesheet while setting
      // `bottom` here over-constrains a fixed box: the browser then derives
      // the height from the two edges instead of the content, and the list
      // collapsed to its padding (10px) whenever it opened upward.
      setStyle({
        position: "fixed",
        left,
        width,
        maxHeight: Math.max(120, Math.min(220, dropUp ? above : below)),
        top: dropUp ? "auto" : r.bottom + gap,
        bottom: dropUp ? window.innerHeight - r.top + gap : "auto",
      });
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, anchor]);
  return style;
}

export function ComboBox({ value, onChange, options, placeholder, testid, allowFreeText }: ComboBoxProps) {
  const [text, setText] = useState(value);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listStyle = useAnchoredList(open, inputRef);
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
        ref={inputRef}
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
        <div
          className="cwyc-combo-list"
          style={listStyle}
          role="listbox"
          onMouseDown={(e) => e.preventDefault()}
        >
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
                <OptionLabel value={option} query={text} />
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
