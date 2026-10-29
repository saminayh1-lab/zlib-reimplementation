//! rz: reference compressor and decompressor for the raw deflate, zlib and gzip containers.
//!
//! The DEFLATE core is a vendored copy of the miniz_oxide crate (MIT), compiled here as a
//! module so that the whole program is one dependency-free crate. The container handling,
//! the checksums and the strictness rules of the CLI contract live in this file.

extern crate alloc;

#[allow(dead_code, unused_imports)]
mod adler2;
#[allow(dead_code, unused_imports)]
mod miniz_oxide;

use std::io::{self, Read, Write};
use std::process::exit;

use miniz_oxide::inflate::core::inflate_flags::TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF;
use miniz_oxide::inflate::core::{decompress, DecompressorOxide};
use miniz_oxide::inflate::TINFLStatus;

#[derive(Clone, Copy, PartialEq)]
enum Format {
    Raw,
    Zlib,
    Gzip,
}

struct Args {
    decompress: bool,
    format: Format,
    level: u8,
}

fn usage() -> ! {
    eprintln!("usage: rz (-d | -c) [-f raw|zlib|gzip] [-l 0..9]");
    exit(2);
}

fn parse_args() -> Args {
    let mut args = Args { decompress: false, format: Format::Zlib, level: 6 };
    let mut mode_set = false;
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "-d" => { args.decompress = true; mode_set = true; }
            "-c" => { args.decompress = false; mode_set = true; }
            "-f" => {
                args.format = match it.next().as_deref() {
                    Some("raw") => Format::Raw,
                    Some("zlib") => Format::Zlib,
                    Some("gzip") => Format::Gzip,
                    _ => usage(),
                }
            }
            "-l" => {
                args.level = match it.next().and_then(|s| s.parse::<u8>().ok()) {
                    Some(l) if l <= 9 => l,
                    _ => usage(),
                }
            }
            _ => usage(),
        }
    }
    if !mode_set {
        usage();
    }
    args
}

// ---------------------------------------------------------------- checksums

fn crc32_table() -> [u32; 256] {
    let mut t = [0u32; 256];
    for i in 0..256u32 {
        let mut c = i;
        for _ in 0..8 {
            c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
        }
        t[i as usize] = c;
    }
    t
}

