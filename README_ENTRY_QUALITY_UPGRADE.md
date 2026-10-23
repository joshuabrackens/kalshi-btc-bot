> **Historical note:** This file describes an earlier, individual patch and is retained for context. For the current reconciled repository, environment settings, test results, and deployment checklist, use `README.md`, `BTC_BOT_MASTER_CHANGELOG.md`, and `PACKAGE_MANIFEST.md`. Do not apply an old file list in isolation.

# BTC Entry Quality + Performance Audit Upgrade

This update keeps the existing paper-only architecture and the corrected order-book-derived quotes. It adds two controlled improvements:

1. **No-chase entry target.** `PAPER_MAX_ENTRY_CENTS` remains the configured hard cap (current setting may be 92). New `PAPER_ENTRY_TARGET_MAX_CENTS` defaults to 80. The worker only opens a paper position when the live order-book-derived ask is at or below `min(PAPER_MAX_ENTRY_CENTS, PAPER_ENTRY_TARGET_MAX_CENTS)`. If a fresh TradingView alert arrives above the target, it remains pending as `waiting_for_better_price`; each worker cycle rechecks the actual order book, signal freshness, spread and (in hybrid mode) the current internal veto checks. If price never gets into the target before the alert expires, the alert is ignored as stale and no trade is opened. The worker never buys at the expensive quote simply because it is below the old 92c hard cap.

2. **Trade-performance audit.** Newly opened trades link to the exact signal-log row and record the internal evidence score when applicable. `/api/dashboard`, `/paper/trades`, and `/paper/stats` provide source-level performance summaries. Legacy trades are classified from stored fields/notes only; historical signal scores are left blank rather than invented. The dashboard displays source-level results and entry metadata.

## Files

Replace at the repository root:
- `main.py`
- `paper_worker.py`
- `db_store.py`
- `dashboard.html`

Add:
- `test_entry_quality.py`
- `test_performance_audit.py`
- `README_ENTRY_QUALITY_UPGRADE.md`

Commit message: `Add BTC entry price guard and strategy audit`

## Render environment

Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`. Do not loosen trade size or daily loss limits. Add `PAPER_ENTRY_TARGET_MAX_CENTS=80` to both Web Service and Background Worker environment variables so the chosen target is explicit and consistent. Leave the existing `PAPER_MAX_ENTRY_CENTS` at its current hard cap; it is not raised by this patch.

## Trade-offs

The 80c target is a cautious initial paper-test value, not a proven optimal price. It can reduce expensive entries but may skip trades that never pull back. A lower ask also does not automatically mean better value: a price decline can reflect a weakening signal. The external alert must still be fresh, the book/spread must pass, and hybrid mode re-runs its checks when the target is reached. Track at least several dozen additional paper trades before tuning the target. The price proxy is not a calibrated probability.

The current entry target is separate from signal score. The internal evidence score is not a win probability. No live order path is added by this patch.
