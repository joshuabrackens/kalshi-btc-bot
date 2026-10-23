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


def test_market_scanner_is_manual_bounded_and_displays_route_reasons_safely():
    html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    assert 'id="market-scan-button"' in html
    assert "getJSON('/api/market-scan')" in html
    assert "No orders were submitted" in html
    assert "market.route_status" in html and "market.reason" in html
    assert "addCell(tr,market.reason||'No routing reason provided.')" in html
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
