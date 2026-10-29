#!/bin/sh
# Install the apt packages listed in packages.txt, wire `fd`, clean the apt lists.
set -eu
here="$(dirname "$0")"
apt-get update
sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$here/packages.txt" | xargs -r apt-get install -y
ln -s "$(command -v fdfind)" /usr/local/bin/fd
rm -rf /var/lib/apt/lists/*

# No deflate implementation may be linkable from the agent's crate. The dev packages that come in
# with the toolchain bring zlib's static library and headers; take them out again and make sure the
# archive is gone. The runtime libz.so.1 stays (python3 needs it) but a static-pie binary cannot
# link a shared object, and the grader runs the binary in an empty chroot anyway.
apt-get purge -y zlib1g-dev libpython3.12-dev python3-dev libexpat1-dev 2>/dev/null || true
apt-get autoremove -y --purge 2>/dev/null || true
rm -f /usr/lib/x86_64-linux-gnu/libz.a /usr/lib/x86_64-linux-gnu/libz.so /usr/include/zlib.h /usr/include/zconf.h
rm -f /usr/lib/x86_64-linux-gnu/libexpat.a /usr/lib/x86_64-linux-gnu/libexpatw.a
test ! -e /usr/lib/x86_64-linux-gnu/libz.a
test ! -e /usr/include/zlib.h
rustc --version && cargo --version && python3 -c "import zlib"
rm -rf /var/lib/apt/lists/*
