---
title: "Chat With Your Cards"
tags: anki addon ai assistant collection study
support_url: https://github.com/ritornello-labs/chat-with-your-cards
---

<img src="https://ritornello.dev/media/brand/listing-banner-v1.png" alt="Ritornello" width="700">

[Explore all Ritornello decks and add-ons](https://ritornello.dev/).

Chat With Your Cards adds a review-aware AI assistant beside Anki. Ask about the current card, find prerequisites in your collection, inspect study history, and review proposed changes before applying them.

## See it in Anki

### Understand a card, then improve its question

![Type a question, watch the explanation stream, and accept an edit that updates the card front](https://ritornello.dev/media/ankiweb/2026-10-07-v7/chat-with-your-cards/explain-and-edit.gif)

Ask for context on a simple photosynthesis card, then review a more specific question. Accepting the edit immediately updates the card front. The existing note and card are retained.

### Build a focused practice deck

![Type a filtered-deck request, review the streamed reply and proposal, then accept to gather five cards](https://ritornello.dev/media/ankiweb/2026-10-07-v7/chat-with-your-cards/filtered-deck.gif)

Ask for five photosynthesis cards in a filtered deck. Review the search, card limit, and scheduling behaviour before accepting; the new practice deck appears with all five cards gathered and normal rescheduling disabled.

Recorded in real Anki using the built-in demo backend and a disposable collection. Replies stream through the actual chat UI; accepting each proposal applies the real edit or deck action. Typing is sped up and excess still time is trimmed for readability.

## New in 0.1.1

- Ask chat to change common CWYC settings; each settings change waits for your review, even when collection writes are allowed.
- Slash commands and skill invocations reach Claude Code correctly; `/compact` reports completion.
- Your accepted, rejected, and partial proposal decisions reach the assistant on your next message.
- Starting a new chat clears the previous chat's review state.
- The context meter reports current session occupancy correctly.

## Requirements

- Anki 25.09 or newer.
- The official [Claude Code](https://claude.com/claude-code) CLI, version 2.1.220 or newer, installed and signed in. Claude Code is the supported AI backend in 0.1.1.
- macOS or Linux. Windows support is experimental.

If Claude Code is missing, the add-on offers a built-in demonstration mode, setup instructions, and a Re-check action. CWYC does not accept or store API keys.

## Safety and privacy

Collection changes use reviewable proposals by default. Destructive operations have confirmation and backup safeguards. Shell and file-writing tools are disabled by default.

Messages and collection context needed for a request are processed through your installed Claude Code CLI. CWYC itself collects no telemetry. See the [privacy statement](https://github.com/ritornello-labs/chat-with-your-cards/blob/main/PRIVACY.md) and [security model](https://github.com/ritornello-labs/chat-with-your-cards/blob/main/SECURITY.md).

GitHub: [https://github.com/ritornello-labs/chat-with-your-cards](https://github.com/ritornello-labs/chat-with-your-cards)

Support continued development: [ritornello.dev/support](https://ritornello.dev/support).
