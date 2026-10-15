# How to verify a call

Everything below runs offline against this repo. No trust in the desk required.

## 1. Check a reveal against its original commitment

Each file in `reveals/` contains `sealed_record`, `salt` and `commitment_sha256`.
Recompute the hash:

```bash
python3 - <<'EOF'
import json, hashlib, sys, pathlib
for p in sorted(pathlib.Path("reveals").glob("*.json")):
    r = json.loads(p.read_text())
    canon = json.dumps(r["sealed_record"], sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False)
    got = hashlib.sha256((canon + "|" + r["salt"]).encode()).hexdigest()
    ok = got == r["commitment_sha256"]
    print(("OK  " if ok else "FAIL"), p.name, got[:16])
    if not ok:
        sys.exit(1)
EOF
```

`FAIL` means the published record is not the record that was committed. That is the
whole point of the exercise.

## 2. Check *when* the commitment was made — the one that actually matters

The hash alone proves the record is unchanged. It does **not** prove the record was
written before the outcome was known. Only git timestamps answer that:

```bash
git log --oneline -S"<commitment_sha256>" -- commitments.json
```

The **earliest** commit returned is when the desk first published that hash. Compare
it against `outcome.exit_date`:

| Earliest commit vs exit date | What the call is worth |
|---|---|
| before the exit | **evidence** — the desk was locked in before the result existed |
| after the exit | **memoir** — self-reported history, proves nothing about timing |

Every reveal states which it is, in `commitment_grade`: `pre_committed` or
`retroactive_seed`. Do not take that field on faith either — `oracle/replay.py`
re-derives it from git and **fails** if a call claims `pre_committed` without history
to back it (check C3). Downgrading yourself is allowed; claiming unearned proof is not.

As of the first publish, **all 6 closed calls are `retroactive_seed`** — this repo
did not exist when they were made, so none of them are proven. The 26 open
commitments are the first batch that can ever earn `pre_committed`.

Commit timestamps are written by GitHub, not by the desk.

## 2b. Run the whole scorer yourself

```bash
python3 oracle/replay.py          # human-readable, exits non-zero on any failure
python3 oracle/replay.py --json   # machine-readable
```

Stdlib only, no network — it can only read what is in the tree. The same script runs
on GitHub's runners (`.github/workflows/verify.yml`) on every push and daily, so a
quietly tampered history goes red on hardware the desk does not control.

## 3. Check that nothing was quietly dropped

A hash that exists in an older `commitments.json` but is absent from both the current
`commitments.json` and `reveals/` is a deleted call:

```bash
git log -p --follow commitments.json | grep commitment_sha256 | \
  sed 's/.*: "//;s/".*//' | sort -u > /tmp/all_hashes
cat commitments.json reveals/*.json | grep -o '[0-9a-f]\{64\}' | sort -u > /tmp/live
comm -23 /tmp/all_hashes /tmp/live
```

Empty output = no call has ever been removed. Non-empty output = ask what the missing
hash was.

## 4. Check the aggregate matches the reveals

`performance.json` is derived, not asserted. Recompute it from `reveals/`:

```bash
python3 - <<'EOF'
import json, pathlib
rets = [json.loads(p.read_text())["outcome"]["realized_return_pct"]
        for p in pathlib.Path("reveals").glob("*.json")]
rets = [r for r in rets if r is not None]
print("n", len(rets), "wins", sum(r > 0 for r in rets),
      "avg", round(sum(rets)/len(rets), 2))
EOF
```

Compare against `n_closed`, `wins`, `avg_return_pct`.

## What this does NOT prove

- **Not** that the calls were traded with real size. It proves what was claimed and when.
- **Not** that open calls are good. An open commitment proves only that *something*
  was committed on that date — the asset is deliberately hidden until close.
- **Not** that the closed set is representative. See `caveats[]` in `performance.json`:
  hand-timed exits inflate the win rate, and the sample is small.
- **Not** — for any call graded `retroactive_seed` — anything at all beyond "this is
  what the desk says happened". Read those as a claim, not a proof.
