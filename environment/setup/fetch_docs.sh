#!/bin/sh
# The three format specifications the agent works from, fetched at build time and pinned by digest.
# The agent sandbox is offline, so these are the only copies it will have.
set -eu
dest=/app/docs
mkdir -p "$dest"
cd "$dest"
curl -fsSL -o rfc1950.txt https://www.rfc-editor.org/rfc/rfc1950.txt
curl -fsSL -o rfc1951.txt https://www.rfc-editor.org/rfc/rfc1951.txt
curl -fsSL -o rfc1952.txt https://www.rfc-editor.org/rfc/rfc1952.txt
sha256sum -c <<'SUMS'
8f0475a5c984657bf26277f73df9456c9b97f175084f0c1748f1eb1f0b9b10b9  rfc1950.txt
5ebf4b5b7fe1c3a0c0ab9aa3ac8c0f3853a7dc484905e76e03b0b0f301350009  rfc1951.txt
164ef0897b4cbec63abf1b57f069f3599bd0fb7c72c2a4dee21bd7e03ec9af67  rfc1952.txt
SUMS
