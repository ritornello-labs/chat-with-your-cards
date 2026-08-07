# Security

CWYC treats card content, attachments, and fetched pages as untrusted model input. Collection writes go through server-enforced permissions and reviewable proposal paths by default. The built-in MCP server binds to loopback, uses a random per-session bearer token, and stops with the add-on session. Shell and file-writing tools are disabled in the default computer-access mode.

Enabling non-sandbox computer tools or additional MCP servers materially widens the impact of prompt injection. Use those modes only with collections and sources you trust.

## Reporting a vulnerability

Please use GitHub's private vulnerability-reporting flow for this repository rather than opening a public issue with exploit details:

<https://github.com/ritornello-labs/chat-with-your-cards/security/advisories/new>

Include the affected version, platform, Anki version, reproduction steps, and the narrowest safe description of impact. Do not include collection exports, credentials, or private card content.
