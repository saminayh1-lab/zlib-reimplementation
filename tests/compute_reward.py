#!/usr/bin/env python3
"""Scoring for zlib-reimplementation. Reads the evidence verify.py wrote, never the agent's code.

reward = W_INFLATE * inflate + W_DEFLATE * deflate_ok + W_RATIO * ratio * [deflate_ok == 1]
         + W_SPEED * speed * [inflate >= 0.99 and deflate_ok == 1]

inflate     valid streams: weighted pass fraction over the groups std / gzip_hdr / hand, then
            0.7 * that + 0.3 * (fraction of malformed streams rejected with exit 1). The rejects
            only count once at least 80% of the valid streams decode, so a program that fails
            everything earns nothing from them.
deflate_ok  fraction of (sample, level, container) outputs that zlib decodes back to the input.
ratio       per-sample compressed size against zlib at the same level, mean over the samples,
            averaged over levels 6 and 9, on a ladder from RATIO_ZERO (nothing) down to
            RATIO_FULL (full marks). Only counted when
            every deflate output was valid: an invalid stream can be arbitrarily small.
speed       throughput against zlib on the two performance inputs, geometric mean of inflate and
            deflate, on a ladder from SPEED_ZERO to SPEED_FULL. Only counted on a correct program.

Gates: no candidate (no src/main.rs, build failed, dynamic binary) -> reward 0, valid 1.
Missing or unreadable evidence -> reward 0, valid 0, which marks an infrastructure error.
"""
import json
import math
import os
import sys

W_INFLATE = 0.40
W_DEFLATE = 0.15
W_RATIO = 0.25
W_SPEED = 0.20

GROUP_WEIGHTS = {"std": 0.5, "gzip_hdr": 0.2, "hand": 0.3}
REJECT_SHARE = 0.3
REJECT_GATE = 0.8

RATIO_FULL = {"6": 1.015, "9": 1.015}  # candidate bytes / zlib bytes at which the ladder tops out
RATIO_ZERO = 1.25

SPEED_FULL = 0.60   # candidate throughput / zlib throughput (geometric mean) for full marks
SPEED_ZERO = 0.10


def ladder(x, zero, full):
    """Linear ramp from `zero` (score 0) to `full` (score 1); works in either direction."""
    if zero == full:
        return 1.0 if x == full else 0.0
    t = (x - zero) / (full - zero)
    return max(0.0, min(1.0, t))


def write(outdir, rewards, details):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "reward.json"), "w") as fh:
        json.dump(rewards, fh, indent=2)
    with open(os.path.join(outdir, "details.json"), "w") as fh:
        json.dump(details, fh, indent=2)
    with open(os.path.join(outdir, "reward.txt"), "w") as fh:
        fh.write(str(rewards["reward"]))
    print(f"Reward: {rewards['reward']} (valid={rewards['valid']})")


def zero(outdir, valid, reason):
    write(outdir, {"reward": 0.0, "valid": int(valid), "inflate": 0.0, "deflate_ok": 0.0, "ratio": 0.0, "speed": 0.0},
          {"reason": reason})


def score_inflate(results):
    """results: list of {group, expect, passed}."""
    valid_groups = {}
    rejects = [r for r in results if r["expect"] == "error"]
    for r in results:
        if r["expect"] != "ok":
            continue
        g = valid_groups.setdefault(r["group"], [0, 0])
        g[0] += int(bool(r["passed"]))
        g[1] += 1
    valid = 0.0
    wsum = 0.0
    per_group = {}
    for g, w in GROUP_WEIGHTS.items():
        if g in valid_groups and valid_groups[g][1]:
            frac = valid_groups[g][0] / valid_groups[g][1]
            per_group[g] = round(frac, 6)
            valid += w * frac
            wsum += w
    valid = valid / wsum if wsum else 0.0
    reject = sum(int(bool(r["passed"])) for r in rejects) / len(rejects) if rejects else 0.0
    counted = reject if valid >= REJECT_GATE else 0.0
    total = (1 - REJECT_SHARE) * valid + REJECT_SHARE * counted
    return total, {"valid_streams": round(valid, 6), "per_group": per_group, "rejects": round(reject, 6),
                   "rejects_counted": valid >= REJECT_GATE}


