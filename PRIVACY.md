# Privacy

Chat With Your Cards (CWYC) runs inside Anki and spawns the user's officially installed Claude Code CLI. CWYC does not operate an AI API, accept API keys, run a remote service, or collect telemetry.

## Data sent through Claude Code

When you send a message, Claude Code may receive the message, the current card's fields and scheduling context, collection information returned by tools, attachments you deliberately add, and local or web sources the assistant is asked to read. Tool results can contain note text, deck and tag names, statistics, template source, and other collection data needed for the request.

That processing is governed by the account and provider policies attached to your Claude Code installation. CWYC never reads or extracts Claude Code's stored credentials.

## Local storage

CWYC stores configuration, chat transcripts, learned conventions, cached collection statistics, staged attachments, proposal history, and diagnostic logs under the add-on's `user_files/` directory. Those files remain on the local machine unless another program or backup system copies them. Uninstalling an Anki add-on may preserve its `user_files/` directory so an upgrade or reinstall does not erase user data.

## Network and computer access

The collection tool server binds only to loopback and requires a random per-session bearer token. By default, unrelated user MCP servers are not exposed to the chat and shell/file-writing tools are disabled. Read access is available so the assistant can inspect sources you identify; web search and page fetching are enabled by default and can be disabled in settings.

Downloaded decks and web pages are untrusted input. Do not enable broader computer-tool or MCP access for collections or sources you do not trust.

## Deleting local data

Close Anki, then remove CWYC's `user_files/` directory from the installed add-on folder to delete its transcripts, learned conventions, caches, staged files, and logs. This does not delete or modify the Anki notes already accepted into the collection.
