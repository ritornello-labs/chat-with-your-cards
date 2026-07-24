# Vendored libraries

`safe_collection_operations/` is a source-vendored copy of the transport-free
core from:

- Repository: `https://github.com/ritornello-labs/anki-addon-safe-collection-operations`
- Commit: `7add67d6ee3a01950012480442de19f870ecc148`
- Package version: `0.1.0`
- License: MIT

CWYC vendors the core so its native scheduler operations work without a
runtime dependency on either AnkiConnect or the standalone desktop bridge.
The bridge remains useful for agents running outside CWYC. Keep local changes
out of this directory; update it from a reviewed, pinned upstream commit.
