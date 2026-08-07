---
title: "Chat With Your Cards"
tags: anki addon ai assistant collection study
support_url: https://github.com/ritornello-labs/chat-with-your-cards
---

Chat With Your Cards adds a review-aware AI assistant beside Anki. Ask about the current card, search your collection for prerequisites and related notes, inspect study history, and review proposed changes before anything is applied.

![Request a focused companion card beside the reviewer](https://ritornello.dev/media/ankiweb/2026-08-06-v4/chat-with-your-cards/gallery-01.png)

![Review the proposed front, back, deck, and tags](https://ritornello.dev/media/ankiweb/2026-08-06-v4/chat-with-your-cards/gallery-02.png)

[3.4-second full-resolution workflow MP4](https://ritornello.dev/media/ankiweb/2026-08-06-v4/chat-with-your-cards/demo.mp4)

## Requirements

- Anki 25.09.
- The official [Claude Code](https://claude.com/claude-code) CLI version 2.1.220 or newer, installed and signed in. Claude Code is the only supported AI backend in v0.1.0; Codex and Pi support are planned.
- macOS or Linux. Windows support is experimental in this preview.

CWYC does not accept or store API keys. If Claude Code is missing, the add-on opens in a built-in demonstration mode and provides setup instructions plus a no-restart Re-check action.

## Safety and privacy

Collection changes use reviewable proposals by default. Destructive operations have additional confirmation and backup safeguards. The collection tool server is loopback-only and protected by a random per-session token; shell and file-writing tools are disabled by default.

Messages and collection context needed for a request are processed through your installed Claude Code CLI. CWYC itself collects no telemetry. Read the full [privacy statement](https://github.com/ritornello-labs/chat-with-your-cards/blob/main/PRIVACY.md) and [security model](https://github.com/ritornello-labs/chat-with-your-cards/blob/main/SECURITY.md).

GitHub: [https://github.com/ritornello-labs/chat-with-your-cards](https://github.com/ritornello-labs/chat-with-your-cards)
