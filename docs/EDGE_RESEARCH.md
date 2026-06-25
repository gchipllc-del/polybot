<!-- deep-research run wf_41a03db8 · 26 sources, 22 claims confirmed / 3 refuted via 3-vote adversarial verification · 2026-06-25 -->

# Where a Single Non-HFT Operator Can Realistically Capture a Persistent Trading/Betting Edge — And Which Public "Edge" Claims Are Mirages

## Executive Summary

The evidence-first answer is sobering but actionable: across prediction markets, sports betting, crypto, and equities, almost every *forecasting/informational* edge accessible to a single retail operator is already priced in or, where a real anomaly is measurable, **structurally uncapturable because transaction costs exceed the gross edge**. The strongest documented, replicated phenomena (Moskowitz's sports-betting momentum, the Kalshi favorite-longshot bias, crypto funding-rate spreads, cross-venue prediction-market price deviations) are all *real signals that persist precisely because they cannot be cheaply arbitraged* — meaning the operator's job is not to "discover" them but to find the narrow conditions where the net-of-cost residual is positive. The three lanes with the best risk-adjusted structural logic for this profile are: **(1) Kalshi maker-side market-making positioned to harvest the favorite-side residual and avoid the taker tax; (2) crypto funding-rate carry on less-mature DEX venues, sized small and treated as a low-drawdown carry trade rather than free arb; and (3) cross-venue prediction-market deviations, but only as a manual, semantics-checked opportunity hunt, not an automated risk-free arb.** The dominant failure mode in public GitHub repos and blog claims is the conflation of a *detected* gross spread with a *capturable* net edge — most named tools (e.g., the popular `aoki-h-jp/funding-rate-arbitrage` scanner) are honest detectors that explicitly do not execute, while the implicit "edge" is destroyed by fees, slippage, spread reversal, and resolution-rule mismatch. The academic backtest-overfitting and factor-zoo literature provides the adversarial frame: assume any backtested edge is overfit until proven out-of-sample with a t-stat well above 3.0.

---

## Ranked Findings

### Finding 1 — Kalshi favorite-longshot bias + the maker/taker structure is the single best-documented retail-accessible edge, but it is small and maker-side only
**Confidence: HIGH** (multiple unanimous 3-0 claims from a primary transaction-level academic study)

Bürgi, Deng & Whelan (2025), *"Makers and Takers: The Economics of the Kalshi Prediction Market"* — using transaction-level data on 300,000+ contracts — establish three load-bearing facts:

- A **clear favorite-longshot bias**: contracts priced under ~10c lose over 60% of money invested, while contracts priced above 50c show *statistically significant small positive returns* ([karlwhelan.com/Papers/Kalshi.pdf](https://www.karlwhelan.com/Papers/Kalshi.pdf); SSRN 5502658; MPRA 126350; UCD WP2025_19). [claim 0]
- **Takers lose ~32% on average; makers lose ~10%** — neither side is profitable in aggregate, but the taker tax is ~3x the maker loss. [claim 1]
- **Makers are the relatively well-informed side** who post offers seeking positive expected return (slightly over-optimistic), and the longshot bias is *much* stronger for takers. [claim 2]

**Mechanism / why it persists:** Retail takers cross the spread on longshots (lottery-ticket demand), structurally overpaying. The other side is the maker who posts the offer. **Who is on the other side:** unsophisticated retail flow seeking cheap binary upside. **Why it isn't arbed away:** the residual after Kalshi's fees is small, the favorite-side positive return is bounded, and capturing it requires *posting* liquidity (maker), patience, and inventory risk — not a one-click trade.

**Realistic edge after fees/slippage:** Small. The aggregate market is ~-20% pre-fee, makers ~-10%; the exploitable residual is the *favorite-side, maker-positioned* sliver, not a large free edge. This is a "shade your way from -10% maker baseline to slightly positive by avoiding longshots and being a disciplined maker on favorites" edge, not a money pump.

**Capital/latency:** Low thousands; seconds-to-minutes polling is adequate for maker quoting on slower Kalshi markets (note: same-venue YES+NO mispricings have a half-life "well under a minute" and are bot-dominated — those are NOT for this operator). **Risks/decay:** adverse selection (you get filled when informed flow knows something), fee drag, and the bias narrowing as the venue matures.

**How to capture:** Be a maker, not a taker. Quote the favorite side of binaries where retail longshot demand is depressing the favorite's price, avoid sub-10c longshots entirely, and treat the ~22% maker-vs-taker structural gap as the thing you are harvesting.

---

### Finding 2 — Cross-venue prediction-market price deviations are real and persistent, but "semantic non-fungibility" makes them capital-intensive and often un-arbitrageable
**Confidence: HIGH** (primary arXiv preprint, mostly unanimous; one 2-1 on magnitude)

Gebele & Matthes (Jan 2026), *"Semantic Non-Fungibility and Violations of the Law of One Price in Prediction Markets"* (arXiv:2601.01706, TU Munich):

- A human-validated dataset of **100,000+ events across 10 venues (2018-2025); ~6% are concurrently cross-listed**, defining the addressable cross-venue arbitrage universe ([arxiv.org/pdf/2601.01706](https://arxiv.org/pdf/2601.01706)). [claim 3]
- Semantically equivalent markets show **persistent, execution-aware price deviations of 2-4% on average**, even in liquid settings — a *structural*, not forecasting, mispricing. [claim 4, medium confidence on magnitude]
- It persists due to **"semantic non-fungibility"**: economically identical claims lack a shared machine-verifiable event identity, so liquidity fails to pool, and arbitrage becomes "capital-intensive or unenforceable," systematically violating the Law of One Price. [claim 5]

**Mechanism / why it persists:** Two venues (e.g., Kalshi vs Polymarket) list the "same" event but with *different resolution rules* (media-call vs inauguration; the paper's Cardi B case where the two venues can resolve the SAME event oppositely). The 2-4% is net of fees and typical spread but **NOT** net of order-book depth, gas, or resolution-mismatch risk. **Who is on the other side:** fragmented liquidity pools that never merge. **Why it isn't arbed:** the "risk-free" spread can become a total loss if the two venues settle differently.

**Realistic net edge:** Much less than 2-4% after the operator accounts for fillable size, Polygon gas, and — critically — manually verifying the two resolution criteria are truly identical. Corroborating evidence (arXiv 2603.03136; Sherwood) found 2024-election prices diverged >5pp and on 62/65 days Harris+Trump didn't sum to $1.

**Capital/latency/capture:** Tens of thousands to make the per-leg economics work; seconds-to-minutes is fine because these deviations are persistent, not microstructural. **This is a manual opportunity-hunt, not an automated bot:** scan cross-listed pairs, hand-verify resolution-rule identity, and only take pairs where settlement risk is genuinely zero. Decay risk: rising as semantic-alignment tooling improves.

---

### Finding 3 — Crypto funding-rate carry is a real low-drawdown carry trade concentrated on immature DEX venues, NOT a high-Sharpe free arbitrage
**Confidence: HIGH** on direction and net-of-cost reality; **MEDIUM** on magnitude/maturity causation

Two primary peer-reviewed sources converge:

- **MDPI Mathematics (Jan 2026), "Two-Tiered Structure of Cryptocurrency Funding Rate Markets"** (35.7M one-minute observations, 26 exchanges): **17% of observations show spreads ≥20bps, but only 40% of the *top* opportunities are net-positive after transaction costs and spread reversals** ([mdpi.com/2227-7390/14/2/346](https://www.mdpi.com/2227-7390/14/2/346)). The majority of large nominal spreads are unprofitable to capture. [claim 6]
- **ScienceDirect (Aug 2025), "Exploring Risk and Return Profiles of Funding Rate Arbitrage on CEX and DEX"**: over a 6-month window, funding-arb returned **7.61% (Drift), 5.59% (ApolloX), 2.17% (Binance), 1.98% (Bitmex)** — far below HODL's 113.62% — but with **dramatically lower drawdowns (0.10-0.14% vs 25.92%)** ([sciencedirect.com/.../S2096720925000818](https://www.sciencedirect.com/science/article/pii/S2096720925000818)). [claim 7]
- The DEX edge is **structurally tied to market immaturity**: returns on mature CEX markets fall below even the DeFi risk-free rate, so the edge decays as venues mature and is concentrated in newer, less-efficient decentralized platforms. [claim 8, medium — 2-1 vote]

**Important refutations (for honesty):** The headline "Sharpe 23.55 on Drift" claim was **REFUTED 0-3** (implausibly high, almost certainly understates spot-leg slippage/costs and reflects token-incentive/liquidity-mining artifacts, not pure structural maturity). The "forced exit before convergence in 95% of opportunities" framing was also contested (1-2). So treat DEX funding yields as a **carry trade inflated partly by emissions/incentives**, not a clean structural arb.

**Mechanism / capture:** Hold a delta-neutral spot-vs-perp position to collect funding; the edge is real but *modest and decaying*, concentrated where venues are immature and where token incentives subsidize funding. **Who's on the other side:** leveraged directional perp longs paying funding. **Net edge:** low single-digit-percent annualized carry with very low drawdown — a defensible Sharpe enhancer, NOT a HODL-beating money machine. **Capital/latency:** thousands; seconds-to-minutes adequate. **Risks:** spread reversal before convergence, incentive-program expiry, DEX smart-contract/depeg risk, and decay as arbitrage capital arrives.

---

### Finding 4 — Sports-betting momentum/value is statistically real across 30 years and 4 sports, but is the canonical example of a documented signal that is NOT a tradeable edge
**Confidence: HIGH** (Moskowitz 2021, *Journal of Finance*, verified verbatim from primary PDF, all 3-0)

Moskowitz, *"Asset Pricing and Sports Betting,"* J. Finance 76(6):3153-3209:

- Across **100,000+ liquid contracts over three decades in four pro sports**, there is **strong momentum and weak value predictability** — but **magnitudes are a fraction of equity-market anomalies** ([primary PDF](https://spinup-000d1a-wp-offload-media.s3.amazonaws.com/faculty/wp-content/uploads/sites/3/2021/08/AssetPricingandSportsBetting_JF.pdf)). [claim 19]
- **Once vig is included, every strategy loses substantial money**: best multifactor open-to-close = **-32.12%/yr net**; close-to-end momentum = **-11.98%/yr net** (Table VIII: all net returns negative; gross returns positive). [claim 20]
- The predictability **persists precisely BECAUSE transaction costs are too high to arbitrage it away** — the edge exists but is structurally uncapturable. [claim 21]

**Implication for the operator:** This is the cleanest illustration of the report's core thesis. A real, replicated, three-decade signal (independently corroborated by Vizard, SSRN 4542265, on Betfair) is **un-tradeable at standard vig**. The only scope refinement: the paper aggregates high-vig markets; a single operator on the *lowest-vig venue (Pinnacle/Betfair)* faces lower costs, so the universal "uncapturable" verdict is marginally stronger than the best-case operator scenario — but it does not flip to profitable.

**Related structural result (Hegarty & Whelan, Oxford Economic Papers 2026, 78(1):90):** Favorite-longshot bias is a function of **market structure × bettor disagreement**, not risk-love or probability misperception — longshot demand is less odds-elastic, so *monopolistic/soft books* shade longshots while *competitive books (Pinnacle/Betfair) are near-efficient* ([UCD WP23_12](https://www.ucd.ie/economics/t4media/WP23_12.pdf)). [claims 11, 12 — both 2-1, medium]. **Practical takeaway:** use Pinnacle/Betfair as the efficient reference price and look for the bias on *less-competitive US retail books* — but this is a lead, not a turnkey edge, and the +EV residual must still clear vig.

> Note: The claim that **NFL home underdogs were profitably ATS-beatable (53.5% over 2002-2011) was REFUTED 0-3** — a textbook overfit/period-specific artifact. Do not build on it.

---

### Finding 5 — The adversarial frame: assume any backtested edge is overfit; the bar for belief is a t-stat well above 3.0
**Confidence: HIGH** (Bailey/López de Prado and Harvey/Liu/Zhu, top-tier peer-reviewed, all 3-0)

This is the meta-finding that governs evaluation of every claim above and every GitHub repo below.

- **Hold-out is unreliable for investment backtests.** Bailey, Borwein, López de Prado & Zhu introduce the **Probability of Backtest Overfitting (PBO)** via Combinatorially Symmetric Cross-Validation ([SSRN 2326253](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)). [claim 13]
- **High in-sample performance is trivially achievable** after only a few configurations, and **P(overfit) rises monotonically with configurations tried**. [claim 14]
- **Under memory effects, overfitting yields NEGATIVE (not zero) out-of-sample returns** — a proposed explanation for why so many quant funds fail. [claim 15]
- **The factor zoo:** Harvey, Liu & Zhu (*Review of Financial Studies* 2016) catalog **316 published factors**; "most claimed research findings in financial economics are likely false" ([SSRN 2249314](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2249314)). [claims 16, 18]
- Therefore the conventional **t > 2.0 threshold is far too lenient; a credible new factor needs roughly t > 3.0** after multiple-testing correction. [claim 17]

**Operator rule:** Any strategy you backtest should be assumed overfit unless it survives out-of-sample/live testing at t > 3.0. This is consistent with the prior multi-week effort's finding that crypto/weather-fade strategies came back PSR < 0.5.

---

## Critical Survey of Public Repos / Open Projects

Only one repo reached 3-vote verification, but the recurring failure modes are well-evidenced.

| Project | Verdict | Evidence |
|---|---|---|
| **`aoki-h-jp/funding-rate-arbitrage`** (GitHub) | **Honest detector — NOT an edge claim.** Plausible-but-unproven as a profit tool. | README states verbatim: *"This library does not include the feature to perform automatic funding rate arbitrage."* It is **detection/analysis only** — scans funding divergences within and across Binance, Bybit, OKX, Gate.io, CoinEx, Bitget; **no order-placement code exists** (GitHub code search for `create_order`/`place_order` returned 0 results). [claims 9, 10, both 3-0] The repo makes **no profitability claim**; the implicit edge is exactly the gross spread that the MDPI paper shows is net-negative ~60% of the time even for top opportunities. |

**The recurring failure modes across public "edge" claims (flag these explicitly):**

1. **Detected spread ≠ capturable net edge.** The single most common error. A scanner shows a 20bps+ funding spread or a 2-4% cross-venue deviation; the repo/blog implies it's free money. Fees, slippage, gas, spread reversal, and fillable depth erase most of it (MDPI: only 40% of *top* opportunities net-positive).
2. **Resolution/settlement-rule mismatch sold as "risk-free arb."** Cross-venue prediction-market spreads (the Cardi B / 2024-election cases) can settle oppositely — converting "arb" into total loss.
3. **Ignored vig.** Sports-betting strategies showing positive *gross* returns that are -12% to -32%/yr *net* (Moskowitz Table VIII).
4. **Overfitting / period-specific artifacts.** E.g., the refuted "NFL home underdogs 53.5%" claim — a single 2002-2011 window, no out-of-sample survival.
5. **Incentive artifacts mistaken for structural edge.** DEX funding "Sharpe 23.55" (refuted) reflects token emissions/liquidity mining, not a durable arbitrage.
6. **Survivorship + marketing.** Bots/blogs publish in-sample curves; the overfitting literature shows these are trivially manufactured and often negative out-of-sample.

---

## The Honest Meta-Answer: Which Lanes to Build, Which Are Mirages

**Worth building (2-3 lanes) for this operator profile:**

1. **Kalshi maker-side market-making harvesting the favorite-longshot/maker-taker structure** (Finding 1). Best-documented, retail-accessible, latency-tolerant. The edge is the ~22% maker-vs-taker structural gap plus the favorite-side positive residual. Small but real; requires discipline (post liquidity, avoid longshots, manage adverse selection).
2. **Crypto funding-rate carry on immature/incentivized DEX venues** (Finding 3), sized small and treated as a **low-drawdown carry trade**, not arb. Defensible Sharpe contributor; expect low single-digit annualized net carry; monitor incentive-program expiry and decay.
3. **Cross-venue prediction-market deviations as a manual, resolution-verified opportunity hunt** (Finding 2) — only when the two venues' settlement criteria are provably identical. Persistent and structural, but capital-intensive and not automatable risk-free.

**Mirages (widely claimed, do not build):**
- **Sports-betting momentum/value as a tradeable strategy** — real signal, structurally uncapturable at vig (Finding 4).
- **Naive automated funding-rate/CEX-DEX "arbitrage"** — net-negative on the majority of large spreads; the popular scanner repos don't even execute, for good reason.
- **Cross-venue prediction "risk-free arb" run automatically** — resolution-mismatch risk makes it not risk-free.
- **Any informational/forecasting edge on liquid efficient venues** (Kalshi weather/crypto fades) — already priced; confirmed dead by the prior effort (PSR < 0.5) and by the factor-zoo/overfitting literature.
- **NFL home-underdog ATS bias** — refuted, overfit.

---

## Caveats and Time-Sensitivity

- **Two key magnitude claims rest on a single unreviewed arXiv preprint** (2601.01706): the 2-4% cross-venue deviation (2-1 vote) and the ~6% cross-listing rate. The *existence* of the structural deviation is corroborated independently; the *exact magnitude* and its capturable fraction are softer.
- **Funding-rate results come from short windows** (6-8 days for MDPI; ~7 months for ScienceDirect, single regime). The Sharpe magnitudes and the "maturity" causal story are weak (the DEX-Sharpe claim was refuted; maturity is confounded with token incentives and small-market effects).
- **MDPI editorial rigor is occasionally questioned**, though the cited statistic is descriptive from the authors' own large dataset.
- **Decay is the universal time-sensitivity:** every structural edge here is documented to narrow as venues mature, semantic-alignment tooling improves, or arbitrage capital arrives. The Kalshi bias, DEX carry, and cross-venue deviations all decay; treat any measured edge as perishable and re-validate live.
- **Several primary PDFs returned HTTP 403** via proxy; verification relied on multiple independent mirrors/abstracts (this affected karlwhelan.com, SSRN, arxiv.org fetches but quotes were confirmed across 4+ sources each).
- **The Kalshi favorite-side positive return is not confirmed strictly net of Kalshi fees** in the available abstracts — the residual may be thinner than it appears.

---

## Open Questions

1. **What is the actual net-of-fee, net-of-adverse-selection P&L of a disciplined Kalshi maker on favorites?** The paper shows makers lose ~10% on average; the question is whether a *selective* favorite-only maker can cross into positive territory, which the aggregate statistic does not resolve.
2. **How much of the DEX funding-rate carry is durable structural premium vs. transient token-incentive subsidy?** Disentangling these determines whether the 5-8% DEX returns survive emissions expiry.
3. **What fraction of the ~6% cross-listed prediction-market pairs have genuinely identical, machine-verifiable resolution criteria** (true risk-free arb) vs. semantic near-matches that carry settlement risk? This is the difference between a real lane and a trap.
4. **Does the Pinnacle/Betfair-as-reference, soft-US-book-as-target favorite-longshot exploitation clear vig in practice** for a single operator, given line-shopping and bet-limit constraints — or does it join sports momentum as documented-but-uncapturable?