fn crc32(data: &[u8]) -> u32 {
    let t = crc32_table();
    let mut c = 0xFFFF_FFFFu32;
    for &b in data {
        c = t[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    !c
}

fn adler32(data: &[u8]) -> u32 {
    let mut h = adler2::Adler32::new();
    h.write_slice(data);
    h.checksum()
}

// ---------------------------------------------------------------- inflate

struct Bad;

/// Inflate one raw deflate stream that starts at the beginning of `input`.
/// Returns the output and the number of input bytes the stream occupied.
fn inflate_raw(input: &[u8]) -> Result<(Vec<u8>, usize), Bad> {
    let flags = TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF;
    let mut out: Vec<u8> = vec![0; input.len().saturating_mul(3).max(64 * 1024)];
    let mut state = Box::<DecompressorOxide>::default();
    let mut in_pos = 0usize;
    let mut out_pos = 0usize;
    loop {
        let (status, consumed, written) = decompress(&mut state, &input[in_pos..], &mut out, out_pos, flags);
        in_pos += consumed;
        out_pos += written;
        match status {
            TINFLStatus::Done => {
                out.truncate(out_pos);
                return Ok((out, in_pos));
            }
            TINFLStatus::HasMoreOutput => {
                let new_len = out.len().saturating_mul(2);
                out.resize(new_len, 0);
            }
            _ => return Err(Bad),
        }
    }
}

fn le32(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}

fn be32(b: &[u8]) -> u32 {
    u32::from_be_bytes([b[0], b[1], b[2], b[3]])
}

fn inflate_zlib(input: &[u8]) -> Result<Vec<u8>, Bad> {
    if input.len() < 2 {
        return Err(Bad);
    }
    let cmf = input[0];
    let flg = input[1];
    if cmf & 0x0F != 8 || cmf >> 4 > 7 {
        return Err(Bad);
    }
    if (cmf as u32 * 256 + flg as u32) % 31 != 0 {
        return Err(Bad);
    }
    if flg & 0x20 != 0 {
        return Err(Bad); // preset dictionary: not part of the contract
    }
    let (out, used) = inflate_raw(&input[2..])?;
    let rest = &input[2 + used..];
    if rest.len() != 4 {
        return Err(Bad);
    }
    if be32(rest) != adler32(&out) {
        return Err(Bad);
    }
    Ok(out)
}

fn inflate_gzip(input: &[u8]) -> Result<Vec<u8>, Bad> {
    let mut pos = 0usize;
    let mut out = Vec::new();
    if input.is_empty() {
        return Err(Bad);
    }
    while pos < input.len() {
        let h = &input[pos..];
        if h.len() < 10 || h[0] != 0x1f || h[1] != 0x8b || h[2] != 8 {
            return Err(Bad);
        }
        let flg = h[3];
        if flg & 0xE0 != 0 {
            return Err(Bad);
        }
        let mut p = 10usize;
        if flg & 0x04 != 0 {
            if h.len() < p + 2 {
                return Err(Bad);
            }
            let xlen = u16::from_le_bytes([h[p], h[p + 1]]) as usize;
            p += 2 + xlen;
        }
        for bit in [0x08u8, 0x10u8] {
            if flg & bit != 0 {
                match h[p.min(h.len())..].iter().position(|&c| c == 0) {
                    Some(n) => p += n + 1,
                    None => return Err(Bad),
                }
            }
        }
        if flg & 0x02 != 0 {
            if h.len() < p + 2 {
                return Err(Bad);
            }
            let want = u16::from_le_bytes([h[p], h[p + 1]]);
            if (crc32(&h[..p]) & 0xFFFF) as u16 != want {
                return Err(Bad);
            }
            p += 2;
        }
        if p > h.len() {
            return Err(Bad);
        }
        let (member, used) = inflate_raw(&h[p..])?;
        let t = &h[p + used..];
        if t.len() < 8 {
            return Err(Bad);
        }
        if le32(t) != crc32(&member) || le32(&t[4..]) != (member.len() as u32) {
            return Err(Bad);
        }
        out.extend_from_slice(&member);
        pos += p + used + 8;
    }
    Ok(out)
}

fn do_decompress(format: Format, input: &[u8]) -> Result<Vec<u8>, Bad> {
    match format {
        Format::Raw => {
            let (out, used) = inflate_raw(input)?;
            if used != input.len() {
                return Err(Bad);
            }
            Ok(out)
        }
        Format::Zlib => inflate_zlib(input),
        Format::Gzip => inflate_gzip(input),
    }
}

// ---------------------------------------------------------------- deflate

fn do_compress(format: Format, level: u8, input: &[u8]) -> Vec<u8> {
    let core_level = if level == 9 { 10 } else { level };
    let body = miniz_oxide::deflate::compress_to_vec(input, core_level);
    match format {
        Format::Raw => body,
        Format::Zlib => {
            let mut out = Vec::with_capacity(body.len() + 6);
            let cmf = 0x78u8;
            let flevel: u8 = match level { 0 | 1 => 0, 2..=5 => 1, 6 => 2, _ => 3 };
            let mut flg = flevel << 6;
            let rem = ((cmf as u32) * 256 + flg as u32) % 31;
            if rem != 0 {
                flg += (31 - rem) as u8;
            }
            out.push(cmf);
            out.push(flg);
            out.extend_from_slice(&body);
            out.extend_from_slice(&adler32(input).to_be_bytes());
            out
        }
        Format::Gzip => {
            let mut out = Vec::with_capacity(body.len() + 18);
            let xfl: u8 = match level { 1 => 4, 9 => 2, _ => 0 };
            out.extend_from_slice(&[0x1f, 0x8b, 8, 0, 0, 0, 0, 0, xfl, 255]);
            out.extend_from_slice(&body);
            out.extend_from_slice(&crc32(input).to_le_bytes());
            out.extend_from_slice(&(input.len() as u32).to_le_bytes());
            out
        }
    }
}

fn main() {
    let args = parse_args();
    let mut input = Vec::new();
    if io::stdin().lock().read_to_end(&mut input).is_err() {
        eprintln!("rz: cannot read stdin");
        exit(2);
    }
    let output = if args.decompress {
        match do_decompress(args.format, &input) {
            Ok(v) => v,
            Err(Bad) => {
                eprintln!("rz: input is not a valid stream");
                exit(1);
            }
        }
    } else {
        do_compress(args.format, args.level, &input)
    };
    let stdout = io::stdout();
    let mut lock = stdout.lock();
    if lock.write_all(&output).and_then(|_| lock.flush()).is_err() {
        exit(2);
    }
}
