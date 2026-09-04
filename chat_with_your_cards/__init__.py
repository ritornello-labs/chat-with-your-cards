"""Chat With Your Cards - a collapsible AI chat dock for Anki.

Importable without Anki (unit tests import .backends etc.); everything
that touches aqt is guarded behind the mw check below.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .controller import ChatController
    from .dock import ChatDock
    from .mcp_server import McpServer
    from .grading import GradingManager
    from .proposals import ProposalManager
    from .stats import StatsCache

DEFAULT_CONFIG: dict[str, Any] = {
    "toggle_shortcut": "Ctrl+J",
    "new_chat_shortcut": "Ctrl+Shift+J",
    "defer_shortcut": "Ctrl+Shift+D",
    # Reviewing affordances (task #32 follow-up): the composer's "Set aside"
    # button, and deferring the on-screen card automatically on every send.
    "defer_button": True,
    "defer_on_send": False,
    "dock_width": 420,
    # The dock is always visible; collapsed means the slim rail. Both persist
    # across sessions (saved at teardown alongside dock_width).
    "dock_collapsed": True,
    "dock_side": "right",
    # Composer vim keybindings (Settings > "Vim keys in composer", or here).
    # vim_mappings are personal [keys, mapped-to, mode] triples with vim
    # `:map` semantics (mode: normal | insert | visual) - the user's vimrc
    # equivalent. Ships EMPTY by default (stock vim behavior; decided
    # 2026-07-14): personal mappings belong in the user's own config, e.g.
    # [["fd", "<Esc>", "insert"], ["j", "gj", "normal"]].
    "vim_mode": False,
    "vim_mappings": [],
    "theme": "teal",
    "backend": "auto",
    "claude_cli_path": "",
    "model": "",
    "effort": "",
    "fast_mode": False,
    "agent_tools": "sandbox",
    "web_access": True,
    "mcp_servers": {},
    "mcp_inherit_user": False,
    # Sandboxed inline widgets (render_widget). Consent gate only - the
    # iframe sandbox is the security boundary and holds regardless.
    "widget_rendering": False,
    "mcp_disabled": [],
    "suggested_questions": True,
    "restore_last_chat": False,
    "open_in_claude_target": "terminal",
    "terminal_app": "",
    "source_fields": {},
    "permission_mode": "default",
    "stats_refresh_minutes": 30,
    "context_token_budget": 8000,
    "auto_accept_cap": 20,
    "write_budget": 200,
    "conventions_prompt": "",
    "custom_instructions": "",
    "created_tag": "ai-created",
    "edited_tag": "ai-edited",
    "session_tag_prefix": "ai-chat-dock::session-",
    "learning_nudge_threshold": 10,
    "learning_nudge_days": 7,
    # What happens when the count/age rule becomes due: open a user-triggered
    # reflection chat (current behavior) or run the analysis in a hidden agent
    # session. Applying the resulting skill diff is a separate decision.
    "learning_run_mode": "chat",
    "skill_update_policy": "review",
    "pins": {},
}

def _norm_agent_tools(value: Any) -> str:
    """Clamp an agent-tools value to a known tier (delegates to the controller's
    single source of truth; imported lazily to keep package import light)."""
    from .controller import _norm_agent_tools as _norm

    return _norm(value)


# Selectable colour palettes (ui styles.css cwyc-theme-* blocks). "teal" is the
# default; anything else falls back to it.
VALID_THEMES = ("teal", "indigo", "evergreen")


def _norm_theme(value: Any) -> str:
    v = str(value).strip()
    return v if v in VALID_THEMES else "teal"

# Visible kickoff message for the skill-review chat (the nudge chip and the
# History note make it clear this starts a NEW chat; the previous one stays
# resumable in History).
SKILL_REVIEW_PROMPT = (
    "I've been editing the cards you created or changed. Use "
    "get_edit_observations to see what I changed, look for recurring "
    "patterns across the observations (the skill-maintenance skill has the "
    "ground rules), explain in plain language what you notice, then propose "
    "an update to the card-authoring skill with propose_skill_update. If "
    "there is no real pattern, just say so."
)

LEARNING_RUN_MODES = ("chat", "background")
SKILL_UPDATE_POLICIES = ("review", "automatic")


def _norm_learning_run_mode(value: Any) -> str:
    mode = str(value).strip()
    return mode if mode in LEARNING_RUN_MODES else "chat"


def _norm_skill_update_policy(value: Any) -> str:
    policy = str(value).strip()
    return policy if policy in SKILL_UPDATE_POLICIES else "review"


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))

USER_FILES = Path(__file__).resolve().parent / "user_files"

# Ceiling for a `long_running` tool (#13). FSRS optimization is minutes on a
# large collection, and the agent must be told to narrow its search rather
# than left waiting forever on a call that will never come back.
LONG_TOOL_TIMEOUT_S = 900.0


@dataclass
class AddonState:
    dock: Optional[ChatDock] = None
    controller: Optional[ChatController] = None
    stats_cache: Optional[StatsCache] = None
    mcp: Optional[McpServer] = None
    proposals: Optional[ProposalManager] = None
    grading: Optional[GradingManager] = None
    transcripts: Any = None
    approvals: Any = None
    learning: Any = None
    learning_timer: Any = None
    background_learning_controller: Optional[ChatController] = None
    background_learning_running: bool = False
    background_learning_job_id: str = ""
    background_learning_observation_ids: list[str] = field(default_factory=list)
    deferral: Any = None
    last_checkpoint: Any = None
    shortcuts: list[Any] = field(default_factory=list)
    web_ready: bool = False
    # Cached count of today's set-aside cards for the tray badge. None = must
    # recount (session start, or a state change that may follow a sync /
    # rollover); mutations refresh it via _push_deferred_list. The cache
    # exists so _push_review_state (fired on EVERY question shown) does not
    # run a collection-wide prop:cdn search per card.
    set_aside_count: Optional[int] = None
    config: dict[str, Any] = field(default_factory=dict)
    # Record-and-push into the chat transcript + webview (set in _setup). Tools
    # use it via _ToolCtx.push_ui to surface UI (e.g. show_image's inline image).
    record_push: Any = None
    # Composer attachments (#15a): files the user picked for the NEXT message,
    # staged under user_files/staging (one pseudo-proposal dir per file so
    # each can be removed individually). Entries: {id, name, kind, size, path}.
    # Cleared after the message they ride on is sent, and on new chat.
    attachments: list[dict[str, Any]] = field(default_factory=list)


state = AddonState()

try:
    from aqt import mw
except ImportError:  # running outside Anki (unit tests)
    mw = None  # type: ignore[assignment]


def toggle_chat_focus() -> None:
    from . import shortcuts as shortcuts_mod

    shortcuts_mod.toggle_chat_focus(state)


def new_chat() -> None:
    from . import shortcuts as shortcuts_mod

    shortcuts_mod.new_chat(state)


def _install_tools_menu(config: dict[str, Any]) -> None:
    """Add a clearly-labeled 'Chat With Your Cards' submenu to Anki's Tools
    menu, with verbs and their shortcuts — instead of a bare add-on-title
    checkbox that silently toggled dock visibility (which also diverged from
    what the Ctrl+J chord does). Both entries drive the same actions as the
    chords, so the menu and the keyboard agree."""
    from aqt.qt import QAction, QKeySequence, QMenu

    def hint(seq: str) -> str:
        return QKeySequence(seq).toString(QKeySequence.SequenceFormat.NativeText)

    menu = QMenu("Chat With Your Cards", mw)
    open_action = QAction(f"Toggle chat\t{hint(config['toggle_shortcut'])}", mw)
    open_action.triggered.connect(lambda *_a: toggle_chat_focus())
    new_action = QAction(f"New chat\t{hint(config['new_chat_shortcut'])}", mw)
    new_action.triggered.connect(lambda *_a: new_chat())
    menu.addAction(open_action)
    menu.addAction(new_action)
    mw.form.menuTools.addMenu(menu)


DEFER_SHORTCUT = "Ctrl+Shift+D"


def defer_current_card() -> str:
    """Push the card on screen to later in this session. Returns a status
    string for the caller to surface (a tooltip, or the agent's tool result).

    Deliberately manual as well as agent-driven (user, 2026-07-27): the
    judgement "not this one right now" is usually the reviewer's own, and
    waiting for an assistant turn to make it would be absurd."""
    if state.deferral is None or mw.col is None:
        return "Deferring is unavailable."
    card = getattr(mw.reviewer, "card", None)
    if mw.state != "review" or card is None:
        return "No card is being reviewed."
    card_id = int(card.id)
    state.deferral.defer(card_id)
    # Move on immediately - the point is to get it off the screen.
    mw.reviewer.nextCard()
    _notify_deferred(card_id)
    return "Card deferred - it comes back later in this session."


def bring_back_deferred() -> str:
    """Show a deferred card next (session-only pin), newest first."""
    if state.deferral is None or mw.col is None:
        return "Deferring is unavailable."
    ids = state.deferral.deferred_card_ids()
    if not ids:
        return "No deferred cards."
    # deferred_card_ids() lists newest-set-aside first (task #33).
    state.deferral.show_next(ids[0])
    _notify_deferred()
    return "It will be the next card."


def unbury_all_deferred() -> str:
    """The tray's "Bring all back": every set-aside card returns to the queue
    in its natural order (no pin - "all of them next" is not a thing)."""
    if state.deferral is None or mw.col is None:
        return "Deferring is unavailable."
    ids = state.deferral.deferred_card_ids()
    if not ids:
        return "No cards are set aside."
    for cid in ids:
        state.deferral.undefer(cid)
    _refresh_after_unbury()
    _notify_deferred()
    return "Card brought back." if len(ids) == 1 else f"{len(ids)} cards brought back."


def _refresh_after_unbury() -> None:
    """Mid-card, the next fetch picks unburied cards up by itself; anywhere
    else (deck list, overview - including the state after finishing a deck)
    the due counts on screen are now stale, so redraw."""
    if mw.state == "review" and getattr(mw.reviewer, "card", None) is not None:
        return
    mw.reset()


def _bind_defer_shortcut() -> None:
    """Bound alongside the other chords so a config edit rebinds all three."""
    from aqt.qt import QKeySequence, QShortcut
    from aqt.utils import tooltip

    chord = str(state.config.get("defer_shortcut", DEFER_SHORTCUT))
    shortcut = QShortcut(QKeySequence(chord), mw)
    shortcut.activated.connect(lambda: tooltip(defer_current_card()))
    state.shortcuts.append(shortcut)


def _tooltip_result(text: str) -> None:
    from aqt.utils import tooltip

    tooltip(text)


def undo_defer(card_id: int) -> str:
    """The undo chip: unmark the card and put it straight back on screen.

    nextCard() is safe on the displaced card - it was never answered, so it
    stays due and simply comes round again after the recalled one."""
    if state.deferral is None or mw.col is None:
        return "Deferring is unavailable."
    state.deferral.show_next(int(card_id))
    if mw.state == "review":
        mw.reviewer.nextCard()
    else:
        # From the tray outside an active review: the card is unburied and
        # pinned for when review resumes; redraw the stale due counts now.
        mw.reset()
    _notify_deferred()
    return "It's back."


def _push_review_state() -> None:
    """Tell the dock whether a review card is on screen, so the composer's
    "Set aside" button only shows when there is something to set aside.
    Carries the set-aside count for the tray badge (cached - see AddonState)."""
    if state.dock is None:
        return
    card = getattr(mw.reviewer, "card", None) if mw.state == "review" else None
    count = state.set_aside_count
    if count is None and state.deferral is not None and mw.col is not None:
        count = len(state.deferral.deferred_card_ids())
        state.set_aside_count = count
    state.dock.bridge.push(
        {
            "type": "review_state",
            "reviewing": card is not None,
            "card_id": int(card.id) if card is not None else None,
            "set_aside_count": int(count or 0),
        }
    )


def _deferred_entries() -> list[dict[str, Any]]:
    """Card summaries for the set-aside tray, newest-set-aside first. A card
    that fails to summarize entirely is skipped rather than sinking the list."""
    from .deferral import card_summary

    if state.deferral is None or mw.col is None:
        return []
    entries: list[dict[str, Any]] = []
    for cid in state.deferral.deferred_card_ids():
        try:
            entries.append(card_summary(mw.col, cid))
        except Exception:
            continue
    return entries


def _push_attachments() -> None:
    """Composer attachment chips (#15a). Transient chrome, never recorded:
    the UI shows name/kind/size; the staged PATH is agent-facing only and
    rides the message text at send."""
    if state.dock is None:
        return
    state.dock.bridge.push(
        {
            "type": "attachments",
            "items": [
                {k: entry[k] for k in ("id", "name", "kind", "size")}
                for entry in state.attachments
            ],
        }
    )


def _stage_composer_files(paths: list[Any]) -> dict[str, Any]:
    """Validate + stage user-picked files for the NEXT message (#15a).

    Each file gets its own pseudo-proposal staging dir (so one chip can be
    removed without touching the others); the existing startup sweep cleans
    abandoned dirs. Per-file errors are collected, never fatal for the batch.
    """
    import os
    import uuid

    from .media_staging import MediaError, MediaStaging

    staging = MediaStaging(USER_FILES / "staging")
    errors: list[str] = []
    added = 0
    for raw in paths:
        pseudo_id = f"composer-{uuid.uuid4().hex[:10]}"
        try:
            staged = staging.stage(pseudo_id, [{"path": str(raw)}])
        except MediaError as exc:
            errors.append(f"{os.path.basename(str(raw))}: {exc}")
            continue
        item = staged[0]
        state.attachments.append(
            {
                "id": pseudo_id,
                "name": item.filename,
                "kind": item.kind,
                "size": item.size,
                "path": str(item.path),
            }
        )
        added += 1
    _push_attachments()
    return {"added": added, "errors": errors}


def _remove_composer_attachment(attachment_id: str) -> None:
    from .media_staging import MediaStaging

    staging = MediaStaging(USER_FILES / "staging")
    kept: list[dict[str, Any]] = []
    for entry in state.attachments:
        if entry["id"] == str(attachment_id):
            staging.discard(entry["id"])
        else:
            kept.append(entry)
    state.attachments = kept
    _push_attachments()


def _clear_composer_attachments(discard_files: bool) -> None:
    """After send the files must SURVIVE (the agent may propose with them a
    turn later; the 7-day sweep is the backstop) - only the pending list
    clears. New chat discards outright."""
    if discard_files:
        from .media_staging import MediaStaging

        staging = MediaStaging(USER_FILES / "staging")
        for entry in state.attachments:
            staging.discard(entry["id"])
    state.attachments = []
    _push_attachments()


def _attachment_message_block(entries: list[dict[str, Any]]) -> str:
    """The agent-facing description of what the user attached (#15a/b)."""
    lines = [
        f"- {entry['path']} ({entry['kind']}, {max(1, entry['size'] // 1024)} KB)"
        for entry in entries
    ]
    notes = [
        "To put one on a card, pass its path in propose_note's media[] "
        "(reference images as <img src=\"name\">, audio/video as "
        "[sound:name]). For a standalone asset use store_media_asset."
    ]
    images = [e for e in entries if e.get("kind") == "image"]
    if images:
        oversized = [
            e["name"] for e in images if int(e.get("size", 0)) > IMAGE_BLOCK_MAX_BYTES
        ]
        if len(oversized) < len(images):
            notes.append("The attached image(s) are also shown to you inline.")
        if oversized:
            notes.append(
                "Too large to show inline (path only): " + ", ".join(oversized)
            )
    documents = [e for e in entries if e.get("kind") == "document"]
    if documents:
        too_big = [
            e["name"]
            for e in documents
            if int(e.get("size", 0)) > DOCUMENT_BLOCK_MAX_BYTES
        ]
        if len(too_big) < len(documents):
            notes.append(
                "The attached PDF(s) are shown to you inline - read them "
                "directly. They are context material, not card media."
            )
        if too_big:
            notes.append(
                "Too large to show inline (path only): "
                + ", ".join(too_big)
                + ". Read these from the path with your file tools; if file "
                "tools are off in this session, say so instead of guessing at "
                "their contents."
            )
    return (
        "<user-attachments>\nThe user attached these files for this message:\n"
        + "\n".join(lines)
        + "\n"
        + "\n".join(notes)
        + "\n</user-attachments>"
    )


def _handle_dropped_paths(paths: list[str]) -> int:
    """Files dragged from the OS onto the dock (#15). Paths arrive at the Qt
    layer, so no bytes ever cross the webview bridge; same staging as the
    picker. Returns how many staged."""
    local = [p for p in paths if p and Path(p).is_file()]
    if not local:
        return 0
    result = _stage_composer_files(local)
    if result["errors"]:
        _tooltip_result("; ".join(result["errors"][:2]))
    return int(result["added"])


def _install_drop_filter() -> None:
    """Intercept OS file drops over the dock's webview (#15). QtWebEngine
    routes drag events to a render child, so the filter watches the whole
    webview subtree via the focus proxy; non-file drags pass through
    untouched (text drags into the composer still work)."""
    if state.dock is None:
        return
    from aqt.qt import QEvent, QObject

    web = state.dock.web

    class _DropFilter(QObject):
        def eventFilter(self, _obj: Any, event: Any) -> bool:  # noqa: N802
            etype = event.type()
            if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                mime = event.mimeData()
                if mime.hasUrls() and any(u.isLocalFile() for u in mime.urls()):
                    event.acceptProposedAction()
                    return True
                return False
            if etype == QEvent.Type.Drop:
                mime = event.mimeData()
                if mime.hasUrls():
                    paths = [
                        u.toLocalFile() for u in mime.urls() if u.isLocalFile()
                    ]
                    if paths and _handle_dropped_paths(paths):
                        event.acceptProposedAction()
                        return True
                return False
            return False

    drop_filter = _DropFilter(web)
    web.setAcceptDrops(True)
    web.installEventFilter(drop_filter)
    proxy = web.focusProxy()
    if proxy is not None:
        proxy.installEventFilter(drop_filter)


def _attach_pasted(msg: dict[str, Any]) -> None:
    """A pasted image from the composer (#15): the one transport where bytes
    legitimately cross the bridge (a clipboard screenshot has no path) -
    the same route Anki's own editor paste uses. Size is enforced twice:
    the data-URL budget here, the staging byte cap after decode."""
    import base64
    import re as _re
    import tempfile
    import time as _time

    data = str(msg.get("data", ""))
    match = _re.match(r"^data:(image/[a-z+.-]+);base64,(.+)$", data, _re.S)
    if match is None:
        _tooltip_result("Could not read the pasted image")
        return
    if len(data) > 12_000_000:  # ~9 MB decoded; staging caps at 8 MB anyway
        _tooltip_result("Pasted image is too large (8 MB cap)")
        return
    mime = match.group(1)
    from .media_staging import IMAGE_MIME_BY_EXT

    ext = next(
        (e for e, m in IMAGE_MIME_BY_EXT.items() if m == mime), ".png"
    )
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except Exception:
        _tooltip_result("Could not decode the pasted image")
        return
    tmp_dir = Path(tempfile.mkdtemp(prefix="cwyc-paste-"))
    name = f"pasted-{_time.strftime('%Y%m%d-%H%M%S')}{ext}"
    path = tmp_dir / name
    path.write_bytes(payload)
    result = _stage_composer_files([str(path)])
    if result["errors"]:
        _tooltip_result("; ".join(result["errors"][:2]))


IMAGE_BLOCK_MAX_BYTES = 3_750_000  # the API's per-image request budget
# Documents ride the same base64 path and so pay the same ~33% encoding
# overhead. Kept well under the API's document ceiling: a PDF large enough to
# need more is better read from disk with file tools than inlined into every
# subsequent turn's context.
DOCUMENT_BLOCK_MAX_BYTES = 8_000_000


def _context_blocks(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attached images and PDFs as stream-json content blocks (#15b), so the
    agent SEES them rather than only knowing their paths. Oversized files stay
    path-only (the block text says which).

    Documents were held back until the shape was verified end-to-end rather
    than assumed: the CLI *parses* an unsupported block without complaint, so
    "it did not crash" proved nothing. Probed 2026-08-02 against CLI 2.1.220 -
    a base64 `document` block carrying a hand-built PDF came back with the
    exact token embedded in that PDF, `is_error: false`. Same wire shape as
    images, different `type` and media_type.
    """
    import base64
    import os as _os

    from .media_staging import MIME_BY_EXT

    kinds = {"image": ("image", IMAGE_BLOCK_MAX_BYTES),
             "document": ("document", DOCUMENT_BLOCK_MAX_BYTES)}
    blocks: list[dict[str, Any]] = []
    for entry in entries:
        spec = kinds.get(str(entry.get("kind")))
        if spec is None:
            continue
        block_type, cap = spec
        if int(entry.get("size", 0)) > cap:
            continue
        try:
            payload = Path(entry["path"]).read_bytes()
        except OSError:
            continue
        mime = MIME_BY_EXT.get(_os.path.splitext(entry["name"])[1].lower())
        if not mime:
            continue
        blocks.append(
            {
                "type": block_type,
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(payload).decode("ascii"),
                },
            }
        )
    return blocks


def _pick_composer_attachments() -> None:
    """Native file picker (#15a): paths stay on the Python side, so no file
    bytes ever cross the webview bridge."""
    from aqt.qt import QFileDialog

    from .media_staging import MIME_BY_EXT

    exts = " ".join(f"*{ext}" for ext in sorted(MIME_BY_EXT))
    paths, _selected_filter = QFileDialog.getOpenFileNames(
        mw,
        "Attach files to your next message",
        "",
        f"Media files ({exts})",
    )
    if not paths:
        return
    result = _stage_composer_files(list(paths))
    if result["errors"]:
        _tooltip_result("; ".join(result["errors"][:2]))


def _push_deferred_list() -> None:
    if state.dock is None:
        return
    entries = _deferred_entries()
    state.set_aside_count = len(entries)
    state.dock.bridge.push({"type": "deferred_list", "entries": entries})


def _notify_deferred(card_id: int | None = None) -> None:
    """UI freshness after any deferral mutation: the transient undo chip
    (when a card was just set aside) plus the tray list and badge count."""
    if state.dock is None:
        return
    if card_id is not None:
        # Pushed, not recorded: transient chrome, not conversation.
        state.dock.bridge.push({"type": "card_deferred", "card_id": int(card_id)})
    _push_deferred_list()


def _install_defer_entry_points(config: dict[str, Any]) -> None:
    """A chord and a right-click item on the card itself - the latter is where
    a reviewer actually looks for a per-card action.

    The two Tools-menu entries this used to add ("Defer this card" / "Bring
    back a deferred card") were REMOVED 2026-08-02 at the user's request. The
    operation is a manual bury, and Anki's own reviewer already offers burying
    where people expect to find it; a second pair of top-level menu items for
    the same thing under a different name was surface without capability. The
    chord and the right-click entry stay - those are the ones actually used
    mid-review - and the dock's tray remains the place to see and recall what
    the session has set aside.
    """
    from aqt import gui_hooks
    from aqt.utils import tooltip

    def run(action: Any) -> None:
        tooltip(action())

    _bind_defer_shortcut()

    def _context_menu(_webview: Any, menu: Any) -> None:
        entry = menu.addAction("Bury this card (manual, back later today)")
        entry.triggered.connect(lambda *_a: run(defer_current_card))

    gui_hooks.reviewer_will_show_context_menu.append(_context_menu)


def _setup() -> None:
    from aqt import gui_hooks

    from . import dock as dock_mod
    from . import shortcuts as shortcuts_mod
    from .context import build_system_prompt
    from .controller import ChatController
    from .grading import GradingManager
    from .proposals import ProposalManager
    from .skills import load_conventions
    from .stats import StatsCache

    config = dict(DEFAULT_CONFIG)
    config.update(mw.addonManager.getConfig(__name__) or {})
    state.config = config

    state.stats_cache = StatsCache(
        USER_FILES / "stats_cache.json",
        refresh_minutes=int(config["stats_refresh_minutes"]),
    )
    state.stats_cache.start()

    state.dock = dock_mod.create_dock(
        dock_width=int(config["dock_width"]),
        collapsed=bool(config.get("dock_collapsed", True)),
        side=str(config.get("dock_side", "right")),
    )
    _install_drop_filter()

    from .transcripts import TranscriptStore

    state.transcripts = TranscriptStore(USER_FILES / "transcripts")

    dock = state.dock

    def recording_push(payload: dict[str, Any]) -> None:
        # One pipe to the webview that also feeds the chat transcript
        # (TranscriptStore ignores payload types not worth replaying).
        if state.transcripts is not None:
            state.transcripts.record(payload)
        dock.bridge.push(payload)

    state.record_push = recording_push

    from .learning import LearningStore
    from .skills import (
        materialize_agent_environment,
        materialize_agent_skills,
        materialize_conventions_agent_skill,
    )

    # State the dock's hard tool limits where the agent AND its subagents see
    # them (agent-home/CLAUDE.md, loaded from cwd), so the agent stops trying
    # to write files / spawn subagents to do so (dogfood 2026-07-12).
    materialize_agent_environment(
        USER_FILES / "agent-home", str(config.get("agent_tools", "sandbox"))
    )
    conventions = load_conventions(USER_FILES, str(config.get("conventions_prompt", "")))
    # Mirror conventions into the agent-home skills dir so the harness
    # auto-discovers them (COMPLIANCE.md rule 3) instead of them being
    # inlined into --append-system-prompt - see context.build_system_prompt.
    materialize_conventions_agent_skill(USER_FILES / "agent-home", conventions)
    card_skill_path = materialize_agent_skills(USER_FILES / "agent-home")
    state.learning = LearningStore(USER_FILES / "learning", card_skill_path)
    from aqt.qt import QTimer

    state.learning_timer = QTimer(mw)
    state.learning_timer.setInterval(60 * 60 * 1000)
    state.learning_timer.timeout.connect(_scan_learning)
    state.learning_timer.start()

    state.proposals = ProposalManager(
        get_col=lambda: mw.col,
        push=recording_push,
        config=config,
        save_pins=_save_pins,
        after_write=_refresh_reviewer,
        checkpoint=_backup_checkpoint,
        observe=_learning_observe,
        apply_skill=_apply_skill_update,
        after_deck_change=_refresh_deck_ui,
        apply_skill_create=_apply_new_skill,
        list_skill_names=_agent_skill_names,
        media_staging=_build_media_staging(),
        sync_now=lambda: mw.onSync(),
    )
    state.grading = GradingManager(
        get_col=lambda: mw.col,
        push=recording_push,
        config=config,
        after_change=_refresh_after_grading,
    )

    def system_prompt() -> str:
        return build_system_prompt(
            permission_mode=str(config["permission_mode"]),
            agent_tools=str(config.get("agent_tools", "sandbox")),
            pins=state.proposals.pins if state.proposals else None,
            custom_instructions=str(config.get("custom_instructions", "")),
        )

    state.controller = ChatController(
        push=recording_push,
        config=config,
        system_prompt_builder=system_prompt,
        ensure_mcp=_ensure_mcp,
        workdir=USER_FILES / "agent-home",
        proposals=state.proposals,
        grading=state.grading,
        transcripts=state.transcripts,
    )
    _wire_bridge()
    shortcuts_mod.register_shortcuts(state)
    _install_tools_menu(config)

    # "Not now" for the card in front of you (task #32). Installed before any
    # review starts so the reviewer wrap is in place for the first card.
    from .deferral import DeferralManager

    state.deferral = DeferralManager(lambda: mw.col)
    try:
        from aqt.reviewer import Reviewer

        state.deferral.install(Reviewer)
    except Exception:
        state.deferral = None  # never let this break reviewing
    if state.deferral is not None:
        _install_defer_entry_points(config)

    # Live context chip: refresh as the user moves between screens/cards.
    def _chip(*_args: Any) -> None:
        if state.controller is not None:
            state.controller.push_context_chip()
        _push_review_state()

    def _state_chip(*_args: Any) -> None:
        # A screen change can follow a sync or day rollover, either of which
        # moves cards in or out of the set-aside state behind our back - so
        # the badge count must be recounted, not trusted (task #33).
        state.set_aside_count = None
        _chip()

    gui_hooks.reviewer_did_show_question.append(_chip)
    gui_hooks.state_did_change.append(_state_chip)


class _ToolCtx:
    @property
    def col(self) -> Any:
        return mw.col

    @property
    def deferral(self) -> Any:
        return state.deferral

    @property
    def deferral_changed(self) -> Any:
        """Tools report deferral mutations here so the dock's undo chip and
        set-aside tray stay fresh (task #33). Optional by design - tools
        getattr it, so tests with bare contexts still run."""
        return _notify_deferred

    @property
    def stats(self) -> dict[str, Any] | None:
        return state.stats_cache.stats if state.stats_cache else None

    @property
    def proposals(self) -> Any:
        return state.proposals

    @property
    def grading(self) -> Any:
        return state.grading

    @property
    def config(self) -> dict[str, Any]:
        return state.config

    @property
    def learning(self) -> Any:
        return state.learning

    def push_ui(self, payload: dict[str, Any]) -> None:
        # Tools run on the main thread (execute_tool marshals via run_on_main),
        # so record + push straight through. No-op before setup wires it.
        if state.record_push is not None:
            state.record_push(payload)


def _ensure_mcp() -> tuple[str, str]:
    """Start the MCP server on first use; returns (url, bearer token)."""
    if state.mcp is None:
        from .mcp_server import McpServer, tool_specs_for_mcp
        from .tools import build_registry

        registry = build_registry()
        ctx = _ToolCtx()
        mode = str(state.config.get("permission_mode", "default"))
        read_only = mode == "read-only"
        trusted = mode in {"trusted-writes", "full-collection"}

        specs_by_name = {spec.name: spec for spec in registry.specs(include_trusted=True)}

        from . import approvals as approvals_mod
        from .approvals import ApprovalBroker

        def push_on_main(payload: dict[str, Any]) -> None:
            mw.taskman.run_on_main(
                lambda: state.dock.bridge.push(payload) if state.dock else None
            )

        def pending_ttl_s() -> float:
            # Read live: editing the config must apply to the next prompt,
            # not require a restart.
            raw = state.config.get(
                "approval_timeout_minutes", approvals_mod.PENDING_TTL_MINUTES
            )
            try:
                minutes = float(raw)
            except (TypeError, ValueError):
                minutes = approvals_mod.PENDING_TTL_MINUTES
            return max(0.0, minutes) * 60.0

        state.approvals = ApprovalBroker(push_on_main, pending_ttl_s=pending_ttl_s)

        def execute_tool(name: str, args: dict[str, Any]) -> Any:
            # The MCP server is the security boundary: enforce the LIVE
            # permission mode here, not just in what tools are advertised.
            # This is what makes runtime mode switching sound.
            spec = specs_by_name.get(name)
            live_mode = str(state.config.get("permission_mode", "default"))
            if spec is not None and spec.writes and live_mode == "read-only":
                raise PermissionError(
                    "this session is read-only; the user must switch the "
                    "permission mode to allow writes"
                )
            if spec is not None and spec.trusted_only and live_mode not in {
                "trusted-writes",
                "full-collection",
            }:
                raise PermissionError(
                    f"{name} is only available in Trusted writes or Full collection mode"
                )
            if (
                spec is not None
                and not spec.writes
                and live_mode == "ask-each-read"
            ):
                # Blocks this MCP thread on the inline Allow/Deny chip.
                # Writes are not double-gated: proposals review them anyway.
                import json as _json

                summary = _json.dumps(args, ensure_ascii=False)[:120]
                verdict = state.approvals.request(name, summary)
                if verdict == approvals_mod.PENDING:
                    # NOT a refusal: the prompt is still on screen, unanswered.
                    # Saying so plainly is the whole point - handed a bare
                    # timeout here, the agent told a user their collection was
                    # busy mid-sync while four prompts sat waiting (dogfood
                    # 2026-07-23).
                    raise PermissionError(
                        f"Approval pending: the user has not answered the prompt for "
                        f"{name}. This call did NOT run. Mention once that a prompt "
                        "needs their answer, then DROP IT - do not retry, do not work "
                        "around it with another tool, and do not carry it forward as "
                        "outstanding work in later replies. If they approve it you "
                        "will be told explicitly and can pick it up then; if they "
                        "never do, it is abandoned and re-raising it is just nagging."
                    )
                if verdict == approvals_mod.DENY:
                    raise PermissionError(f"the user declined this {name} call")
            box: dict[str, Any] = {}
            done = threading.Event()

            if spec is not None and spec.long_running:
                # Seconds-to-minutes of compute (FSRS optimization, #13). The
                # main thread would freeze Anki and blow the 15s wait below,
                # and a thread of our own must never touch mw.col (SAFETY.md
                # hazard 19). QueryOp runs it on Anki's own single collection
                # worker with a progress dialog the user can see.
                def start_long() -> None:
                    from aqt.operations import QueryOp

                    if mw.col is None:
                        box["error"] = RuntimeError("collection is not open")
                        done.set()
                        return

                    def work(_col: Any) -> Any:
                        return registry.call(ctx, name, args)

                    def succeeded(value: Any) -> None:
                        box["result"] = value
                        done.set()

                    def failed(exc: Exception) -> None:
                        box["error"] = exc
                        done.set()

                    (
                        QueryOp(parent=mw, op=work, success=succeeded)
                        .failure(failed)
                        .with_progress(spec.progress_label)
                        .run_in_background()
                    )

                mw.taskman.run_on_main(start_long)
                if not done.wait(timeout=LONG_TOOL_TIMEOUT_S):
                    raise TimeoutError(
                        f"{name} did not finish within "
                        f"{int(LONG_TOOL_TIMEOUT_S)}s; narrow it with a `search` "
                        "so it has less review history to chew through"
                    )
                if "error" in box:
                    raise box["error"]
                return box["result"]

            def run() -> None:
                try:
                    if mw.col is None:
                        raise RuntimeError("collection is not open")
                    box["result"] = registry.call(ctx, name, args)
                except Exception as exc:  # noqa: BLE001 - marshaled to caller
                    box["error"] = exc
                finally:
                    done.set()

            mw.taskman.run_on_main(run)
            if not done.wait(timeout=15):
                raise TimeoutError("Anki main thread did not respond within 15s")
            if "error" in box:
                raise box["error"]
            return box["result"]

        state.mcp = McpServer(
            tool_specs=tool_specs_for_mcp(
                registry.specs(include_writes=not read_only, include_trusted=trusted)
            ),
            execute_tool=execute_tool,
        )
        state.mcp.start()
        _write_project_mcp_json(state.mcp.url, state.mcp.token)
    return state.mcp.url, state.mcp.token


def _write_project_mcp_json(url: str, token: str) -> None:
    """Write agent-home/.mcp.json so a terminal Claude Code opened in that
    directory (the open-in-Claude-Code flow) discovers the anki tools too.
    Rewritten each Anki run because the port and token rotate."""
    import json as _json

    from .backends.claude_cli import MCP_REQUEST_TIMEOUT_MS as _MCP_REQUEST_TIMEOUT_MS

    path = USER_FILES / "agent-home" / ".mcp.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _json.dumps(
                {
                    "mcpServers": {
                        "anki": {
                            "type": "http",
                            "url": url,
                            "headers": {"Authorization": f"Bearer {token}"},
                            # Same per-request ceiling the dock's own session
                            # uses; ask-each-read holds requests open.
                            "timeout": _MCP_REQUEST_TIMEOUT_MS,
                        }
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)  # carries the bearer token
    except OSError:
        pass


def _wire_bridge() -> None:
    from . import shortcuts as shortcuts_mod

    assert state.dock is not None and state.controller is not None
    bridge = state.dock.bridge
    controller = state.controller

    bridge.on("ready", lambda _msg: _mark_web_ready())
    def _on_send(msg: dict[str, Any]) -> None:
        # "Always defer": sending a question about the card is the moment the
        # card stops being reviewable, so get it off the screen while the
        # agent thinks (user, 2026-07-28). Deferred BEFORE the send so the
        # next card is up as the reply streams.
        if (
            bool(state.config.get("defer_on_send", False))
            and state.deferral is not None
            and mw.state == "review"
            and getattr(mw.reviewer, "card", None) is not None
        ):
            defer_current_card()
        text = str(msg.get("text", ""))
        extra_blocks: list[dict[str, Any]] = []
        if state.attachments:
            # Paths ride the message (#15a); attached images ALSO ride as
            # inline image blocks so the agent sees them (#15b). The files
            # stay staged - the agent may only propose with them a turn later.
            extra_blocks = _context_blocks(state.attachments)
            text = text + "\n\n" + _attachment_message_block(state.attachments)
            _clear_composer_attachments(discard_files=False)
        controller.send_user_message(text, extra_blocks=extra_blocks or None)

    bridge.on("send", _on_send)
    bridge.on("pick_attachments", lambda _msg: _pick_composer_attachments())
    bridge.on("attach_pasted", _attach_pasted)
    bridge.on(
        "remove_attachment",
        lambda msg: _remove_composer_attachment(str(msg.get("id", ""))),
    )
    bridge.on("defer_current", lambda _msg: _tooltip_result(defer_current_card()))
    bridge.on(
        "undo_defer",
        lambda msg: _tooltip_result(undo_defer(int(msg.get("card_id", 0)))),
    )
    # The set-aside tray (task #33): full list on demand, bring-all-back.
    # Per-card bring-back reuses undo_defer above.
    bridge.on("get_deferred", lambda _msg: _push_deferred_list())
    bridge.on("unbury_all_deferred", lambda _msg: _tooltip_result(unbury_all_deferred()))
    bridge.on("cancel", lambda _msg: controller.cancel())

    def _on_new_chat(_msg: dict[str, Any]) -> None:
        # A new chat abandons the pending message; its attachments go too.
        _clear_composer_attachments(discard_files=True)
        new_chat()

    bridge.on("new_chat", _on_new_chat)
    bridge.on("toggle_focus", lambda _msg: toggle_chat_focus())
    bridge.on("focus_reviewer", lambda _msg: shortcuts_mod.focus_main_window())

    proposals = state.proposals
    assert proposals is not None

    def _accept_proposal(msg: dict[str, Any]) -> None:
        decision = proposals.accept(msg)
        if decision is not None:
            controller.note_proposal_decision(decision)

    def _reject_proposal(msg: dict[str, Any]) -> None:
        decision = proposals.reject(msg)
        if decision is not None:
            controller.note_proposal_decision(decision)

    bridge.on("proposal_accept", _accept_proposal)
    bridge.on("proposal_revise", proposals.revise)
    bridge.on("proposal_reject", _reject_proposal)
    bridge.on("proposal_supersede", proposals.supersede)
    bridge.on("proposal_revert", proposals.revert)
    bridge.on("proposal_readd", proposals.readd)
    bridge.on("proposal_restore", proposals.restore)
    bridge.on("proposal_preview", proposals.preview_request)

    def _open_proposal_preview(msg: dict[str, Any]) -> None:
        # The large preview window (#2): render through the same ephemeral
        # path as the in-card preview, then hand off to the Qt dialog.
        previews = proposals.render_for_window(
            str(msg.get("id", "")), msg.get("fields") or None
        )
        if not previews:
            _tooltip_result("Nothing to preview for this proposal")
            return
        from .preview_window import show_preview

        show_preview("Proposal preview — Chat With Your Cards", previews)

    bridge.on("proposal_preview_window", _open_proposal_preview)
    grading = state.grading
    assert grading is not None
    bridge.on("grading_accept", grading.accept)
    bridge.on("grading_reject", grading.reject)
    bridge.on("grading_make_available", grading.make_available_from_failure)
    bridge.on("undo_session", lambda _msg: proposals.undo_session())
    bridge.on("set_pins", lambda msg: proposals.set_pins(msg.get("pins") or {}))
    bridge.on("open_session_browser", lambda _msg: _open_session_browser())
    bridge.on(
        "open_in_claude",
        lambda msg: _open_in_claude_code(str(msg.get("target", "terminal"))),
    )
    bridge.on(
        "set_open_in_claude_target",
        lambda msg: _set_open_target(str(msg.get("target", "terminal"))),
    )
    bridge.on("list_history", lambda _msg: controller.push_history_list())
    bridge.on("load_history", lambda msg: controller.load_history(str(msg.get("id", ""))))
    bridge.on("run_doctor", lambda _msg: _run_doctor())
    bridge.on("recheck_backend", lambda _msg: _recheck_backend())
    bridge.on("start_skill_review", lambda _msg: _start_skill_review())
    bridge.on(
        "show_background_skill_update",
        lambda msg: _show_background_skill_update(str(msg.get("proposal_id", ""))),
    )
    bridge.on(
        "tool_approval_response",
        lambda msg: state.approvals.respond(msg) if state.approvals else None,
    )
    bridge.on("set_agent", _set_agent)
    bridge.on("set_permission_mode", _set_permission_mode)
    bridge.on(
        "set_dock_expanded",
        lambda msg: state.dock.set_expanded(bool(msg.get("expanded", True)))
        if state.dock is not None
        else None,
    )
    bridge.on("set_setting", _set_setting)
    bridge.on("open_addon_config", lambda _msg: _open_addon_config())
    # Saving the add-on's config in Anki's built-in editor re-pushes the live
    # preferences (vim mode/mappings, theme, dock side, ...) so an edit applies
    # without an Anki restart.
    mw.addonManager.setConfigUpdatedAction(__name__, _on_config_updated)


def _mark_web_ready() -> None:
    state.web_ready = True
    if state.dock is not None:
        state.dock.web.eval("window.chatUI && window.chatUI.ackReady();")
    if state.proposals is not None:
        state.proposals.push_ui_state()
    if state.controller is not None:
        state.controller.push_agent_state()
    if state.dock is not None:
        state.dock.bridge.push(
            {
                "type": "ui_config",
                "suggested_questions": bool(
                    state.config.get("suggested_questions", True)
                ),
                "open_in_claude_target": _norm_open_target(
                    state.config.get("open_in_claude_target", "terminal")
                ),
            }
        )
    if state.controller is not None:
        state.controller.push_context_chip()
    # Authoritative dock + settings state (the body fragment planted the
    # initial dock state for first paint; this keeps a reloaded webview honest
    # and feeds the Settings panel).
    if state.dock is not None:
        state.dock.push_state(animating=False)
    _push_settings()
    _push_collection_meta()
    _push_review_state()
    # Seed the tray so its badge and list are right from the first paint,
    # even for cards set aside earlier today before a restart, or on another
    # device (the marker syncs; it expires at the day rollover).
    _push_deferred_list()
    _scan_learning()
    # Optionally reopen the last chat where the user left off (default off:
    # a fresh chat each launch). Runs after the initial UI state is pushed.
    if state.controller is not None and bool(state.config.get("restore_last_chat")):
        state.controller.restore_last_chat()


def _push_collection_meta() -> None:
    """Feed the pins selectors: deck names, note types + fields, tags."""
    if state.dock is None or mw.col is None:
        return
    try:
        decks = sorted(mw.col.decks.all_names())
        note_types = [
            {"name": m["name"], "fields": [f["name"] for f in m["flds"]]}
            for m in (mw.col.models.by_name(nt.name) for nt in mw.col.models.all_names_and_ids())
            if m is not None
        ]
        tags = sorted(mw.col.tags.all())
    except Exception:
        return
    state.dock.bridge.push(
        {
            "type": "collection_meta",
            "decks": decks,
            "note_types": note_types,
            "tags": tags,
        }
    )


def _open_addon_config() -> None:
    """Settings > 'Edit config…': open Anki's config editor directly on THIS
    add-on's config (the vimrc equivalent - vim_mappings - lives there, plus
    every advanced key documented in config.md). Anki 25.09 has no
    mw.onAddons (first cut failed silently on that - dogfood 2026-07-14);
    the real path is AddonsDialog(mw.addonManager) + ConfigEditor(dlg, addon,
    conf), both self-showing. Any failure now surfaces as a notice, never
    silence."""

    def notice(text: str) -> None:
        if state.dock is not None:
            state.dock.bridge.push({"type": "notice", "text": text})

    try:
        from aqt.addons import AddonsDialog, ConfigEditor

        addon = mw.addonManager.addonFromModule(__name__)
        conf = mw.addonManager.getConfig(__name__) or {}
        dlg = AddonsDialog(mw.addonManager)
        ConfigEditor(dlg, addon, conf)
        notice(
            "Config editor opened. vim_mappings holds [keys, mapped-to, mode] "
            "triples (vim :map semantics); restart Anki to apply changes."
        )
    except Exception as exc:
        _log_line(f"open_addon_config failed: {exc}")
        notice(
            "Couldn't open the config editor - use Tools > Add-ons > "
            '"Chat With Your Cards" > Config instead.'
        )


def _push_settings() -> None:
    """Feed the Settings panel (gear icon) its authoritative snapshot."""
    if state.dock is None:
        return
    # Only well-formed [keys, mapped-to, mode] string triples reach the UI;
    # a malformed hand-edited config entry is dropped, never crashes vim.
    mappings = [
        [str(m[0]), str(m[1]), str(m[2])]
        for m in (state.config.get("vim_mappings") or [])
        if isinstance(m, (list, tuple)) and len(m) == 3
    ]
    state.dock.bridge.push(
        {
            "type": "settings",
            "restore_last_chat": bool(state.config.get("restore_last_chat", False)),
            "dock_side": str(state.config.get("dock_side", "right")),
            "toggle_shortcut": str(state.config.get("toggle_shortcut", "")),
            "new_chat_shortcut": str(state.config.get("new_chat_shortcut", "")),
            "vim_mode": bool(state.config.get("vim_mode", False)),
            "vim_mappings": mappings,
            "theme": _norm_theme(state.config.get("theme")),
            "mcp_inherit_user": bool(state.config.get("mcp_inherit_user", False)),
            "widget_rendering": bool(state.config.get("widget_rendering", False)),
            "defer_shortcut": str(state.config.get("defer_shortcut", DEFER_SHORTCUT)),
            "defer_button": bool(state.config.get("defer_button", True)),
            "defer_on_send": bool(state.config.get("defer_on_send", False)),
            "learning_nudge_threshold": _bounded_int(
                state.config.get("learning_nudge_threshold"),
                10,
                minimum=1,
                maximum=10_000,
            ),
            "learning_nudge_days": _bounded_int(
                state.config.get("learning_nudge_days"),
                7,
                minimum=1,
                maximum=3_650,
            ),
            "learning_run_mode": _norm_learning_run_mode(
                state.config.get("learning_run_mode")
            ),
            "skill_update_policy": _norm_skill_update_policy(
                state.config.get("skill_update_policy")
            ),
        }
    )


def _set_setting(msg: dict[str, Any]) -> None:
    """Settings-panel writes: a small whitelist, each persisted via
    writeConfig and applied live where it can be. Unknown keys are ignored
    (never a generic config poke - the panel is not a JSON editor)."""
    key = str(msg.get("key", ""))
    value = msg.get("value")
    if key == "restore_last_chat":
        state.config["restore_last_chat"] = bool(value)
    elif key == "vim_mode":
        state.config["vim_mode"] = bool(value)
    elif key == "mcp_inherit_user":
        # Widening MCP scope is read as a value at backend-build time, so it
        # takes effect on the next new chat (see ChatController.new_chat).
        state.config["mcp_inherit_user"] = bool(value)
    elif key == "widget_rendering":
        # Read live at each render_widget call, so flipping it (from the
        # settings panel OR the in-chat enable chip) applies immediately.
        state.config["widget_rendering"] = bool(value)
    elif key == "theme":
        state.config["theme"] = _norm_theme(value)
    elif key == "defer_button":
        state.config["defer_button"] = bool(value)
    elif key == "defer_on_send":
        state.config["defer_on_send"] = bool(value)
    elif key == "learning_nudge_threshold":
        state.config[key] = _bounded_int(value, 10, minimum=1, maximum=10_000)
    elif key == "learning_nudge_days":
        state.config[key] = _bounded_int(value, 7, minimum=1, maximum=3_650)
    elif key == "learning_run_mode":
        state.config[key] = _norm_learning_run_mode(value)
    elif key == "skill_update_policy":
        state.config[key] = _norm_skill_update_policy(value)
    elif key == "defer_shortcut":
        from aqt.qt import QKeySequence

        chord = str(value).strip()
        # An unparseable chord must not eat the binding: reject it and keep
        # the old one, telling the panel via the re-pushed snapshot.
        if not chord or QKeySequence(chord).isEmpty():
            _push_settings()
            return
        state.config["defer_shortcut"] = chord
        from . import shortcuts as shortcuts_mod

        shortcuts_mod.register_shortcuts(state)
        if state.deferral is not None:
            _bind_defer_shortcut()
    elif key == "dock_side":
        side = "left" if value == "left" else "right"
        state.config["dock_side"] = side
        if state.dock is not None:
            from .dock import move_dock

            move_dock(state.dock, side)
    else:
        return
    config = mw.addonManager.getConfig(__name__) or {}
    config[key] = state.config[key]
    mw.addonManager.writeConfig(__name__, config)
    _push_settings()
    if key.startswith("learning_") or key == "skill_update_policy":
        _push_learning_state()


def _on_config_updated(*_args: Any) -> None:
    """Anki's built-in add-on config editor was saved (registered via
    setConfigUpdatedAction). Reload config and re-push the *preferences* that
    can change live - vim mode/mappings, theme, dock side, shortcuts, suggested
    questions, open-in-Claude target - so editing them (e.g. a vim mapping) now
    applies WITHOUT an Anki restart. Agent keys (model/effort/fast/agent_tools/
    permission_mode) keep their existing "applies on your next message"
    semantics and are deliberately not re-applied here, so a config edit can't
    yank the model out from under an in-flight chat. Reads getConfig fresh
    rather than the passed arg, so it's robust to the callback's signature."""
    if state.dock is None:
        return
    merged = dict(DEFAULT_CONFIG)
    merged.update(mw.addonManager.getConfig(__name__) or {})
    # Mutate the existing dict in place - do NOT rebind `state.config`. Other
    # components (ProposalManager, controller, dock) captured a reference to
    # this dict at init; rebinding would leave them reading a stale config.
    state.config.clear()
    state.config.update(merged)
    # Chords are rebound here, not just read: config.md promises they apply
    # live, and until now nothing re-registered them.
    from . import shortcuts as shortcuts_mod

    shortcuts_mod.register_shortcuts(state)
    if state.deferral is not None:
        _bind_defer_shortcut()
    desired_side = "left" if merged.get("dock_side") == "left" else "right"
    if state.dock.side != desired_side:
        from .dock import move_dock

        move_dock(state.dock, desired_side)
    _push_settings()
    state.dock.bridge.push(
        {
            "type": "ui_config",
            "suggested_questions": bool(merged.get("suggested_questions", True)),
            "open_in_claude_target": _norm_open_target(
                merged.get("open_in_claude_target", "terminal")
            ),
        }
    )
    _push_learning_state()


def _set_permission_mode(msg: dict[str, Any]) -> None:
    if state.controller is None:
        return
    state.controller.set_permission_mode(str(msg.get("mode", "")))
    config = mw.addonManager.getConfig(__name__) or {}
    config["permission_mode"] = state.config.get("permission_mode", "default")
    mw.addonManager.writeConfig(__name__, config)


def _set_agent(msg: dict[str, Any]) -> None:
    if state.controller is None:
        return
    model = str(msg.get("model", ""))
    effort = str(msg.get("effort", ""))
    # Default to the current stored value (not False) when "fast" is absent,
    # so a hand-crafted/legacy set_agent command that only carries
    # model/effort can't silently flip fast mode off.
    fast_mode = bool(msg.get("fast", state.config.get("fast_mode", False)))
    # Same guard for the agent-tools axis: a tools-less command keeps whatever
    # the user last chose instead of silently resetting to sandbox.
    agent_tools = _norm_agent_tools(msg.get("tools", state.config.get("agent_tools", "sandbox")))
    state.controller.set_agent_config(model, effort, fast_mode, agent_tools)
    config = mw.addonManager.getConfig(__name__) or {}
    config["model"] = state.config.get("model", "")
    config["effort"] = state.config.get("effort", "")
    config["fast_mode"] = state.config.get("fast_mode", False)
    config["agent_tools"] = state.config.get("agent_tools", "sandbox")
    mw.addonManager.writeConfig(__name__, config)


def _norm_open_target(target: Any) -> str:
    """Canonical open-in-Claude target vocabulary is 'terminal' | 'desktop'.
    'gui' is the legacy word (classic UI / older config) and maps to 'desktop'
    so a config written before the assistant-ui rebuild still works. Anything
    else falls back to 'terminal'."""
    t = str(target).strip().lower()
    if t in ("desktop", "gui"):
        return "desktop"
    return "terminal"


def _set_open_target(target: str) -> None:
    """Persist the default 'Open in Claude Code' target (terminal / desktop)
    the split button acts on, so it survives restarts. Normalized so the new
    UI's 'desktop' is accepted (the old check rejected it - dogfood
    2026-07-12: picking Desktop silently did nothing, then opened terminal)."""
    normalized = _norm_open_target(target)
    state.config["open_in_claude_target"] = normalized
    config = mw.addonManager.getConfig(__name__) or {}
    config["open_in_claude_target"] = normalized
    mw.addonManager.writeConfig(__name__, config)


def _save_pins(pins: dict[str, Any]) -> None:
    config = mw.addonManager.getConfig(__name__) or {}
    config["pins"] = pins
    mw.addonManager.writeConfig(__name__, config)


def _learning_observe(event: dict[str, Any]) -> None:
    """Capture hook the ProposalManager fires at content-write sites; feeds
    the learning store (DESIGN.md section 15). Best-effort: learning must
    never break a write."""
    store = state.learning
    if store is None or mw is None:
        return
    kind = str(event.get("event", ""))
    try:
        note_ids = [int(n) for n in (event.get("note_ids") or [])]
        if kind == "applied":
            store.snapshot_notes(mw.col, note_ids)
        elif kind == "resync":
            store.snapshot_notes(mw.col, note_ids, add=False)
        elif kind == "reviewed":
            recorded = store.record_review(
                proposal_kind=str(event.get("proposal_kind", "")),
                note_type=str(event.get("note_type", "")),
                deck_before=str(event.get("deck_before", "")),
                deck_after=str(event.get("deck_after", "")),
                tags_before=event.get("tags_before"),
                tags_after=event.get("tags_after"),
                fields_before=event.get("fields_before"),
                fields_after=event.get("fields_after"),
                declined_fields=event.get("declined_fields"),
                declined_field_comments=event.get("declined_field_comments"),
            )
            if recorded:
                _push_learning_state()
    except Exception as exc:
        _log_line(f"learning capture failed ({kind}): {exc}")


def _apply_skill_update(proposal: Any) -> list[str]:
    """Accepted skill-update proposal: archive the prior skill, write the new
    one, consume the observations it was based on."""
    from .proposals import ProposalError

    store = state.learning
    if store is None:
        raise ProposalError("the learning store is not available")
    backup = store.write_skill(str(proposal.op_args.get("new_content", "")))
    store.consume([str(i) for i in proposal.op_args.get("observation_ids") or []])
    _push_learning_state()
    if backup is not None:
        return [
            "Previous skill version archived as "
            f"user_files/learning/skill-backups/{backup.name}"
        ]
    return []


def _build_media_staging() -> Any:
    """Staged proposal media (task #21) lives under user_files/ so it survives
    Anki restarts for still-pending proposals and is upgrade-safe."""
    from .media_staging import MediaStaging

    return MediaStaging(USER_FILES / "staging")


def _agent_skill_names() -> set[str]:
    """Skills that already exist under agent-home, for
    ProposalManager.submit_skill_create's collision check (workspace task
    #20)."""
    from .skills import agent_skill_names

    return agent_skill_names(USER_FILES / "agent-home")


def _apply_new_skill(proposal: Any) -> list[str]:
    """Accepted skill_create proposal: write the brand-new SKILL.md. See
    skills.write_new_skill's security note - this is the ONLY path that ever
    writes a new skill file, and it always runs from an already-accepted,
    user-reviewed proposal. Re-checks existence itself (not just at propose
    time), so a race with another proposal accepted for the same name in the
    meantime fails loudly as a ProposalError instead of overwriting it."""
    from .proposals import ProposalError
    from .skills import write_new_skill

    args = proposal.op_args
    name = str(args.get("name", ""))
    description = str(args.get("description", ""))
    markdown = str(args.get("markdown", ""))
    try:
        path = write_new_skill(USER_FILES / "agent-home", name, description, markdown)
    except FileExistsError as exc:
        raise ProposalError(str(exc)) from None
    return [f"New skill written to user_files/{path.relative_to(USER_FILES)}"]


def _push_learning_state() -> None:
    """Push learning state and start a due background analysis when enabled."""
    if state.dock is None or state.learning is None:
        return
    threshold = _bounded_int(
        state.config.get("learning_nudge_threshold"),
        10,
        minimum=1,
        maximum=10_000,
    )
    days = _bounded_int(
        state.config.get("learning_nudge_days"),
        7,
        minimum=1,
        maximum=3_650,
    )
    run_mode = _norm_learning_run_mode(state.config.get("learning_run_mode"))
    nudge = state.learning.nudge_state(threshold, days)
    ready = (
        state.proposals.background_skill_update_ready()
        if state.proposals is not None
        else None
    )
    pending_update = bool(
        state.proposals is not None and state.proposals.has_pending_skill_update()
    )
    state.dock.bridge.push(
        {
            "type": "learning",
            "pending": nudge["pending"],
            "nudge": bool(
                nudge["nudge"]
                and run_mode == "chat"
                and not pending_update
                and not state.background_learning_running
            ),
            "running": state.background_learning_running,
            "update_ready": ready is not None,
            "proposal_id": ready.id if ready is not None else "",
        }
    )
    if (
        run_mode == "background"
        and not state.background_learning_running
        and not pending_update
        and state.learning.background_due(threshold, days)
    ):
        from aqt.qt import QTimer

        QTimer.singleShot(0, _start_background_learning)


def _background_learning_prompt(job_id: str) -> str:
    return (
        SKILL_REVIEW_PROMPT
        + " This is a hidden background analysis, so do not narrate intermediate "
        "work. If you propose an update, pass background_job_id="
        + job_id
        + " exactly."
    )


def _start_background_learning() -> None:
    """Start an isolated agent session without replacing the visible chat."""
    if (
        state.background_learning_running
        or state.learning is None
        or state.proposals is None
        or mw is None
        or mw.col is None
    ):
        return
    threshold = _bounded_int(
        state.config.get("learning_nudge_threshold"), 10, minimum=1, maximum=10_000
    )
    days = _bounded_int(
        state.config.get("learning_nudge_days"), 7, minimum=1, maximum=3_650
    )
    if not state.learning.background_due(threshold, days):
        return

    from .context import build_system_prompt
    from .controller import ChatController

    job_id = secrets.token_hex(12)
    observation_ids = state.learning.pending_ids()
    state.background_learning_running = True
    state.background_learning_job_id = job_id
    state.background_learning_observation_ids = observation_ids
    state.proposals.begin_background_skill_job(job_id)

    background_config = dict(state.config)
    background_config["agent_tools"] = "sandbox"
    background_config["permission_mode"] = "default"

    def system_prompt() -> str:
        return build_system_prompt(
            permission_mode="default",
            agent_tools="sandbox",
            pins=state.proposals.pins if state.proposals else None,
            custom_instructions=str(state.config.get("custom_instructions", "")),
        )

    def background_push(payload: dict[str, Any]) -> None:
        event_type = str(payload.get("type", ""))
        if event_type == "done":
            from aqt.qt import QTimer

            QTimer.singleShot(0, lambda: _finish_background_learning(job_id))
        elif event_type in {"cancelled", "error", "setup_needed"}:
            message = (
                str(payload.get("message", ""))
                or "the agent backend is unavailable"
            )
            from aqt.qt import QTimer

            QTimer.singleShot(
                0, lambda: _finish_background_learning(job_id, error=message)
            )

    controller = ChatController(
        push=background_push,
        config=background_config,
        system_prompt_builder=system_prompt,
        ensure_mcp=_ensure_mcp,
        workdir=USER_FILES / "agent-home",
    )
    state.background_learning_controller = controller
    _push_learning_state()
    try:
        controller.send_background_message(_background_learning_prompt(job_id))
    except Exception as exc:
        _finish_background_learning(job_id, error=str(exc))


def _finish_background_learning(job_id: str, *, error: str = "") -> None:
    if job_id != state.background_learning_job_id:
        return
    controller = state.background_learning_controller
    state.background_learning_controller = None
    state.background_learning_running = False
    state.background_learning_job_id = ""
    observation_ids = list(state.background_learning_observation_ids)
    state.background_learning_observation_ids = []
    if state.proposals is not None:
        state.proposals.end_background_skill_job(job_id)
    if controller is not None:
        controller.shutdown()

    if error:
        _log_line(f"background learning failed: {error}")
        if state.dock is not None:
            state.dock.bridge.push(
                {"type": "notice", "text": f"Background learning failed: {error}"}
            )
    elif state.learning is not None:
        ready = (
            state.proposals.background_skill_update_ready()
            if state.proposals is not None
            else None
        )
        # Hidden review proposals are session-local. Remember their evidence
        # in memory so this session does not duplicate the job, but let a
        # restart recompute it rather than stranding an unreachable update.
        state.learning.mark_background_attempt(
            observation_ids, persist=ready is None
        )
        if state.dock is not None:
            if ready is not None:
                text = "A writing-guidance update is ready for review."
            elif state.proposals is not None and state.proposals.has_pending_skill_update():
                text = "Automatic guidance update failed; a review card was opened."
            elif len(state.learning.pending_ids()) < len(observation_ids):
                text = "Writing guidance was updated in the background."
            else:
                text = "Background learning found no durable writing pattern to add."
            state.dock.bridge.push({"type": "notice", "text": text})
    _push_learning_state()


def _show_background_skill_update(proposal_id: str) -> None:
    if state.proposals is None:
        return
    if state.proposals.show_background_skill_update(proposal_id):
        _push_learning_state()


def _scan_learning() -> None:
    """Diff tracked notes against their snapshots (catches edits made in the
    editor, the Browser, or on another device after sync). Cheap: one bulk
    mod query, full reads only for changed notes."""
    if state.learning is None or mw is None or mw.col is None:
        return
    try:
        found = state.learning.scan(mw.col)
        if found:
            _log_line(f"learning scan: {found} new observation(s)")
    except Exception as exc:
        _log_line(f"learning scan failed: {exc}")
    _push_learning_state()


def _start_skill_review() -> None:
    """Nudge chip clicked: fresh scan, then a NEW chat seeded with the
    reflection prompt (the previous chat stays resumable in History)."""
    if state.controller is None:
        return
    _scan_learning()
    state.controller.new_chat()
    if state.dock is not None:
        # The webview normally renders the user bubble itself on send; this
        # send originates in Python, so push it explicitly (transcripts get
        # it from send_user_message - a plain bridge push avoids recording
        # the message twice).
        state.dock.bridge.push({"type": "user_message", "text": SKILL_REVIEW_PROMPT})
        state.dock.bridge.push({"type": "learning_review_started"})
    state.controller.send_user_message(SKILL_REVIEW_PROMPT)


def _log_line(message: str) -> None:
    """Append one line to the shared backend log (best-effort)."""
    try:
        from .backends.claude_cli import BackendLog

        BackendLog(USER_FILES / "logs" / "backend.log").write(message)
    except Exception:
        pass


def _backup_checkpoint(reason: str, critical: bool = False) -> bool:
    """Force an Anki backup before bulk/delete/change-set applies, so even
    non-ledger-revertible operations have a way back.

    critical=True (irreversible ops like delete) waits for the backup to
    finish so it is on disk before the destructive change proceeds -
    otherwise the "safety net" would be racing the delete. Reversible ops
    (ledger-undoable) back up asynchronously to avoid stalling the UI, and
    on a huge collection even that is best-effort insurance behind the
    ledger.

    Returns True when the checkpoint is safe to proceed on, False only when
    it actually failed. ``col.create_backup(force=True, ...)`` can legitimately
    return False for "nothing has changed since the last backup" (see its
    pylib docstring) - that is NOT a failure, there is still a good backup on
    disk, so we still return True. Only an exception - the documented failure
    signal - means no safety net exists. ProposalManager (proposals.py) treats
    False as fatal for critical=True writes (delete: the op ABORTS rather than
    proceed with no way back) and as a surfaced warning otherwise; this
    function's job is only to report the outcome honestly, never to swallow
    it (previously it silently ate the exception and let the write proceed
    regardless)."""
    state.last_checkpoint = {"reason": reason, "critical": critical,
                             "created": None, "error": None}
    if mw is None or mw.col is None:
        state.last_checkpoint["error"] = "collection is not open"
        return False
    try:
        created = mw.col.create_backup(
            backup_folder=mw.pm.backupFolder(),
            force=True,
            wait_for_completion=critical,
        )
        state.last_checkpoint["created"] = bool(created)
        return True
    except Exception as exc:
        # Backups are defense-in-depth, but a failure must be visible (backend
        # log + doctor) and, for critical writes, must block the op - see the
        # docstring above.
        state.last_checkpoint["error"] = str(exc)
        _log_line(f"backup checkpoint failed ({reason}): {exc}")
        return False


def _refresh_reviewer(note_ids: list[int]) -> None:
    """Re-render the reviewer if it is showing a note we just wrote to, so an
    accepted edit shows without the user leaving and re-entering review."""
    if mw is None or mw.state != "review":
        return
    reviewer: Any = getattr(mw, "reviewer", None)
    card = getattr(reviewer, "card", None) if reviewer else None
    if reviewer is None or card is None or card.nid not in set(note_ids):
        return
    try:
        card.load()  # re-read the mutated note from the collection
        if getattr(reviewer, "state", "question") == "answer":
            reviewer._showAnswer()
        else:
            reviewer._showQuestion()
    except Exception:
        # Best-effort: a private-API drift shouldn't break the write itself.
        pass


def _refresh_after_grading(card_ids: list[int]) -> None:
    """Reload a reviewer that was showing a card just graded by the agent.

    The scheduler write is already complete, but leaving the reviewer's stale
    in-memory card on screen could let the user answer it a second time.
    ``mw.reset()`` is Anki's normal UI refresh path.
    """

    if mw is None or mw.col is None:
        return
    reviewer: Any = getattr(mw, "reviewer", None)
    card = getattr(reviewer, "card", None) if reviewer else None
    if card is None or int(getattr(card, "id", 0)) not in set(card_ids):
        return
    try:
        mw.reset()
    except Exception:
        pass


def _refresh_deck_ui() -> None:
    """Redraw the deck list / overview after a deck operation, so a created,
    renamed, or rebuilt deck shows without the user switching screens."""
    if mw is None:
        return
    try:
        if mw.state == "deckBrowser":
            mw.deckBrowser.refresh()
        elif mw.state == "overview":
            mw.overview.refresh()
    except Exception:
        pass  # cosmetic refresh; never let it break the write


def _recheck_backend() -> None:
    """The setup card's "Re-check" button (task #19): result (found/still
    missing) is discarded here - ChatController.recheck_backend() already
    pushes whichever of setup_resolved/notice fits, so there is nothing left
    for this thin bridge wrapper to do with the bool."""
    if state.controller is not None:
        state.controller.recheck_backend()


def _run_doctor() -> None:
    """Gather the setup report off the main thread (version subprocesses can
    take a second each) and push it to the doctor panel."""
    from .backends.claude_cli import find_claude_cli
    from .doctor import gather_report

    config = dict(state.config)
    backend_kind = state.controller.backend_kind if state.controller else "unset"
    mcp_url = state.mcp.url if state.mcp else None
    learning_stats = state.learning.stats() if state.learning else None

    def work() -> list[dict[str, Any]]:
        return gather_report(
            config=config,
            backend_kind=backend_kind,
            mcp_url=mcp_url,
            agent_home=USER_FILES / "agent-home",
            find_claude=find_claude_cli,
            learning=learning_stats,
        )

    def done(future: Any) -> None:
        try:
            results = future.result()
        except Exception as exc:
            results = [{"label": "Doctor", "status": "broken", "detail": str(exc)}]
        if state.dock is not None:
            state.dock.bridge.push({"type": "doctor", "results": results})

    mw.taskman.run_in_background(work, done)


def _open_in_claude_code(target: str = "terminal") -> None:
    """Hand this chat to a full-power Claude Code.

    Terminal: same session id via --resume, same cwd (agent-home carries
    .mcp.json for our anki tools plus the skills dir), same agent settings
    (model/effort/fast/agent-tools flags).
    Desktop app: there is STILL no resume-by-id deep link (re-verified
    2026-07-13, DESIGN.md section 14) - but interactive claude executes a
    slash command passed as the initial prompt (verified empirically with
    /cost under a PTY), and `/desktop` migrates the CURRENT session to the
    desktop app. So the desktop path resumes the session in a terminal and
    immediately runs /desktop: a true resume, with the terminal window as
    scaffolding. Fallback (no session yet, non-macOS, or terminal
    automation failed): the old claude://code/new deep link opening a NEW
    desktop session pointed at this chat's transcript file.
    """
    import shlex
    import subprocess
    import sys as _sys
    import urllib.parse

    agent_home = USER_FILES / "agent-home"
    # Live session id if a message was exchanged; else the resume id of a chat
    # loaded from History (session not respawned yet) - either lets the target
    # continue THIS conversation rather than starting blank.
    sid = state.controller.backend_session_id if state.controller else None
    if not sid and state.transcripts is not None:
        sid = state.transcripts.backend_session_id

    def notice(text: str) -> None:
        if state.dock is not None:
            state.dock.bridge.push({"type": "notice", "text": text})

    # Carry the dock's agent settings into the handed-off session so the
    # conversation continues under the same model/effort/fast-mode - and the
    # same environment power: full agent tools maps to bypassPermissions,
    # exactly what the dock itself spawns with (user-requested 2026-07-13).
    # The collection-write permission mode needs no flag: it is enforced live
    # by our MCP server, which the handed-off session talks to via .mcp.json.
    from .backends.claude_cli import VALID_EFFORTS

    extra_args: list[str] = []
    model = str(state.config.get("model", "")).strip()
    effort = str(state.config.get("effort", "")).strip()
    if model:
        extra_args += ["--model", model]
    if effort in VALID_EFFORTS:
        extra_args += ["--effort", effort]
    if state.config.get("fast_mode"):
        import json as _json

        extra_args += ["--settings", _json.dumps({"fastMode": True})]
    # Carry the dock's agent-tools tier into the resumed session so the terminal
    # / desktop handoff starts in the same posture (sandbox adds nothing - a real
    # terminal has an interactive approver, so the human gates tools there).
    _resume_perm_mode = {
        "acceptEdits": "acceptEdits",
        "auto": "auto",
        "full": "bypassPermissions",
    }.get(_norm_agent_tools(state.config.get("agent_tools", "sandbox")))
    if _resume_perm_mode:
        extra_args += ["--permission-mode", _resume_perm_mode]

    def resume_argv(initial_prompt: str | None = None) -> list[str]:
        argv = ["claude"]
        if sid:
            argv += ["--resume", str(sid)]
        argv += extra_args
        if initial_prompt is not None:
            argv.append(initial_prompt)
        return argv

    def resume_command(initial_prompt: str | None = None) -> str:
        # POSIX-shell form (macOS/Linux terminals and the clipboard fallback).
        parts = " ".join(shlex.quote(a) for a in resume_argv(initial_prompt))
        return f"cd {shlex.quote(str(agent_home))} && {parts}"

    if _norm_open_target(target) == "desktop":
        if sid and _desktop_handoff_invisible(sid, extra_args, agent_home):
            notice(
                "Resuming this chat and handing it to the Claude Code desktop "
                "app… (runs invisibly; you'll get a notice if it can't)."
            )
            return
        prompt = (
            "This continues an Anki chat from the Chat With Your Cards "
            "add-on (the anki MCP tools are configured in this folder)."
        )
        if state.transcripts is not None:
            state.transcripts.flush()
            transcript = (
                USER_FILES / "transcripts" / f"{state.transcripts.current_id}.json"
            )
            if transcript.exists():
                prompt += (
                    f" Read the conversation so far at {transcript} before "
                    "responding, then continue helping."
                )
        url = (
            "claude://code/new?folder="
            + urllib.parse.quote(str(agent_home), safe="")
            + "&q="
            + urllib.parse.quote(prompt, safe="")
        )
        try:
            if _sys.platform == "darwin":
                subprocess.run(["open", url], check=True, timeout=10)
            else:
                import webbrowser

                webbrowser.open(url)
            notice(
                "Opened in the Claude Code desktop app (new session reading "
                "this chat's transcript - no session to truly resume here)."
            )
        except Exception:
            notice(f"Could not open the desktop app; deep link: {url}")
        return

    cmd = resume_command()
    app = str(state.config.get("terminal_app", "")).strip()
    opened = False
    if _sys.platform == "darwin":
        opened = _open_macos_terminal(cmd, app)
    elif _sys.platform.startswith("linux"):
        opened = _open_linux_terminal(cmd)
    elif _sys.platform == "win32":
        opened = _open_windows_terminal(resume_argv(), agent_home)
    if opened:
        where = app or ("Terminal" if _sys.platform == "darwin" else "a terminal window")
        if sid:
            carried = " under this chat's model/effort settings" if extra_args else ""
            notice(
                f"Resumed this chat in Claude Code ({where}){carried} - it has "
                "your anki tools via .mcp.json plus Claude Code's full toolset."
            )
        else:
            # No exchanged message yet -> no session to resume. Be honest
            # rather than implying the conversation carried over (dogfood
            # 2026-07-12: 'the chat wasn't even loaded there').
            notice(
                f"Opened a fresh Claude Code ({where}) in this chat's folder "
                "(no message sent yet, so there was nothing to resume)."
            )
        return
    try:
        from aqt.qt import QApplication

        QApplication.clipboard().setText(cmd)
        notice(
            "Command copied to the clipboard - paste it into a terminal to "
            "continue this chat in Claude Code."
        )
    except Exception:
        notice(f"Run this in a terminal to continue in Claude Code: {cmd}")


def _desktop_handoff_invisible(
    sid: str, extra_args: list[str], agent_home: Path
) -> bool:
    """Migrate this chat to the Claude Code desktop app with NO visible
    terminal: run `claude --resume <sid> … "/desktop"` on a hidden PTY (the
    CLI needs a tty to run interactively; it does not need a window - the
    /desktop-under-script(1) probe proved this, 2026-07-13). POSIX only
    (macOS + Linux; Windows has no pty module and falls back to the
    deep-link path).

    A watcher thread drains the PTY (the TUI blocks if nobody reads) and
    reports: clean exit = migrated (that is /desktop's behavior - hand off,
    then exit); still running at the deadline = migration unavailable
    (older CLI / API-key auth), so terminate and tell the user the visible
    fallback. Returns False only on spawn failure, so the caller can fall
    back to the deep link immediately."""
    import os
    import select
    import subprocess
    import sys as _sys
    import threading

    if _sys.platform != "darwin" and not _sys.platform.startswith("linux"):
        return False
    from .backends.claude_cli import find_claude_cli

    claude = find_claude_cli(str(state.config.get("claude_cli_path", "")).strip())
    if not claude:
        return False
    try:
        import pty

        master, slave = pty.openpty()
    except Exception:
        return False
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    try:
        proc = subprocess.Popen(
            [claude, "--resume", str(sid), *extra_args, "/desktop"],
            cwd=str(agent_home),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
    except Exception as exc:
        os.close(master)
        os.close(slave)
        _log_line(f"invisible /desktop spawn failed: {exc}")
        return False
    os.close(slave)

    def notice_on_main(text: str) -> None:
        def push() -> None:
            if state.dock is not None:
                state.dock.bridge.push({"type": "notice", "text": text})

        mw.taskman.run_on_main(push)

    def watch() -> None:
        import time

        deadline = time.monotonic() + 90
        try:
            while time.monotonic() < deadline and proc.poll() is None:
                readable, _, _ = select.select([master], [], [], 0.5)
                if readable:
                    try:
                        if not os.read(master, 4096):
                            break
                    except OSError:
                        break
        finally:
            try:
                os.close(master)
            except OSError:
                pass
        if proc.poll() == 0:
            notice_on_main("Handed this chat to the Claude Code desktop app.")
            return
        if proc.poll() is None:
            proc.terminate()
        _log_line(f"invisible /desktop did not migrate (exit={proc.poll()})")
        notice_on_main(
            "The desktop migration didn't complete (older CLI or API-key "
            "auth?). Fallback: Open in Claude Code > Terminal, then type "
            "/desktop there."
        )

    threading.Thread(target=watch, name="cwyc-desktop-handoff", daemon=True).start()
    return True


def _open_linux_terminal(command: str) -> bool:
    """Open a visible terminal running `command` on Linux (user-requested
    2026-07-14 - the terminal handoff was macOS-only). Best-effort sweep of
    common emulators; the first one found wins. Returns False so the caller
    can fall back to the clipboard."""
    import shutil
    import subprocess

    candidates: list[tuple[str, list[str]]] = [
        ("x-terminal-emulator", ["-e"]),  # Debian alternatives symlink
        ("gnome-terminal", ["--"]),
        ("konsole", ["-e"]),
        ("xfce4-terminal", ["-x"]),
        ("kitty", []),
        ("alacritty", ["-e"]),
        ("wezterm", ["start", "--"]),
        ("xterm", ["-e"]),
    ]
    for name, flags in candidates:
        path = shutil.which(name)
        if not path:
            continue
        try:
            subprocess.Popen(
                [path, *flags, "bash", "-lc", command], start_new_session=True
            )
            return True
        except Exception:
            continue
    return False


def _open_windows_terminal(argv: list[str], cwd: Path) -> bool:
    """Open a visible cmd window running `argv` on Windows (user-requested
    2026-07-14). `start` is a cmd builtin, hence shell=True; the empty ""
    is the window title - without it, start eats a quoted program path as
    the title. /k keeps the window open, which is the point (it hosts the
    interactive resumed session)."""
    import subprocess

    try:
        quoted = subprocess.list2cmdline(argv)
        subprocess.Popen(f'start "" cmd /k "{quoted}"', shell=True, cwd=str(cwd))
        return True
    except Exception:
        return False


def _open_macos_terminal(command: str, app: str) -> bool:
    """Run `command` in a macOS terminal, honoring the configured terminal_app.

    Empty (or "Terminal") drives Apple Terminal via AppleScript `do script`
    (the known-good default). Any other app name launches a throwaway
    executable `.command` script in that app via `open -a`, which most
    terminals (iTerm, Warp, Ghostty, kitty, …) run on open. Best-effort:
    returns False so the caller can fall back to the clipboard."""
    import subprocess
    import sys as _sys

    if _sys.platform != "darwin":
        return False
    if not app or app.lower() in ("terminal", "terminal.app"):
        script = command.replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e", 'tell application "Terminal" to activate',
                    "-e", f'tell application "Terminal" to do script "{script}"',
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            return True
        except Exception:
            return False
    try:
        import os as _os
        import stat
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".command", prefix="cwyc-cc-")
        with _os.fdopen(fd, "w") as handle:
            handle.write(f"#!/bin/bash\n{command}\n")
        _os.chmod(path, _os.stat(path).st_mode | stat.S_IXUSR)
        subprocess.run(
            ["open", "-a", app, path], check=True, capture_output=True, timeout=15
        )
        return True
    except Exception:
        return False


def _open_session_browser() -> None:
    """One-click review of this session's AI-created notes in the Browser."""
    if state.proposals is None:
        return
    session_tag = state.proposals.session_tag
    if not session_tag:
        state.dock.bridge.push(
            {
                "type": "notice",
                "text": "Session tagging is off (session_tag_prefix is empty), so "
                "this session's notes can't be filtered in the Browser.",
            }
        ) if state.dock else None
        return
    from aqt import dialogs

    query = f'tag:"{session_tag}"'
    browser = dialogs.open("Browser", mw)
    try:
        browser.search_for(query)
    except AttributeError:
        browser.form.searchEdit.lineEdit().setText(query)
        browser.onSearchActivated()


def _teardown() -> None:
    if state.learning_timer is not None:
        state.learning_timer.stop()
        state.learning_timer = None
    if state.background_learning_controller is not None:
        state.background_learning_controller.shutdown()
        state.background_learning_controller = None
    if state.proposals is not None and state.background_learning_job_id:
        state.proposals.end_background_skill_job(state.background_learning_job_id)
    state.background_learning_running = False
    state.background_learning_job_id = ""
    state.background_learning_observation_ids = []
    if state.deferral is not None:
        from aqt.reviewer import Reviewer

        state.deferral.uninstall(Reviewer)
        state.deferral.clear_session()
    if state.approvals is not None:
        state.approvals.deny_all()
    if state.controller is not None:
        state.controller.shutdown()
    if state.mcp is not None:
        state.mcp.stop()
        state.mcp = None
    _save_dock_width()


def _save_dock_width() -> None:
    if state.dock is None:
        return
    config = mw.addonManager.getConfig(__name__) or {}
    # expanded_width, not width(): while collapsed the live width is the rail.
    config["dock_width"] = state.dock.expanded_width
    config["dock_collapsed"] = not state.dock.expanded
    mw.addonManager.writeConfig(__name__, config)


if mw is not None:
    from aqt import gui_hooks

    mw.addonManager.setWebExports(__name__, r"web/.*\.(css|js)")
    gui_hooks.main_window_did_init.append(_setup)
    gui_hooks.profile_will_close.append(_teardown)
