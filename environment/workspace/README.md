# rz: deflate, zlib and gzip in Rust

Build a program that compresses and decompresses the three container formats around DEFLATE: raw
deflate (RFC 1951), zlib (RFC 1950) and gzip (RFC 1952). The RFCs are in `/app/docs`. The grader
holds the result to what zlib does, byte for byte on decompression, and to zlib's compression ratio
and speed on compression.

## Command line

    rz -d [-f raw|zlib|gzip]            decompress stdin to stdout
    rz -c [-f raw|zlib|gzip] [-l 0..9]  compress stdin to stdout

`-f` defaults to `zlib`, `-l` to 6. Exit 0 on success. Exit 1 when the input is not a valid stream
of the named format. What is on stdout then does not matter. Exit 2 for a usage error. Any other
exit status, a panic or a signal counts as a failure for that input.

## What "valid" means when decompressing

zlib's verdict is the rule. A stream zlib decodes must decode to the same bytes, and a stream
zlib rejects must be rejected. In particular: a dynamic block may carry a distance tree with a single
code or with no codes at all, lengths may be repeated across the boundary between the literal and
the distance code lengths, and length 258 may be spelled with code 284 and five extra bits. Over
subscribed or incomplete trees are rejected except for those single-code cases, as are symbols
286, 287, distance codes 30 and 31, a distance that reaches before the start of the output, bad
stored block lengths and block type 3. A stream that ends before its final block is complete is an
error.

Two rules are part of this contract and not something zlib decides. Bytes left over after the end of the
stream (after the final block in raw mode, after the Adler-32 in zlib mode, after the last member
in gzip mode) are an error. A zlib header with FDICT set is an error, since there is no dictionary.
The window size announced in the zlib header is not enforced. Any distance up to 32768 is fine as
long as the data exists.

zlib mode checks CM = 8, CINFO <= 7, FCHECK and the Adler-32. gzip mode accepts one or more
members back to back, each with CM = 8, no reserved flag bits, FEXTRA, FNAME, FCOMMENT and FHCRC
handled (the header CRC verified when present), CRC-32 and ISIZE verified. An empty input is an
error in every format.

## What "valid" means when compressing

Any output zlib decodes back to the input, in the container named by `-f`. Nothing else about the
bytes is checked, so the choice of block types, Huffman trees and matching strategy is yours.
Levels 0 to 9 all have to produce valid output. Levels 6 and 9 are also measured for size against
zlib at the same level, and level 6 is measured for speed.

## How the grader builds and runs your program

Only `src/**/*.rs` is taken from this directory. It is compiled with the `Cargo.toml` shipped here
(no dependencies, no `build.rs`), offline, with `RUSTFLAGS="-C target-feature=+crt-static"`, as the
`agent` user, with a 15 minute build limit. The sources have to stand on their own: a `#[link]`
attribute, an `extern crate` other than `std`, `core` or `alloc`, an `.incbin` directive, a `.rs`
file that is not UTF-8 text or more than 2 MB of source in total is refused and scores 0. Inline
assembly and `unsafe` are fine. The static binary is then copied alone into an empty
chroot and run there: no libraries, no other programs, no files besides the one it is reading.
Each run has 3 GB of address space and a time limit: 20 s per decompression under 1 MB of input,
90 s above, 60 s per compression under 1 MB, 180 s above, 300 s for each speed run.

## Scoring

The score is the sum of four parts.

Decompression, 0.40. A hidden corpus of about a thousand streams: zlib output at every level and
strategy in all three containers with several window sizes, flush boundaries, gzip header
variants and multi member files, hand assembled bitstreams for the tree and length edge cases
above, and malformed inputs (truncations, bit flips, trailing bytes, bad checksums and headers)
that must be rejected with exit 1. The valid streams are weighted by group. The rejections count
for 30% of this part, and only once 80% of the valid streams decode.

Compression validity, 0.15. Sixteen plain samples (empty, tiny, text, source code, an ELF
binary, JSON logs, CSV, zeros, runs, random bytes, periodic data inside and beyond the window) at
levels 0, 1, 3, 6 and 9 in all three containers. Every output must decode back to its input.

Compression ratio, 0.25, counted only when every compression output was valid. For level 6 and
for level 9, the mean over samples of (your size / zlib's size at that level). Full marks at or
below 1.015, nothing at 1.25 or above, linear between. The two levels are averaged.

Speed, 0.20, counted only when decompression scores at least 0.99 and every compression output
was valid. Decompression of a 48 MB zlib stream and level 6 compression of a 16 MB input, each the
best of three runs against zlib doing the same job (driven from Python, same file redirections).
The geometric mean of the two throughput ratios goes on a ladder: full marks at 0.60 of zlib,
nothing at 0.10.

A program that fails to build, is not a static ELF, or has no `src/main.rs` scores 0.

## Checking your work

`python3 /app/tests/check.py` builds the crate the way the grader does and runs a public version of
every stage: zlib generated streams, a handful of malformed inputs, round trips at all levels and
containers, ratio against zlib and a speed measurement. It uses different data from the hidden
corpus, and it does not run the chroot, but a program that passes it cleanly is in good shape.
Python's `zlib` module is available for your own experiments.
