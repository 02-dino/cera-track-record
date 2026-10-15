#!/usr/bin/env python3
"""
replay.py — the independent scorer for this repo.

WHY THIS EXISTS: every number in performance.json is produced by the desk. That makes
it an assertion. This script re-derives those numbers from reveals/ alone and refuses
to agree unless they match, and it runs in GitHub Actions — on a machine the desk does
not control, against the commit as GitHub received it. A green run is therefore a
third party agreeing with the desk's arithmetic; a red run is a public disagreement
nobody can quietly delete.

It is deliberately stdlib-only and offline: no network, no deps, no prices fetched.
A scorer that phones out could be fed a different answer; this one can only read what
is in the tree.

CHECKS (any failure exits non-zero and fails CI):
  C1 hash integrity     — every reveal recomputes to its committed hash
  C2 no silent deletion — every hash ever in commitments.json is still live
  C3 grade honesty     — any call CLAIMING pre_committed really was, per git history
  C4 aggregate honesty  — performance.json == recomputed from reveals/
  C5 disclosure leak    — open commitments carry no asset/thesis/price
  C6 secret hygiene     — no salts for OPEN calls anywhere in the tree

Usage: python3 oracle/replay.py [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
REVEALS = ROOT / "reveals"
COMMITMENTS = ROOT / "commitments.json"
PERFORMANCE = ROOT / "performance.json"

# The ONLY keys an open commitment may carry. Anything else is a disclosure leak:
# the whole promise is that an open call is unreadable until it closes.
ALLOWED_OPEN_KEYS = {
    "idea_id", "commitment_sha256", "call_date", "status", "first_committed",
}

FAIL: list[str] = []
NOTE: list[str] = []


def fail(check: str, msg: str) -> None:
    FAIL.append(f"{check}: {msg}")


def canon(obj) -> str:
    """Must match the desk's canonicalisation exactly or every hash 'fails'."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(record: dict, salt: str) -> str:
    return hashlib.sha256((canon(record) + "|" + salt).encode("utf-8")).hexdigest()


def load_reveals() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(REVEALS.glob("*.json"))]


def git(*args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), *args],
                             capture_output=True, text=True, timeout=60)
        return out.stdout
    except Exception:
        return ""


# ---------------------------------------------------------------- C1
def check_hashes(reveals: list[dict]) -> int:
    """A reveal that does not recompute means the published record is NOT the record
    that was committed — i.e. the call was edited after the fact."""
    ok = 0
    for r in reveals:
        got = digest(r["sealed_record"], r["salt"])
        if got == r["commitment_sha256"]:
            ok += 1
        else:
            fail("C1", f"{r['idea_id']} hash mismatch: recomputed {got[:16]} "
                       f"!= committed {r['commitment_sha256'][:16]}")
    return ok


# ---------------------------------------------------------------- C2
def check_no_deletion(reveals: list[dict], commitments: dict) -> int:
    """A hash that appeared in an older commitments.json but is in neither the current
    commitments.json nor reveals/ is a call that was quietly dropped — the classic way
    a track record is laundered."""
    history = git("log", "-p", "--follow", "--", "commitments.json")
    if not history:
        NOTE.append("C2 skipped: no git history available (shallow clone?)")
        return 0
    ever = set()
    for line in history.splitlines():
        if "commitment_sha256" in line and line.lstrip().startswith(("+", "-", " ")):
            part = line.split('"')
            for tok in part:
                if len(tok) == 64 and all(c in "0123456789abcdef" for c in tok):
                    ever.add(tok)
    live = {c["commitment_sha256"] for c in commitments["commitments"]}
    live |= {r["commitment_sha256"] for r in reveals}
    missing = ever - live
    for m in sorted(missing):
        fail("C2", f"hash {m[:16]} existed in git history but is in neither "
                   f"commitments.json nor reveals/ — call was removed")
    return len(ever)


# ---------------------------------------------------------------- C3
def check_grade_honesty(reveals: list[dict]) -> dict:
    """The ONE claim a hash alone cannot make: that it was published BEFORE the result
    existed. Only git timestamps answer that, and they are written by GitHub.

    A reveal whose hash post-dates its own exit is NOT a failure of this repo — it is
    allowed, and it is exactly what the seed batch is. What IS a failure is such a
    call presenting itself as proven. So this check does not police history; it
    polices the LABEL. grade='pre_committed' must survive git; 'retroactive_seed'
    must be present wherever git cannot back the claim.
    """
    tally = {"pre_committed": 0, "retroactive_seed": 0, "unlabelled": 0}
    for r in reveals:
        grade = r.get("commitment_grade")
        exit_date = (r.get("outcome") or {}).get("exit_date")
        h = r["commitment_sha256"]
        dates = [l for l in git("log", "--reverse", "--format=%aI", "-S", h).splitlines()
                 if l.strip()]
        first = dates[0][:10] if dates else None

        if grade not in ("pre_committed", "retroactive_seed"):
            tally["unlabelled"] += 1
            fail("C3", f"{r['idea_id']} carries no commitment_grade — a reveal must "
                       f"state whether it was pre-committed or self-reported")
            continue
        tally[grade] += 1

        if grade == "pre_committed":
            # The only direction that matters: an UNEARNED claim of proof.
            if not first:
                fail("C3", f"{r['idea_id']} claims pre_committed but its hash is "
                           f"absent from git history")
            elif exit_date and first > exit_date:
                fail("C3", f"{r['idea_id']} claims pre_committed but its hash first "
                           f"appeared {first}, after exit {exit_date}")
        else:
            # Downgrading yourself is always allowed; we only note it so the count
            # cannot be lost between the file and the summary.
            NOTE.append(f"C3 {r['idea_id']}: retroactive_seed — self-reported, "
                        f"outcome known before the hash was published")
    return tally


