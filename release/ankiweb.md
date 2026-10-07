---
title: "Chat With Your Cards"
tags: anki addon ai assistant collection study
support_url: https://github.com/ritornello-labs/chat-with-your-cards
---

<img src="https://ritornello.dev/media/brand/listing-banner-v1.png" alt="Ritornello" width="700">

[Explore all Ritornello decks and add-ons](https://ritornello.dev/).

Chat With Your Cards adds a review-aware AI assistant beside Anki. Ask about the current card, find prerequisites in your collection, inspect study history, and review proposed changes before applying them.

## See it in Anki

![Find prerequisites and review a proposed companion note in native Anki](https://ritornello.dev/media/ankiweb/2026-10-07-v7/chat-with-your-cards/conversation.gif)

![Review and apply an indigo theme change without altering collection notes](https://ritornello.dev/media/ankiweb/2026-10-07-v7/chat-with-your-cards/settings.gif)

These demonstration conversations run inside real Anki. The assistant can connect a difficult card to related notes and propose a focused companion card for you to review.

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
