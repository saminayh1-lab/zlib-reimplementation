#!/usr/bin/env python3
"""Verifier for zlib-reimplementation: build the candidate, run it in a jail, score it.

Pipeline (test.sh execs this as root in the verifier container):
  1. copy src/**/*.rs from the captured /app/zlib-rs into a clean build tree with the pristine
     Cargo.toml (refusing sources that reach outside the crate: #[link], extern crate beyond std,
     .incbin, files that are not text, more than 2 MB in all) and build it as the unprivileged
     `agent` user, statically linked;
  2. place the binary alone in an empty chroot, so at grading time it has no libraries, no other
     programs and no files to lean on;
  3. inflate stage: every corpus stream, expected output hash or expected rejection;
  4. deflate stage: every plain sample at five levels in three containers, each output decoded by
     zlib and compared with the input, sizes kept for the ratio ladder;
  5. speed stage: inflate and deflate throughput against zlib on the two performance inputs;
  6. evidence.json -> compute_reward.score -> reward.json / reward.txt.

Dev use outside Harbor:
  python3 verify.py --dev --binary ./rz --corpus ./corpus --out ./out
"""
import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import compute_reward  # noqa: E402

GRADER = Path(os.environ.get("ZR_GRADER", "/root/grader"))
APP_SRC = Path("/app/zlib-rs/src")
BUILD = Path("/build")
JAIL = Path("/jail")
WORK = Path("/work")
AGENT_UID = 1000
AGENT_ENV = {"HOME": "/home/agent", "USER": "agent", "LOGNAME": "agent", "LANG": "C.UTF-8",
             "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "RUSTFLAGS": "-C target-feature=+crt-static", "CARGO_NET_OFFLINE": "true"}

BUILD_CAP = 900
WORKERS = 3
INFLATE_BUDGET = 1200
DEFLATE_BUDGET = 1500
PERF_CAP = 300
LEVELS = (0, 1, 3, 6, 9)
FORMATS = (("raw", -15), ("zlib", 15), ("gzip", 31))
MEM_LIMIT = 3 << 30

ZLIB_INFLATE = "import sys,zlib;sys.stdout.buffer.write(zlib.decompress(sys.stdin.buffer.read()))"
ZLIB_DEFLATE = "import sys,zlib;sys.stdout.buffer.write(zlib.compress(sys.stdin.buffer.read(),6)[2:-4])"


class Ctx:
    """Where things are and how the candidate is run; differs between Harbor and dev mode."""

    def __init__(self, vdir, corpus, binary=None, jail=True):
        self.vdir = Path(vdir)
        self.corpus = Path(corpus)
        self.binary = binary
        self.jail = jail
        self.manifest = json.load(open(self.corpus / "manifest.json"))

    def argv(self, args):
        if self.jail:
            return ["chroot", f"--userspec={AGENT_UID}:{AGENT_UID}", str(JAIL), "/rz", *args]
        return [str(self.binary), *args]


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _limits():
    resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT, MEM_LIMIT))
    os.setsid()


def run_proc(argv, in_path, out_path, timeout, cwd=None):
    """Run argv with stdin/stdout on files. Returns (returncode or 'timeout', seconds)."""
    with open(in_path, "rb") as fin, open(out_path, "wb") as fout:
        t0 = time.perf_counter()
        proc = subprocess.Popen(argv, stdin=fin, stdout=fout, stderr=subprocess.DEVNULL, preexec_fn=_limits, cwd=cwd)
        try:
            proc.wait(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            rc = "timeout"
        return rc, time.perf_counter() - t0


# ---------------------------------------------------------------- build + jail

def write_invalid(vdir):
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "reward.json").write_text('{"reward": 0.0, "valid": 0}\n')
    (vdir / "reward.txt").write_text("0.0\n")