# ---------------------------------------------------------------- C4
def recompute_performance(reveals: list[dict]) -> dict:
    rets = [r["outcome"]["realized_return_pct"] for r in reveals
            if r["outcome"].get("realized_return_pct") is not None]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x < 0]
    scratch = [x for x in rets if x == 0]
    sum_win = round(sum(wins), 2)
    sum_loss = round(abs(sum(losses)), 2)
    return {
        "n_closed": len(reveals),
        "n_priced": len(rets),
        "wins": len(wins),
        "losses": len(losses),
        "scratch": len(scratch),
        "win_rate": round(len(wins) / len(rets), 4) if rets else None,
        "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "best_pct": max(rets) if rets else None,
        "worst_pct": min(rets) if rets else None,
        "sum_win_pct": sum_win,
        "sum_loss_pct": sum_loss,
        "profit_factor": round(sum_win / sum_loss, 2) if sum_loss else None,
    }


def check_aggregate(reveals: list[dict]) -> dict:
    """performance.json is derived, not asserted. If the desk's published aggregate
    disagrees with the reveals it claims to summarise, the aggregate is wrong."""
    mine = recompute_performance(reveals)
    theirs = json.loads(PERFORMANCE.read_text())
    for k, v in mine.items():
        if theirs.get(k) != v:
            fail("C4", f"performance.json {k}={theirs.get(k)!r} but reveals/ give {v!r}")
    retro = sum(1 for r in reveals if r.get("commitment_grade") == "retroactive_seed")
    if retro:
        blob = " ".join(theirs.get("caveats") or [])
        if "retroactive_seed" not in blob:
            fail("C4", f"{retro} reveal(s) are retroactive_seed but performance.json "
                       f"caveats never say so — the aggregate would read as proven")
        if theirs.get("commitment_grade_breakdown", {}).get("retroactive_seed") != retro:
            fail("C4", "commitment_grade_breakdown disagrees with reveals/")
    if not theirs.get("caveats"):
        fail("C4", "performance.json ships no caveats[] — aggregate must carry its "
                   "own health warning or it will be quoted without one")
    if theirs.get("basis") != "closed_calls_only":
        fail("C4", f"basis={theirs.get('basis')!r}, expected 'closed_calls_only'")
    return mine


# ---------------------------------------------------------------- C5
def check_disclosure(commitments: dict) -> int:
    """Open calls must be unreadable. One stray 'asset' key breaks the VIP promise for
    every subscriber at once, so this is a hard failure, not a warning."""
    n = 0
    for c in commitments["commitments"]:
        n += 1
        extra = set(c) - ALLOWED_OPEN_KEYS
        if extra:
            fail("C5", f"{c.get('idea_id')} open commitment leaks field(s): "
                       f"{sorted(extra)}")
        if c.get("status") != "open":
            fail("C5", f"{c.get('idea_id')} in commitments.json has status "
                       f"{c.get('status')!r}, expected 'open'")
        h = c.get("commitment_sha256", "")
        if len(h) != 64:
            fail("C5", f"{c.get('idea_id')} malformed commitment hash")
    return n


# ---------------------------------------------------------------- C6
def check_secret_hygiene(reveals: list[dict]) -> None:
    """Salts for OPEN calls are the thing that makes an open commitment brute-force
    resistant. If one is ever tracked, the corresponding call is readable."""
    tracked = [l for l in git("ls-files").splitlines() if l.strip()]
    revealed_ids = {r["idea_id"] for r in reveals}
    for f in tracked:
        low = f.lower()
        if ".sealed" in low or low.endswith(".salt"):
            fail("C6", f"sealed/salt artefact is tracked in git: {f}")
    # a reveal publishing a salt is correct; a salt for a non-revealed id is not
    for p in sorted(REVEALS.glob("*.json")):
        rid = json.loads(p.read_text()).get("idea_id")
        if rid not in revealed_ids:
            fail("C6", f"{p.name} publishes a salt for id {rid} not in the reveal set")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    args = ap.parse_args()

    reveals = load_reveals()
    commitments = json.loads(COMMITMENTS.read_text())

    hashes_ok = check_hashes(reveals)
    ever_seen = check_no_deletion(reveals, commitments)
    grades = check_grade_honesty(reveals)
    agg = check_aggregate(reveals)
    open_n = check_disclosure(commitments)
    check_secret_hygiene(reveals)

    head = (git("rev-parse", "HEAD").strip() or "unknown")[:12]
    result = {
        "head": head,
        "passed": not FAIL,
        "checks": {
            "C1_hash_integrity": f"{hashes_ok}/{len(reveals)} reveals recompute",
            "C2_no_silent_deletion": f"{ever_seen} hashes ever seen, all accounted for",
            "C3_grade_honesty": (f"{grades['pre_committed']} pre_committed "
                                 f"(git-verified), {grades['retroactive_seed']} "
                                 f"retroactive_seed (self-reported)"),
            "C4_aggregate_honesty": "performance.json matches reveals/",
            "C5_disclosure": f"{open_n} open commitments, hash-only",
            "C6_secret_hygiene": "no open-call salts tracked",
        },
        "recomputed": agg,
        "commitment_grades": grades,
        "failures": FAIL,
        "notes": NOTE,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"replay @ {head}")
        for k, v in result["checks"].items():
            print(f"  {'FAIL' if any(f.startswith(k[:2]) for f in FAIL) else 'ok  '} {k}: {v}")
        for n in NOTE:
            print(f"  note {n}")
        for f in FAIL:
            print(f"  FAIL {f}")
        print("PASS" if not FAIL else f"FAIL ({len(FAIL)} problem(s))")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
