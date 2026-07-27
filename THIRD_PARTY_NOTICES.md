# Third-party notices

## Safe Collection Operations for Anki Add-ons

CWYC contains a source-vendored copy of the Safe Collection Operations Python
core, version 0.1.0, at upstream commit
`7add67d6ee3a01950012480442de19f870ecc148`.

Copyright Elvis Sikora. Licensed under the MIT License. The upstream project
is available at
<https://github.com/ritornello-labs/anki-addon-safe-collection-operations>.

## First-party vendored UI packages

`ui/vendor/` contains built tarballs of the first-party packages
`@elvis-labs/interaction-schema` and `@elvis-labs/interaction-ui-react`.
Copyright Elvis Sikora, licensed under the MIT License (declared upstream;
tarballs built before the declaration are covered by the same grant). Their
source repository is not yet public; the tarballs are committed so this
repository builds hermetically. See `ui/README.md` for how they are
consumed.

## Bundled JavaScript libraries

The committed web bundles (`chat_with_your_cards/web/next/bundle.js`,
`bundle.css`, `mermaid.bundle.js`) contain the following libraries, each
under its own MIT or BSD-style license (full texts travel with the packages
in `ui/package-lock.json`'s resolved distributions):

- React and ReactDOM (MIT, Meta Platforms)
- @assistant-ui/react and assistant-stream (MIT)
- CodeMirror 6 (`@codemirror/*`) (MIT) and @replit/codemirror-vim (MIT)
- marked (MIT), DOMPurify (Apache-2.0 OR MPL-2.0), KaTeX (MIT),
  Mermaid (MIT), zustand (MIT), Radix UI primitives (MIT)

## IBM Plex fonts

The IBM Plex Sans and IBM Plex Mono latin woff2 subsets are shipped in
`ui/src/assets/fonts/` and inlined (base64) into the committed `bundle.css`.
Copyright © IBM Corp., licensed under the SIL Open Font License 1.1 — the
full license text is included at `ui/src/assets/fonts/OFL.txt`.
