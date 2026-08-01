"""Permission-aware UI workflow for native arbitrary-card grading.

The scheduler implementation lives in the vendored Safe Collection Operations
library.  This module is deliberately only CWYC policy and presentation:

- resolve exact cards to stable note GUIDs;
- show a dedicated confirmation/audit chip;
- apply immediately only in the two user-selected automatic modes, under
  their existing per-session caps;
- report preserved suspension/burial and offer a separate native
  ``make_cards_available`` action.

All methods are called on Anki's main thread.  There are no direct scheduling
row writes here or in the shared core.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ._vendor.safe_collection_operations import (
    EventRef,
    OperationError,
    Rating,
    Target,
    get_grading_cursor,
    grade_cards_now,
    inspect_cards,
    make_cards_available,
)


GRADING_STREAM_ID = "chat-with-your-cards"
MAX_CARDS_PER_OPERATION = 50
QUEUE_LABELS = {
    -1: "suspended",
    -2: "scheduler-buried",
    -3: "manually buried",
}
_HTML = re.compile(r"<[^>]+>")


class GradingWorkflowError(RuntimeError):
    """A grading request could not be prepared or applied safely."""


def _plain(value: Any, limit: int = 180) -> str:
    text = _HTML.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _unique_card_ids(raw_ids: Iterable[Any]) -> tuple[int, ...]:
    result: list[int] = []
    seen: set[int] = set()
    for raw in raw_ids:
        try:
            card_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise GradingWorkflowError("card_ids must contain only integers") from exc
        if card_id <= 0:
            raise GradingWorkflowError(f"invalid card id {card_id}")
        if card_id not in seen:
            seen.add(card_id)
            result.append(card_id)
    if not result:
        raise GradingWorkflowError("at least one card is required")
    if len(result) > MAX_CARDS_PER_OPERATION:
        raise GradingWorkflowError(
            f"one grading operation may target at most {MAX_CARDS_PER_OPERATION} cards"
        )
    return tuple(result)


@dataclass
class GradingRequest:
    id: str
    action: str
    card_ids: tuple[int, ...]
    cards: list[dict[str, Any]]
    rationale: str
    status: str = "pending"
    warnings: list[str] = field(default_factory=list)
    targets: tuple[Target, ...] = ()
    result: dict[str, Any] | None = None
    availability: dict[str, Any] | None = None
    available_card_ids: tuple[int, ...] = ()
    automatic_mode: str | None = None
    event: EventRef | None = None
    # Which native rating a `fail` request records (#16). Named `rating`
    # rather than folded into `action` because the action is still "record a
    # review on these exact cards" - only the button pressed differs.
    rating: str = "again"

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "status": self.status,
            "card_ids": list(self.card_ids),
            "cards": self.cards,
            "rationale": self.rationale,
            "warnings": self.warnings,
            "result": self.result,
            "availability": self.availability,
            "available_card_ids": list(self.available_card_ids),
            "automatic_mode": self.automatic_mode,
            "rating": self.rating,
        }


class GradingManager:
    """Own CWYC's grading confirmations, budgets, and visible audit trail."""

    def __init__(
        self,
        *,
        get_col: Callable[[], Any],
        push: Callable[[dict[str, Any]], None],
        config: dict[str, Any],
        after_change: Callable[[list[int]], None] | None = None,
    ) -> None:
        self._get_col = get_col
        self._push = push
        self._config = config
        self._after_change = after_change or (lambda _card_ids: None)
        self._requests: dict[str, GradingRequest] = {}
        self._counter = 0
        self._auto_graded = 0
        self._trusted_graded = 0
        self.session_id = secrets.token_hex(4)

    def new_session(self) -> None:
        self._requests.clear()
        self._counter = 0
        self._auto_graded = 0
        self._trusted_graded = 0
        self.session_id = secrets.token_hex(4)

    def _col(self) -> Any:
        col = self._get_col()
        if col is None:
            raise GradingWorkflowError("collection is not open")
        return col

    def _next_id(self) -> str:
        self._counter += 1
        return f"g{self._counter}"

    def _push_request(self, request: GradingRequest) -> None:
        self._push({"type": "grading", "grading": request.to_payload()})

    def _inspect(self, card_ids: tuple[int, ...]) -> tuple[list[dict[str, Any]], tuple[Target, ...]]:
        col = self._col()
        inspected = inspect_cards(col, card_ids)["cards"]
        cards: list[dict[str, Any]] = []
        targets: list[Target] = []
        for item in inspected:
            card_id = int(item["card_id"])
            card = col.get_card(card_id)
            note = col.get_note(int(item["note_id"]))
            fields = list(note.items())
            prompt_name = ""
            prompt = ""
            for name, value in fields:
                rendered = _plain(value)
                if rendered:
                    prompt_name = str(name)
                    prompt = rendered
                    break
            current_deck = col.decks.name(int(item["current_deck_id"]))
            home_deck = col.decks.name(int(item["home_deck_id"]))
            try:
                template = str(card.template().get("name", ""))
            except Exception:
                template = ""
            queue = int(item["queue"])
            cards.append(
                {
                    **item,
                    "deck": home_deck,
                    "current_deck": current_deck,
                    "template": template,
                    "prompt_field": prompt_name,
                    "prompt": prompt or f"Note {int(item['note_id'])}",
                    "hidden_state": QUEUE_LABELS.get(queue),
                }
            )
            targets.append(Target(card_id=card_id, note_guid=str(item["note_guid"])))
        return cards, tuple(targets)

    @staticmethod
    def _warnings_for_cards(
        cards: list[dict[str, Any]], action: str, rating: str = "again"
    ) -> list[str]:
        warnings: list[str] = []
        label = rating.capitalize()
        hidden = [card for card in cards if card.get("hidden_state")]
        if action == "fail" and hidden:
            labels = sorted({str(card["hidden_state"]) for card in hidden})
            warnings.append(
                "The review will be recorded, but existing "
                + ", ".join(labels)
                + " state will remain. You can make the cards available afterward."
            )
        preview = [card for card in cards if card.get("preview_filtered")]
        if action == "fail" and preview:
            warnings.append(
                "Preview-filtered targets will leave preview individually before "
                f"Anki records {label} in their home deck."
            )
        filtered = [card for card in cards if card.get("rescheduling_filtered")]
        if action == "fail" and filtered:
            warnings.append(
                "Targets already in a rescheduling filtered deck will receive "
                f"their native {label} there."
            )
        return warnings

    def _automatic_mode(self, count: int) -> str | None:
        mode = str(self._config.get("permission_mode", "default"))
        if mode == "auto-accept":
            cap = max(0, int(self._config.get("auto_accept_cap", 20)))
            return mode if self._auto_graded + count <= cap else None
        if mode == "trusted-writes":
            budget = max(0, int(self._config.get("write_budget", 200)))
            return mode if self._trusted_graded + count <= budget else None
        return None

    def _consume_budget(self, mode: str | None, count: int) -> None:
        if mode == "auto-accept":
            self._auto_graded += count
        elif mode == "trusted-writes":
            self._trusted_graded += count

    def submit_fail(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_ids = args.get("card_ids")
        if not isinstance(raw_ids, list):
            raise GradingWorkflowError("card_ids must be an array")
        card_ids = _unique_card_ids(raw_ids)
        # Parsed here so a bad rating is refused before a confirmation card is
        # ever shown, rather than at apply time with the user's click spent.
        try:
            rating = Rating.from_value(args.get("rating", "again"))
        except OperationError as exc:
            raise GradingWorkflowError(str(exc)) from exc
        cards, targets = self._inspect(card_ids)
        request = GradingRequest(
            id=self._next_id(),
            action="fail",
            card_ids=card_ids,
            cards=cards,
            targets=targets,
            rationale=_plain(args.get("rationale"), 500),
            warnings=self._warnings_for_cards(cards, "fail", rating.name.lower()),
            rating=rating.name.lower(),
        )
        self._requests[request.id] = request
        automatic = self._automatic_mode(len(card_ids))
        if automatic is None:
            self._push_request(request)
            return {
                "grading_id": request.id,
                "status": "pending_user_confirmation",
                "card_ids": list(card_ids),
                "warnings": request.warnings,
            }

        request.automatic_mode = automatic
        request.status = "applying"
        self._push_request(request)
        try:
            response = self._apply_fail(request, automatic=True)
        except Exception:
            request.status = "failed"
            self._push_request(request)
            raise
        self._consume_budget(automatic, len(card_ids))
        return response

    def submit_make_available(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_ids = args.get("card_ids")
        if not isinstance(raw_ids, list):
            raise GradingWorkflowError("card_ids must be an array")
        card_ids = _unique_card_ids(raw_ids)
        cards, targets = self._inspect(card_ids)
        request = GradingRequest(
            id=self._next_id(),
            action="make_available",
            card_ids=card_ids,
            cards=cards,
            targets=targets,
            rationale=_plain(args.get("rationale"), 500),
        )
        self._requests[request.id] = request
        automatic = self._automatic_mode(len(card_ids))
        if automatic is None:
            self._push_request(request)
            return {
                "grading_id": request.id,
                "status": "pending_user_confirmation",
                "card_ids": list(card_ids),
            }
        request.automatic_mode = automatic
        request.status = "applying"
        self._push_request(request)
        try:
            response = self._apply_make_available(request, automatic=True)
        except Exception:
            request.status = "failed"
            self._push_request(request)
            raise
        self._consume_budget(automatic, len(card_ids))
        return response

    def accept(self, msg: dict[str, Any]) -> None:
        request = self._requests.get(str(msg.get("id", "")))
        if request is None or request.status != "pending":
            return
        if str(self._config.get("permission_mode", "default")) == "read-only":
            warning = (
                "The session is now read-only. Change the permission mode before "
                "accepting this grading operation."
            )
            if warning not in request.warnings:
                request.warnings.append(warning)
            self._push_request(request)
            return
        request.status = "applying"
        self._push_request(request)
        try:
            if request.action == "fail":
                self._apply_fail(request, automatic=False)
            else:
                self._apply_make_available(request, automatic=False)
        except Exception as exc:
            request.status = "failed"
            request.warnings.append(str(exc))
            self._push_request(request)

    def reject(self, msg: dict[str, Any]) -> None:
        request = self._requests.get(str(msg.get("id", "")))
        if request is None or request.status != "pending":
            return
        request.status = "rejected"
        self._push_request(request)

    def make_available_from_failure(self, msg: dict[str, Any]) -> None:
        request = self._requests.get(str(msg.get("id", "")))
        if (
            request is None
            or request.action != "fail"
            or request.status not in {"accepted", "auto-accepted"}
            or not request.available_card_ids
        ):
            return
        if str(self._config.get("permission_mode", "default")) == "read-only":
            warning = (
                "The session is now read-only. Change the permission mode before "
                "making these cards available."
            )
            if warning not in request.warnings:
                request.warnings.append(warning)
            self._push_request(request)
            return
        try:
            result = make_cards_available(self._col(), request.available_card_ids)
            request.availability = result.to_dict()
            restored = set(result.card_ids)
            request.cards = [
                {**card, "hidden_state": None}
                if int(card["card_id"]) in restored
                else card
                for card in request.cards
            ]
            request.available_card_ids = ()
            self._after_change(list(result.card_ids))
        except Exception as exc:
            request.warnings.append(f"Could not make the cards available: {exc}")
        self._push_request(request)

    def _verify_same_cards(self, request: GradingRequest) -> tuple[Target, ...]:
        _cards, current_targets = self._inspect(request.card_ids)
        expected = {target.card_id: target.note_guid for target in request.targets}
        for target in current_targets:
            if expected.get(target.card_id) != target.note_guid:
                raise GradingWorkflowError(
                    f"card {target.card_id} changed identity while awaiting confirmation; "
                    "inspect it again before grading"
                )
        return current_targets

    def _apply_fail(self, request: GradingRequest, *, automatic: bool) -> dict[str, Any]:
        col = self._col()
        targets = self._verify_same_cards(request)
        cursor = get_grading_cursor(col, GRADING_STREAM_ID)
        event = EventRef(
            stream_id=GRADING_STREAM_ID,
            sequence=int(cursor["sequence"]) + 1,
            event_id=f"{self.session_id}:{request.id}:{secrets.token_hex(8)}",
        )
        request.event = event
        try:
            result = grade_cards_now(
                col, targets, rating=request.rating, event=event
            )
        except OperationError as exc:
            raise GradingWorkflowError(str(exc)) from exc
        payload = result.to_dict()
        request.result = payload
        request.status = "auto-accepted" if automatic else "accepted"
        request.warnings.extend(str(warning) for warning in payload.get("warnings", []))
        preserved = payload.get("preserved_hidden_state") or {}
        request.available_card_ids = tuple(
            dict.fromkeys(
                [
                    *preserved.get("suspended", []),
                    *preserved.get("user_buried", []),
                    *preserved.get("scheduler_buried", []),
                    *payload.get("newly_suspended", []),
                ]
            )
        )
        self._after_change(list(request.card_ids))
        self._push_request(request)
        return {
            "grading_id": request.id,
            "status": request.status,
            **payload,
            "offer_make_available": list(request.available_card_ids),
        }

    def _apply_make_available(
        self, request: GradingRequest, *, automatic: bool
    ) -> dict[str, Any]:
        self._verify_same_cards(request)
        try:
            result = make_cards_available(self._col(), request.card_ids)
        except OperationError as exc:
            raise GradingWorkflowError(str(exc)) from exc
        payload = result.to_dict()
        request.availability = payload
        restored = set(result.card_ids)
        request.cards = [
            {**card, "hidden_state": None}
            if int(card["card_id"]) in restored
            else card
            for card in request.cards
        ]
        request.status = "auto-accepted" if automatic else "accepted"
        self._after_change(list(request.card_ids))
        self._push_request(request)
        return {
            "grading_id": request.id,
            "status": request.status,
            **payload,
        }
