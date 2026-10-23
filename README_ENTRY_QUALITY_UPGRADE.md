> **Historical note:** This file describes an earlier, individual patch and is retained for context. For the current reconciled repository, environment settings, test results, and deployment checklist, use `README.md`, `BTC_BOT_MASTER_CHANGELOG.md`, and `PACKAGE_MANIFEST.md`. Do not apply an old file list in isolation.

# BTC Entry Quality + Performance Audit Upgrade

This update keeps the existing paper-only architecture and the corrected order-book-derived quotes. It adds two controlled improvements:

1. **Preferred entry target plus hard cap.** `PAPER_MAX_ENTRY_CENTS` is the actual hard cap (default 92). `PAPER_ENTRY_TARGET_MAX_CENTS` defaults to 80 and is a preferred price for diagnostics and learning, not a blocker. A fresh TradingView-primary hybrid signal can enter above the preference up to the hard cap if it passes the major-flaw veto, current executable order-book quote/spread, timeframe/window freshness, daily-loss, and trade-budget checks. Above the hard cap it waits and does not open a position. High prices are permitted by request but are not inherently good value: they offer little maximum upside relative to the possible loss.

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

The 80c preferred target is a reference for reporting and post-trade learning, not an execution gate. The 92c hard cap is the only fixed contract-price ceiling by default; the worker still requires a fresh external alert, an executable order-book quote, acceptable spread, valid market timing, hybrid major-flaw checks, and budget/risk approval. A lower ask is not automatically better value, and the direction/evidence score is not a calibrated probability. Review paper results before tuning the hard cap or risk limits.

The current entry target is separate from signal score. The internal evidence score is not a win probability. No live order path is added by this patch.

### Entry-price semantics update

`PAPER_ENTRY_TARGET_MAX_CENTS` (default 80¢) is a soft preferred price, not a trade blocker. A directional signal that passes freshness, major-flaw, quote, spread, risk, and budget checks may enter above that preference up to `PAPER_MAX_ENTRY_CENTS` (default hard cap 92¢). Quotes above the hard cap wait and are not opened. Higher entry prices leave less maximum upside; a direction/evidence score is not a calibrated win probability.