ALLOWED_EXTERN = {"std", "core", "alloc", "test", "proc_macro"}
BANNED = (
    (re.compile(r"#\s*!?\s*\[\s*link\b"), "link_attribute"),           # would pull a system library into the binary
    (re.compile(r"\.incbin\b"), "incbin_directive"),                      # raw file inclusion through inline assembly
    (re.compile(r"\bextern\s+crate\s+(\w+)"), "extern_crate"),
)
SOURCE_CAP = 2 << 20


def scan_source(text):
    """Return a hard-fail reason if the source reaches outside the crate, else ''."""
    for pat, reason in BANNED:
        for m in pat.finditer(text):
            if reason == "extern_crate" and m.group(1) in ALLOWED_EXTERN:
                continue
            return reason
    return ""


def collect_sources():
    """Copy the candidate's .rs files into the build tree. Returns a hard-fail reason or ''."""
    if not (APP_SRC / "main.rs").is_file():
        return "no_main_rs"
    if BUILD.exists():
        shutil.rmtree(BUILD)
    (BUILD / "src").mkdir(parents=True)
    total = 0
    for p in sorted(APP_SRC.rglob("*.rs")):
        if not p.is_file():
            continue
        data = p.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"source_not_text:{p.relative_to(APP_SRC)}"
        reason = scan_source(text)
        if reason:
            return f"{reason}:{p.relative_to(APP_SRC)}"
        rel = p.relative_to(APP_SRC)
        dest = BUILD / "src" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        total += len(data)
    if total > SOURCE_CAP:
        return "sources_too_large"
    shutil.copy2(GRADER / "Cargo.toml", BUILD / "Cargo.toml")
    subprocess.run(["chown", "-R", f"{AGENT_UID}:{AGENT_UID}", str(BUILD)], check=False)
    return ""


def build(log_path):
    argv = ["runuser", "-u", "agent", "--", "env", *(f"{k}={v}" for k, v in AGENT_ENV.items()),
            "cargo", "build", "--release", "--offline"]
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(argv, cwd=str(BUILD), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            proc.wait(timeout=BUILD_CAP)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return False
    return proc.returncode == 0 and (BUILD / "target" / "release" / "rz").is_file()


def is_static_elf(path):
    """True when the ELF has no PT_INTERP program header."""
    with open(path, "rb") as fh:
        hdr = fh.read(64)
        if hdr[:4] != b"\x7fELF" or hdr[4] != 2:
            return False
        e_phoff = int.from_bytes(hdr[32:40], "little")
        e_phentsize = int.from_bytes(hdr[54:56], "little")
        e_phnum = int.from_bytes(hdr[56:58], "little")
        fh.seek(e_phoff)
        for _ in range(e_phnum):
            ph = fh.read(e_phentsize)
            if int.from_bytes(ph[:4], "little") == 3:  # PT_INTERP
                return False
    return True


def stage_jail(binary):
    if JAIL.exists():
        shutil.rmtree(JAIL)
    JAIL.mkdir(mode=0o755)
    shutil.copy2(binary, JAIL / "rz")
    os.chown(JAIL / "rz", 0, 0)
    os.chmod(JAIL / "rz", 0o755)


# ---------------------------------------------------------------- stages

def inflate_stage(ctx):
    cases = ctx.manifest["inflate"]
    deadline = time.monotonic() + INFLATE_BUDGET
    outdir = WORK / "inflate"
    outdir.mkdir(parents=True, exist_ok=True)

    def one(case):
        res = {"id": case["id"], "group": case["group"], "expect": case["expect"], "passed": False}
        if time.monotonic() > deadline:
            res["note"] = "stage deadline"
            return res
        in_path = ctx.corpus / "inflate" / (case["id"] + ".in")
        out_path = outdir / case["id"]
        timeout = 20 if case["in_len"] < (1 << 20) else 90
        rc, secs = run_proc(ctx.argv(["-d", "-f", case["fmt"]]), in_path, out_path, timeout)
        res["rc"] = rc
        res["sec"] = round(secs, 4)
        if case["expect"] == "ok":
            res["passed"] = rc == 0 and out_path.stat().st_size == case["out_len"] and sha_file(out_path) == case["sha256"]
        else:
            res["passed"] = rc == 1
        try:
            out_path.unlink()
        except OSError:
            pass
        return res

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(one, cases))
    passed = sum(r["passed"] for r in results)
    print(f"inflate: {passed}/{len(results)} cases passed")
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['id']} expect={r['expect']} rc={r.get('rc')} {r.get('note', '')}")
    return results


