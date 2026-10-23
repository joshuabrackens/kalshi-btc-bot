# Single current-signal dashboard + 3-minute indicator response allowance

This update removes the dashboard's prominent "Latest TradingView alert" and "Last bot check" cards. The main view is now a single **Current signal** card backed only by the current UTC-aligned 15-minute window. Older UP/DOWN signals are not shown as current. A separate Alert history table is clearly labeled as history only.

The display status remains `WAIT THIS WINDOW` when there is no fresh UP/DOWN alert. During the first 180 seconds after each 15-minute window starts, the card explains that the indicator usually takes about 3 minutes to respond and shows the remaining time. After those 3 minutes, it explains that no fresh signal arrived within the expected response period. A fresh valid UP/DOWN alert or explicit WAIT in the current window updates the card as soon as the backend refreshes.

Important: the 3-minute period is **display grace only**. It does not extend the worker's configured signal freshness rules, it does not fabricate an alert, and it never opens/closes a trade. The fallback is not sent to the webhook queue.

## Install

Replace `main.py` and `dashboard.html` in the repository root, add `test_current_signal.py`, commit, and deploy the same commit to the Web Service and Background Worker. No environment variable is required; the response allowance defaults to 180 seconds. To change it, set `INDICATOR_RESPONSE_GRACE_SECONDS` on the Web Service; values are limited by the code to 0–900 seconds.

Keep the shared `DATABASE_URL` identical on both services, `PAPER_SIGNAL_SOURCE=hybrid`, `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and keep the Web Service's `PAPER_WORKER_ENABLED=false` when a separate Background Worker is the processor.

## Verify

Run:

```bash
python -m unittest -v test_current_signal.py test_wait_alerts.py test_signal_engine.py test_paper_bot_fixes.py
python -m py_compile main.py
```

Then check `/api/dashboard` for `current_window_decision.phase` values (`awaiting_indicator`, `no_fresh_signal`, `signal_received`, or `indicator_wait`) and inspect the current signal card. The alert history table is for reference only.

This remains paper-only; no live order path is added.
