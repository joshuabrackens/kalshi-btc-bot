"""Syntax-check inline dashboard JavaScript with Node.js when available."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    dashboard = Path(__file__).with_name("dashboard.html")
    html = dashboard.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.I | re.S)
    if not scripts:
        raise SystemExit("No inline JavaScript blocks found")
    node = shutil.which("node")
    if not node:
        print("DASHBOARD JS SYNTAX: SKIP (Node.js not installed; HTML checks still run in pytest)")
        return
    checked = 0
    with tempfile.TemporaryDirectory(prefix="btc-dashboard-js-") as temp:
        for index, script in enumerate(scripts):
            if not script.strip():
                continue
            path = Path(temp) / f"dashboard-{index}.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(path)], text=True, capture_output=True)
            if result.returncode:
                raise SystemExit(result.stderr[-4000:] or f"JavaScript syntax check failed for inline script {index}")
            checked += 1
    print(f"DASHBOARD JS SYNTAX: PASS ({checked} inline script block(s))")


if __name__ == "__main__":
    main()
