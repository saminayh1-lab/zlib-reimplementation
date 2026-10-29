#!/bin/bash
# Reference solution: the vendored miniz_oxide DEFLATE core (MIT) behind a CLI that implements the
# container rules of the contract. Stages the sources into the workspace and builds them the way the
# grader does, then does one round trip as a smoke test.
set -eu
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
rm -rf /app/zlib-rs/src
cp -r "$here/src" /app/zlib-rs/src
cd /app/zlib-rs
RUSTFLAGS="-C target-feature=+crt-static" cargo build --release --offline
./target/release/rz -c -f gzip -l 9 < README.md | ./target/release/rz -d -f gzip | cmp - README.md
echo "oracle: reference sources staged in /app/zlib-rs/src and built"
if [ "$(id -u)" = 0 ]; then
    chown -R agent:agent /app/zlib-rs
fi
