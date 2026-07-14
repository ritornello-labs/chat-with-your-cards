import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { highlightMatch } from "./ComboBox";

/**
 * Anki-editor-like tag entry: removable chips plus an inline text input with
 * an in-DOM suggestion dropdown (reuses the ComboBox's cwyc-combo-list/item
 * styling so the pins panel's two typeable fields feel like one family -
 * never a native <select>/<datalist>, which render at broken positions
 * inside Anki's webview).
 */
export interface TagChipsProps {
  readonly tags: readonly string[];
  readonly onChange: (tags: string[]) => void;
  readonly suggestions: readonly string[];
  readonly testid?: string;
}

export function TagChips({ tags, onChange, suggestions, testid }: TagChipsProps) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const filtered = useMemo(() => {
    const q = text.trim().toLowerCase();
    const remaining = suggestions.filter((s) => !tags.includes(s));
    if (!q) return remaining;
    return remaining.filter((s) => s.toLowerCase().includes(q));
  }, [suggestions, tags, text]);

  const addTag = (raw: string) => {
    const t = raw.trim();
    setText("");
    setOpen(false);
    setActiveIndex(-1);
    if (!t || tags.includes(t)) return;
    onChange([...tags, t]);
  };

  const removeTag = (index: number) => {
    onChange(tags.filter((_, i) => i !== index));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      setActiveIndex((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) return;
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" || e.key === "," || e.key === " ") {
      e.preventDefault();
      if (open && activeIndex >= 0 && filtered[activeIndex] !== undefined) {
        addTag(filtered[activeIndex]);
      } else {
        addTag(text);
      }
    } else if (e.key === "Backspace" && text === "" && tags.length > 0) {
      e.preventDefault();
      removeTag(tags.length - 1);
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
        setActiveIndex(-1);
      }
    }
  };

  return (
    <div className="cwyc-tagchips" ref={rootRef} onClick={() => rootRef.current?.querySelector("input")?.focus()}>
      {tags.map((tag, i) => (
        <span className="cwyc-tagchip" key={tag}>
          {tag}
          <button
            type="button"
            className="cwyc-tagchip-remove"
            aria-label={`Remove tag ${tag}`}
            onClick={(e) => {
              e.stopPropagation();
              removeTag(i);
            }}
          >
            ×
          </button>
        </span>
      ))}
      <input
        type="text"
        className="cwyc-tagchips-input"
        value={text}
        placeholder={tags.length ? "" : "Add tag…"}
        data-testid={testid}
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setText(e.target.value);
          setOpen(true);
          setActiveIndex(-1);
        }}
        onBlur={() => setOpen(false)}
        onKeyDown={onKeyDown}
      />
      {open && filtered.length > 0 ? (
        <div className="cwyc-combo-list cwyc-tagchips-list" role="listbox" onMouseDown={(e) => e.preventDefault()}>
          {filtered.map((option, i) => (
            <button
              key={option}
              type="button"
              className={"cwyc-combo-item" + (i === activeIndex ? " cwyc-combo-active" : "")}
              onClick={() => addTag(option)}
            >
              {highlightMatch(option, text)}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
