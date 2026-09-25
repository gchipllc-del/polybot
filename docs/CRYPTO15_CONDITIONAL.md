# Crypto-15 — the Conditional Test (Decision Record, 2026-09-22)

## Why this exists

Weather taught the lesson (docs/WEATHER_CONDITIONAL.md): a screener that asks the
unconditional question — "at price P, does this settle YES more often than P?" — is
blind, *by construction*, to any edge that only appears when an independent estimate
disagrees with the price. The crypto venue has now been screened unconditionally twice
(edge_analysis on 12,821 deep-history windows, and the live stage0 join) and both reads
were honest nulls-with-threads: 0 HELD, three thin persisters, one live late-longshot
thread (H1b/H5, `<2min 05-10c` significant +EV in the observation table).

Before retiring anything or promoting anything, the same correction applies here: ask
the conditional question. Crypto has exactly one independent estimate this project
trusts enough to test — the production volatility model (`lib/fair_value` →
`binary_justify.fair_p_above`: EWMA sigma from the collector's own spot samples,
zero-drift Student-t nu=4). It has ranked strikes for the paper trader since
2026-08-11, and every paper entry since carries its `fv_edge` stamp.

`scripts/vol_replay.py` asks the question two ways, with two evidence grades:

- **`replay`** — every settled market in the stage0 log re-priced from spot + trailing
  vol *as of that minute*, bet only where model and book disagree beyond a threshold,
  settled on Kalshi's own result. Honest but reconstructed.
- **`ledger`** — the forward paper book bucketed by the `fv_edge` stamped at entry,
  before settlement. Nothing reconstructed; the strongest evidence we own. If $/window
  rises with the stamped edge, the model sees real mispricing, forward.

## The guards (each one killed a defect during construction)

1. **No lookahead.** Sigma at an entry uses spot observations at or before the entry
   timestamp; the tests recompute an entry's sigma in a world with no future at all
   and require bit-identical results. Returns spanning collector outages are dropped —
   a return across a 2-hour gap is not a 1-minute return.
2. **The model must beat the price — per band.** Brier of model vs the book's own mid,
   out of sample, on at least 100 entries, by at least 1% relative. Per band, because
   the fixture proved the global average drowns a concentrated edge: a planted 8-cent
   late-band mispricing moved the global gate by 0.0003 while the model's own sigma
   noise cost 0.0007. And with a margin, because a clean band once "won" its gate on
   0.3% of cent-rounding luck. The gate is a precondition; the corrected cell interval
   is the verdict.
3. **Walk-forward, per window, search-corrected.** The one calibration constant (sigma
   scale k) is measured on the first half by date and frozen. A window is one 15-min
   event — all its strikes resolve on the same move, ONE bet. 4 bands × 4 thresholds,
   frozen; out-of-sample intervals Bonferroni-corrected over cells searched; 30
   windows minimum per side before a cell may be called anything.

## Fixture-measured discrimination

"Beatable" plants an 8c late-band mid displacement (late longshots cheap ⇔ late
favorites rich — the live H1b/H5 story); "efficient" quotes the settlement-true
probability plus spread.

| 15-min windows | beatable world | efficient world |
|---|---|---|
| ~100 | gates open, 0 HELD | 0 HELD |
| ~200 | 1 HELD, planted bands only | 0 HELD |
| ~400 | 1 HELD, planted bands only | 0 HELD |
| ~600 | 3 HELD, planted bands only | 0 HELD |

At small n a band gate occasionally opens on luck; no HELD ever followed on clean
data. Caveat: the plant is large — a 1-2c edge needs far more windows than this table
suggests. The stage0 file already holds ~4,000 windows, growing ~100/day.

Three fixture bugs were themselves caught and are pinned as tests: a plant smaller
than friction (rightly invisible — the first "failure" was the fixture's), an
"efficient" book accidentally quoted from the model's own t4 shape instead of the
gaussian settlement truth (which made it genuinely beatable), and synthetic windows
packed so tight the date-based split came out 190/50.

## Adversarial review before first use

Five independent review passes (lookahead, statistics, microstructure, data
contracts, code) produced 19 findings; each was then attacked by two refuters; 7
survived and all 7 are fixed and pinned as tests:

- an interpolation-table off-by-one that could crash at the z-boundary;
- the late-longshot slices picked each window's **cheapest** ask in hindsight — a
  price no forward rule can have; now first-touch, the house convention;
- the slices split on their own median dates instead of the global walk-forward cut;
- the outage-gap tests were vacuous (passed with the guard deleted);
- the docstring promised a future-perturbation selftest that didn't exist — it does
  now, at both selftest and pytest level;
- the ledger's headline fv-bin table can look monotone off one lucky favorite streak,
  because opposite-side rules put the same window in different bins — the output now
  says so and points at the within-rule table as the real read;
- **the depth slices were reading the wrong side of the order book.** A taker buying
  YES fills against resting NO bids; `shadow_book._depth_at` counts same-side bids —
  your competition — as liquidity. vol_replay now has a correct `liftable_depth`.

**Open production flag (not fixed here, needs a decision):** that last defect lives
in `shadow_book._depth_at`, which the LIVE paper trader uses for its `no_depth`
refusals and its ledger `depth` stamps. **Verified against a live book row on
2026-09-25:** the API serves `{"orderbook_fp": {"yes_dollars": [...],
"no_dollars": [...]}}` — a wrapper key and dollar-string levels `_depth_at` does not
recognize, so it returns None and the paper trader applies NO gate at all. The
production depth check is a silent no-op on current payloads (and was wrong-sided on
any older payload it did parse). The first run of this tool's depth slices returned
the same degenerate answer (every book "empty"), which is what exposed it;
`liftable_depth` now parses the real payload and is pinned to that captured row in
tests. Fixing `_depth_at` changes live paper behavior mid-sample, so it is flagged
rather than silently patched; the fix should land with its own dated note, like the
2026-08-07 envload bugfix did.

## Running it (on the host, where the data lives)

```powershell
cd C:\Users\dxncr\polybot
py scripts\vol_replay.py replay      # the conditional replay over stage0 history
py scripts\vol_replay.py ledger      # the forward book x fv_edge stamps
```

No env vars needed — defaults point at `data\stage0_crypto.jsonl` and
`data\paper_crypto15.jsonl`. No network. `selftest` runs the fixtures anywhere.

## How to read it

- **All band gates closed** → the model knows nothing the book doesn't, anywhere. The
  late-longshot thread then rests entirely on H5's forward windows reaching 100+, and
  the H1/H2 retirement decision proceeds on the ledger evidence alone.
- **A late-band gate opens but nothing HELD** → the model sees the mispricing but
  friction eats it at taker prices. The pre-identified next step is a maker-side
  variant, not a threshold hunt.
- **A cell HELD, or the `ledger` fv-bins are cleanly monotone** → the model earns a
  role: a pre-registered rule (`named_at`-stamped, e.g. "H5 entry AND stamped
  fv_edge ≥ X") tested forward on the paper book. Nominate, never promote — no
  replay result sizes anything up or touches real money.

The slice tables (late-longshot by model view / book depth / series) are labelled
hypothesis generators and Bonferroni-corrected as a family, but anything found there
still re-earns itself forward from its naming date. That rule has no exceptions; it is
the reason this project's nulls are trustworthy.