def score_deflate(results):
    """results: list of {sample, level, fmt, passed, size}."""
    if not results:
        return 0.0
    return sum(int(bool(r["passed"])) for r in results) / len(results)


def score_ratio(results, zlib_sizes):
    """Raw-container outputs at levels 6 and 9 on the samples that have a zlib reference size. Each
    sample weighs the same: the mean of candidate_bytes / zlib_bytes per sample goes on the ladder."""
    detail = {}
    scores = []
    for level in ("6", "9"):
        rels = {}
        for r in results:
            if r["fmt"] != "raw" or str(r["level"]) != level or r["sample"] not in zlib_sizes:
                continue
            if not r["passed"]:
                return 0.0, {"reason": "a deflate output was invalid"}
            rels[r["sample"]] = r["size"] / zlib_sizes[r["sample"]][level]
        if not rels:
            continue
        mean_rel = sum(rels.values()) / len(rels)
        s = ladder(mean_rel, RATIO_ZERO, RATIO_FULL[level])
        detail[f"level{level}"] = {"mean_relative": round(mean_rel, 5), "score": round(s, 6),
                                   "per_sample": {k: round(v, 4) for k, v in sorted(rels.items())}}
        scores.append(s)
    return (sum(scores) / len(scores) if scores else 0.0), detail


def score_speed(perf):
    """perf: {inflate: {ok, cand_sec, zlib_sec}, deflate: {...}}."""
    rels = {}
    for k in ("inflate", "deflate"):
        p = perf.get(k) or {}
        if not p.get("ok") or not p.get("cand_sec") or not p.get("zlib_sec"):
            return 0.0, {k: "not measured or incorrect output"}
        rels[k] = p["zlib_sec"] / p["cand_sec"]
    gm = math.sqrt(rels["inflate"] * rels["deflate"])
    s = ladder(gm, SPEED_ZERO, SPEED_FULL)
    return s, {"relative_inflate": round(rels["inflate"], 4), "relative_deflate": round(rels["deflate"], 4),
               "geometric_mean": round(gm, 4), "score": round(s, 6)}


def score(outdir, evidence_path):
    try:
        with open(evidence_path) as fh:
            ev = json.load(fh)
    except Exception as e:  # noqa: BLE001
        zero(outdir, 0, f"evidence unreadable: {e}")
        return
    if ev.get("hard_fail"):
        zero(outdir, 1, f"HARD FAIL: {ev['hard_fail']}")
        return

    inflate, inf_detail = score_inflate(ev.get("inflate", []))
    deflate_ok = score_deflate(ev.get("deflate", []))
    ratio, ratio_detail = score_ratio(ev.get("deflate", []), ev.get("zlib_sizes", {}))
    speed, speed_detail = score_speed(ev.get("perf", {}))

    ratio_eff = ratio if deflate_ok >= 1.0 else 0.0
    speed_eff = speed if (inflate >= 0.99 and deflate_ok >= 1.0) else 0.0
    reward = W_INFLATE * inflate + W_DEFLATE * deflate_ok + W_RATIO * ratio_eff + W_SPEED * speed_eff
    reward = round(min(1.0, max(0.0, reward)), 6)

    rewards = {"reward": reward, "valid": 1, "inflate": round(inflate, 6), "deflate_ok": round(deflate_ok, 6),
               "ratio": round(ratio_eff, 6), "speed": round(speed_eff, 6)}
    details = {"inflate": inf_detail, "ratio": ratio_detail, "speed": speed_detail,
               "ratio_counted": deflate_ok >= 1.0, "speed_counted": speed_eff == speed and speed > 0,
               "timing": ev.get("timing", {}),
               "reason": f"inflate={inflate:.4f} deflate_ok={deflate_ok:.4f} ratio={ratio_eff:.4f} speed={speed_eff:.4f} -> {reward}"}
    write(outdir, rewards, details)


if __name__ == "__main__":
    score(sys.argv[1] if len(sys.argv) > 1 else "/logs/verifier",
          sys.argv[2] if len(sys.argv) > 2 else "/logs/verifier/evidence.json")
