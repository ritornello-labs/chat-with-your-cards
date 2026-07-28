"""Diagnose an empty Anki search: did a term name something that exists?

Why this exists. To Anki, a search naming a deck, tag, or note type that does
not exist is not an error - it simply matches nothing. So a typo and a
genuinely empty deck produce byte-identical results, and the assistant cannot
tell them apart. In dogfooding it searched ``deck:Default``, got zero cards,
and reported to the user that the deck was empty; the real deck was
``Decks::Default`` (2026-07-23). An empty result had been read as an answer.

The check runs ONLY on a search that already returned nothing. A query that
matched something is never second-guessed, so nothing here can break a working
search - the worst case for a parser disagreement is a missing hint on an
already-empty result. It is a diagnosis, not a validator.

Suggestions are tiered rather than pure edit distance, because the failure that
started this is not a typo: ``Default`` is six edits from ``Decks::Default``
but is exactly its leaf component. Leaf and component matches therefore rank
above substring matches, which rank above near-misses.

aqt-free for unit testing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

MAX_SUGGESTIONS = 5
MAX_EDIT_DISTANCE = 2
# A diagnosis runs on Anki's main thread; never let a pathological tag list
# turn a failed search into a visible stall.
MAX_CANDIDATES_SCANNED = 5000

_BOOLEAN_KEYWORDS = {"and", "or", "not"}

# prefix -> (human name, hierarchical). Hierarchical means `x` also matches
# `x::child`, which is how Anki treats both decks and tags.
_CHECKED: dict[str, tuple[str, bool]] = {
    "deck": ("deck", True),
    "tag": ("tag", True),
    "note": ("note type", False),
}

# Values Anki gives a special meaning; they name no real entity, so their
# absence from the collection means nothing.
_SPECIAL_VALUES: dict[str, frozenset[str]] = {
    "deck": frozenset({"filtered", "current"}),
    "tag": frozenset({"none"}),
    "note": frozenset(),
}


@dataclass(frozen=True)
class Term:
    prefix: str
    value: str
    negated: bool = False


@dataclass(frozen=True)
class Problem:
    kind: str  # "deck" / "tag" / "note type"
    prefix: str  # the search prefix as written: deck / tag / note
    value: str  # the name as written, unescaped
    suggestions: tuple[str, ...]


# ---------------------------------------------------------------- parsing


def _split_tokens(query: str) -> list[str]:
    """Split on whitespace and parens outside quotes.

    Quote delimiters are dropped but backslash escapes are kept, so the caller
    can still tell a literal ``\\*`` from a wildcard. Dropping the quotes is
    what makes ``"deck:my deck"`` and ``deck:"my deck"`` - both legal Anki, and
    both meaning the same thing - parse identically.
    """
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(query[i + 1])
            i += 2
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
            i += 1
            continue
        if ch == '"':
            quote = ch
            i += 1
            continue
        if ch.isspace() or ch in "()":
            if buf:
                tokens.append("".join(buf))
                buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _first_unescaped_colon(token: str) -> int | None:
    i = 0
    while i < len(token):
        if token[i] == "\\":
            i += 2
            continue
        if token[i] == ":":
            return i
        i += 1
    return None


def parse_terms(query: str) -> list[Term]:
    """Every ``prefix:value`` term in the query, in order."""
    terms: list[Term] = []
    for token in _split_tokens(query):
        negated = token.startswith("-")
        if negated:
            token = token[1:]
        if not token or token.lower() in _BOOLEAN_KEYWORDS:
            continue
        idx = _first_unescaped_colon(token)
        if not idx:  # None, or 0 = ":foo", which names no prefix
            continue
        terms.append(Term(token[:idx].lower(), token[idx + 1 :], negated))
    return terms


def _unescape(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
            continue
        out.append(value[i])
        i += 1
    return "".join(out)


def _regex_source(value: str) -> str:
    """Anki wildcards: ``*`` any run, ``_`` one character, ``\\`` escapes both."""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(re.escape(value[i + 1]))
            i += 2
            continue
        if ch == "*":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _matches_any(value: str, names: Sequence[str], hierarchical: bool) -> bool:
    # `deck:x` matches x and every x::child; same for tags. Expressed as an
    # optional suffix so the regex engine can backtrack into it.
    suffix = "(?:::.*)?" if hierarchical else ""
    try:
        pattern = re.compile(_regex_source(value) + suffix + r"\Z", re.IGNORECASE)
    except re.error:
        return True  # unparseable: say nothing rather than guess wrong
    return any(pattern.match(name) for name in names)


# ------------------------------------------------------------ suggestions


def _edit_distance(a: str, b: str, cap: int) -> int:
    """Levenshtein, abandoned as soon as every cell exceeds `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        if min(current) > cap:
            return cap + 1
        previous = current
    return previous[-1]


def suggest(value: str, names: Sequence[str]) -> tuple[str, ...]:
    """Plausible real names for `value`, best first."""
    target = value.lower()
    if not target:
        return ()
    scored: list[tuple[int, int, int, str]] = []
    for name in names[:MAX_CANDIDATES_SCANNED]:
        low = name.lower()
        parts = low.split("::")
        if parts[-1] == target:
            # The dogfood case: right leaf, missing parent path.
            tier, dist = 0, 0
        elif target in parts:
            tier, dist = 1, 0
        elif target in low or low in target:
            tier, dist = 2, abs(len(low) - len(target))
        else:
            dist = min(
                [_edit_distance(target, low, MAX_EDIT_DISTANCE)]
                + [_edit_distance(target, part, MAX_EDIT_DISTANCE) for part in parts]
            )
            if dist > MAX_EDIT_DISTANCE:
                continue
            tier = 3
        scored.append((tier, dist, len(name), name))
    scored.sort()
    return tuple(name for _tier, _dist, _len, name in scored[:MAX_SUGGESTIONS])


# -------------------------------------------------------------- diagnosis


def find_unknown_terms(
    query: str,
    *,
    decks: Sequence[str],
    tags: Sequence[str],
    note_types: Sequence[str],
) -> list[Problem]:
    """Terms in `query` naming a deck/tag/note type this collection lacks."""
    pools = {"deck": decks, "tag": tags, "note": note_types}
    problems: list[Problem] = []
    seen: set[tuple[str, str]] = set()
    for term in parse_terms(query):
        checked = _CHECKED.get(term.prefix)
        if checked is None:
            continue
        kind, hierarchical = checked
        literal = _unescape(term.value)
        # A bare wildcard is "any of them", not a name.
        if not literal or set(literal) <= {"*"}:
            continue
        if literal.lower() in _SPECIAL_VALUES[term.prefix]:
            continue
        key = (term.prefix, literal.lower())
        if key in seen:
            continue
        seen.add(key)
        names = pools[term.prefix]
        if _matches_any(term.value, names, hierarchical):
            continue
        problems.append(Problem(kind, term.prefix, literal, suggest(literal, names)))
    return problems


def explain(problems: Sequence[Problem]) -> str:
    """The message the assistant gets instead of a plausible-looking zero."""
    details: list[str] = []
    for problem in problems:
        detail = f'{problem.prefix}:"{problem.value}" — this collection has no {problem.kind} by that name'
        if problem.suggestions:
            names = ", ".join(f'"{name}"' for name in problem.suggestions)
            detail += f"; did you mean {names}?"
        details.append(detail)
    return (
        "This search matched nothing because a term in it names something that "
        "does not exist here. That is why the result is empty — it is NOT "
        "evidence that the collection has no such cards, and reporting it to "
        "the user as an empty deck/tag would be wrong. "
        + " ".join(details)
        + " Fix the term and search again; real names come from deck_tree, "
        "tag_tree, list_note_types, or get_collection_overview."
    )


def diagnose(
    query: str,
    *,
    decks: Sequence[str],
    tags: Sequence[str],
    note_types: Sequence[str],
) -> str | None:
    """`explain(...)` for an empty search, or None if every term resolves."""
    problems = find_unknown_terms(
        query, decks=decks, tags=tags, note_types=note_types
    )
    return explain(problems) if problems else None


def diagnose_collection(col: Any, query: str) -> str | None:
    """`diagnose` against a live collection. Duck-typed, so still aqt-free.

    Never raises: a diagnosis is a nicety layered on top of an already-failed
    search, and must not become a new way for that search to break.
    """
    if not query or not query.strip():
        return None
    try:
        decks = [d.name for d in col.decks.all_names_and_ids()]
        note_types = [nt.name for nt in col.models.all_names_and_ids()]
        try:
            tags = list(col.tags.all())
        except Exception:
            tags = []
        return diagnose(query, decks=decks, tags=tags, note_types=note_types)
    except Exception:
        return None
