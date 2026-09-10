"""Standalone launcher for the Flask dashboard only (no Discord bots).

Used by the UI redesign verification to boot the dashboard, log in as admin,
and exercise every view so the new Aurora Glass CSS can be screenshotted.
"""
import os
os.environ.setdefault('LAZYFARMERS_DASHBOARD_USER', 'admin')
os.environ.setdefault('LAZYFARMERS_DASHBOARD_PASSWORD', 'auroratest2025')

from dashboard.app import app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    print(f"[dashboard] listening on 0.0.0.0:{port}", flush=True)
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