def deflate_stage(ctx):
    plain = ctx.manifest["plain"]
    deadline = time.monotonic() + DEFLATE_BUDGET
    outdir = WORK / "deflate"
    outdir.mkdir(parents=True, exist_ok=True)
    jobs = [(p, level, fmt, wbits) for p in plain for level in LEVELS for fmt, wbits in FORMATS]

    def one(job):
        p, level, fmt, wbits = job
        res = {"sample": p["name"], "level": level, "fmt": fmt, "passed": False, "size": 0}
        if time.monotonic() > deadline:
            res["note"] = "stage deadline"
            return res
        in_path = ctx.corpus / "plain" / (p["name"] + ".bin")
        out_path = outdir / f"{p['name']}_L{level}_{fmt}"
        timeout = 60 if p["size"] < (1 << 20) else 180
        rc, secs = run_proc(ctx.argv(["-c", "-f", fmt, "-l", str(level)]), in_path, out_path, timeout)
        res["rc"] = rc
        res["sec"] = round(secs, 4)
        if rc == 0:
            data = out_path.read_bytes()
            res["size"] = len(data)
            try:
                d = zlib.decompressobj(wbits)
                back = d.decompress(data) + d.flush()
                ok = d.eof and not d.unused_data
                if fmt == "gzip" and ok:
                    ok = len(data) >= 18 and data[:3] == b"\x1f\x8b\x08"
                res["passed"] = ok and hashlib.sha256(back).hexdigest() == p["sha256"] and len(back) == p["size"]
            except zlib.error:
                res["passed"] = False
        try:
            out_path.unlink()
        except OSError:
            pass
        return res

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(one, jobs))
    passed = sum(r["passed"] for r in results)
    print(f"deflate: {passed}/{len(results)} outputs valid")
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['sample']} L{r['level']} {r['fmt']} rc={r.get('rc')} {r.get('note', '')}")
    return results


