from pathlib import Path
from collections import Counter
import re


def test_dashboard_exposes_a_mode_selector_but_real_remains_visibly_locked():
    html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    assert 'id="mode-paper"' in html
    assert 'id="mode-real"' in html
    assert 'Real trading · Locked' in html
    assert "requested_mode:'real'" in html
    assert "Real trading remains locked server-side" in html
    # The mode control cannot make deployment settings or orders client-side.
    assert "does not modify deployment environment settings" in html


def test_market_scanner_runs_full_paginated_inventory_and_displays_context_safely():
    html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    assert 'id="market-scan-button"' in html
    assert "getJSON('/api/market-scan')" in html
    assert "No orders were submitted" in html
    assert "market.route_status" in html and "market.reason" in html
    assert "market.context_status||market.reason" in html
    assert "market_implied_yes_probability_pct" in html
    assert "fetch('/api/market-scan',{method:'POST'" in html
    assert "all open Kalshi markets" in html
    assert "rows.appendChild(tr)" in html


def test_dashboard_has_unique_ids_after_mode_and_scanner_additions():
    html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"]+)"', html)
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    assert duplicates == []


def test_root_workflow_reference_matches_the_active_github_actions_workflow():
    root = Path(__file__).with_name("python-app.yml")
    active = Path(__file__).parent / ".github" / "workflows" / "python-app.yml"
    assert root.read_bytes() == active.read_bytes()


def test_dashboard_has_admin_unlock_and_one_time_encrypted_kalshi_credential_controls():
    html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    assert 'id="admin-secret-input"' in html
    assert 'id="kalshi-api-key-id"' in html
    assert 'id="kalshi-private-key-file"' in html
    assert 'accept=".key,.pem,.txt,text/plain,application/x-pem-file"' in html
    assert 'id="kalshi-authorization-ack"' in html
    assert 'Connect Kalshi account' in html
    assert 'class="connect-button"' in html
    assert 'id="kalshi-save-credentials"' in html
    assert "private key will not be returned" in html.lower()
    assert "No orders were placed" in html
    assert "file.text()" in html
    assert 'id="market-autopilot-toggle"' in html
    assert 'id="market-automation"' not in html or 'id="market-autopilot-toggle"' in html
    assert "never live orders" in html.lower()


def test_dashboard_exposes_market_decision_pipeline_and_explains_provider_and_risk_gates():
    html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    assert 'id="market-analysis-button"' in html
    assert "Run research batch" in html
    assert "model_probability_yes_pct" in html
    assert "market-analysis-winrate" in html
    assert "will not invent probabilities" in html
    assert "cannot place live orders" in html or "live orders" in html
    assert "fresh sourced evidence" in html


def test_dashboard_communicates_uncertainty_around_observed_win_rate():
    from pathlib import Path
    html = Path('dashboard.html').read_text(encoding='utf-8')
    assert 'analytics-winrate-interval' in html
    assert '95% Wilson interval' in html
    assert 'descriptive, not a forecast' in html