def perf_stage(ctx):
    perf = ctx.manifest["perf"]
    outdir = WORK / "perf"
    outdir.mkdir(parents=True, exist_ok=True)
    result = {}

    def measure(name, cand_args, baseline_code, in_path, check):
        out_c = outdir / f"{name}_cand"
        out_b = outdir / f"{name}_base"
        rc, _ = run_proc(ctx.argv(cand_args), in_path, out_c, PERF_CAP)
        ok = rc == 0 and check(out_c)
        entry = {"ok": ok, "rc": rc}
        if ok:
            cand_times, base_times = [], []
            for _ in range(3):
                rc, s = run_proc(ctx.argv(cand_args), in_path, out_c, PERF_CAP)
                if rc != 0:
                    entry["ok"] = False
                    break
                cand_times.append(s)
                _, s = run_proc(["python3", "-c", baseline_code], in_path, out_b, PERF_CAP)
                base_times.append(s)
            if entry["ok"]:
                entry["cand_sec"] = round(min(cand_times), 4)
                entry["zlib_sec"] = round(min(base_times), 4)
                entry["cand_runs"] = [round(x, 4) for x in cand_times]
                entry["zlib_runs"] = [round(x, 4) for x in base_times]
        for p in (out_c, out_b):
            try:
                p.unlink()
            except OSError:
                pass
        print(f"perf {name}: {entry}")
        return entry

    def inflate_ok(path):
        return path.stat().st_size == perf["inflate_out_len"] and sha_file(path) == perf["inflate_out_sha256"]

    deflate_in = ctx.corpus / perf["deflate_in"]
    deflate_sha = sha_file(deflate_in)

    def deflate_ok(path):
        d = zlib.decompressobj(-15)
        h = hashlib.sha256()
        n = 0
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                piece = d.decompress(chunk)
                h.update(piece)
                n += len(piece)
        tail = d.flush()
        h.update(tail)
        n += len(tail)
        return d.eof and not d.unused_data and n == perf["deflate_in_len"] and h.hexdigest() == deflate_sha

    result["inflate"] = measure("inflate", ["-d", "-f", "zlib"], ZLIB_INFLATE, ctx.corpus / perf["inflate_in"], inflate_ok)
    result["deflate"] = measure("deflate", ["-c", "-f", "raw", "-l", "6"], ZLIB_DEFLATE, deflate_in, deflate_ok)
    return result


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", action="store_true", help="score a prebuilt binary outside Harbor")
    ap.add_argument("--binary")
    ap.add_argument("--corpus")
    ap.add_argument("--out", default="/logs/verifier")
    ap.add_argument("--jail", action="store_true", help="dev mode: still run inside the chroot jail")
    ap.add_argument("--skip-perf", action="store_true")
    args = ap.parse_args()

    vdir = Path(args.out)
    vdir.mkdir(parents=True, exist_ok=True)
    if not args.dev:
        os.chmod(vdir, 0o700)
    write_invalid(vdir)
    global WORK
    if args.dev:
        WORK = vdir / "work"
    WORK.mkdir(parents=True, exist_ok=True)

    t_start = time.time()
    evidence = {"hard_fail": "", "timing": {}}
    if args.dev:
        ctx = Ctx(vdir, args.corpus, binary=os.path.abspath(args.binary), jail=args.jail)
        if args.jail:
            stage_jail(ctx.binary)
    else:
        ctx = Ctx(vdir, GRADER / "corpus")
        print("=== build ===")
        reason = collect_sources()
        if not reason:
            ok = build(vdir / "build.log")
            if not ok:
                reason = "build_failed"
                print((vdir / "build.log").read_text(errors="replace")[-4000:])
        if not reason:
            binary = BUILD / "target" / "release" / "rz"
            if not is_static_elf(binary):
                reason = "dynamic_binary"
            else:
                stage_jail(binary)
        evidence["hard_fail"] = reason
        evidence["timing"]["build_sec"] = round(time.time() - t_start, 1)
        print(f"build: {'OK' if not reason else reason}")

    if not evidence["hard_fail"]:
        t = time.time()
        evidence["inflate"] = inflate_stage(ctx)
        evidence["timing"]["inflate_sec"] = round(time.time() - t, 1)
        t = time.time()
        evidence["deflate"] = deflate_stage(ctx)
        evidence["timing"]["deflate_sec"] = round(time.time() - t, 1)
        evidence["zlib_sizes"] = ctx.manifest["zlib_sizes"]
        inflate_score, _ = compute_reward.score_inflate(evidence["inflate"])
        deflate_score = compute_reward.score_deflate(evidence["deflate"])
        if args.skip_perf:
            evidence["perf"] = {}
        elif inflate_score >= 0.99 and deflate_score >= 1.0:
            t = time.time()
            evidence["perf"] = perf_stage(ctx)
            evidence["timing"]["perf_sec"] = round(time.time() - t, 1)
        else:
            evidence["perf"] = {}
            print("perf: skipped, the program is not yet correct on both stages")
    evidence["timing"]["total_sec"] = round(time.time() - t_start, 1)

    ev_path = vdir / "evidence.json"
    ev_path.write_text(json.dumps(evidence, indent=1))
    compute_reward.score(str(vdir), str(ev_path))
    print(json.dumps(json.load(open(vdir / "reward.json")), indent=1))
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        try:
            out = Path("/logs/verifier")
            if not (out / "reward.json").exists():
                write_invalid(out)
        except Exception:  # noqa: BLE001
            pass
    sys.exit(0)
