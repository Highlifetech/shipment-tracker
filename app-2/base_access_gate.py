"""Fail-closed shipping access while delegated Base authorization is unavailable.

An identity cookie or shared dashboard token is NOT a record permission grant.
Do not remove this gate until record, field, attachment and export permissions
are verified server-side with the acting user's Lark authorization. Legacy
shipping sheets cannot be treated as an authorized fallback for Base records.
"""
from flask import jsonify, request, Response

LOCKED_PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Shipping · Access setup</title>
<style>*{box-sizing:border-box}body{margin:0;background:#f6f7fa;color:#1d2940;font:15px system-ui,-apple-system,sans-serif}header{padding:24px max(24px,5vw);background:white;border-bottom:1px solid #e3e7ee;font-size:20px;font-weight:650}header span{font-size:12px;color:#69768b;display:block;margin-bottom:4px;letter-spacing:.08em}main{max-width:560px;margin:10vh auto;padding:0 24px}.card{background:white;padding:36px;border:1px solid #e3e7ee;border-radius:16px;box-shadow:0 8px 30px #17213a06}.badge{display:inline-block;padding:6px 10px;background:#eef2ff;color:#3654be;border-radius:6px;font-size:12px;font-weight:600}h1{font-size:26px;letter-spacing:-.6px;margin:20px 0 12px}p{color:#5e6b80;line-height:1.65;margin:0 0 24px}a{display:inline-block;background:#355bea;color:white;padding:12px 18px;border-radius:8px;text-decoration:none;font-weight:600}a:focus-visible{outline:3px solid #8299ff;outline-offset:4px}.note{font-size:12px;margin:18px 0 0;color:#68768a}</style></head>
<body><header>Shipping</header><main><section class="card"><h1>Access paused</h1><p>Lark permissions setup is required.</p><a href="https://off-menu.jp.larksuite.com/base/VcAlbwImaab1KlsFLBVjunTNp1c" rel="noreferrer">Open Lark ↗</a></section></main></body></html>'''


def register(app):
    @app.before_request
    def require_verified_base_access():
        path = request.path
        if path == '/dashboard/health':
            return jsonify(ok=True, access='locked'), 200, {'Cache-Control': 'no-store'}
        protected = (path == '/dashboard' or path.startswith('/dashboard/')
                     or path == '/fulfillment' or path.startswith('/fulfillment/')
                     or path == '/packing-list' or path.startswith('/packing-list/')
                     or path == '/api' or path.startswith('/api/'))
        if not protected:
            return None
        # There is intentionally no environment flag, shared-token exception,
        # owner-based approximation, or app-token fallback that bypasses this.
        response = jsonify(
            error='Shipping access is temporarily locked. Your existing Lark Base '
                  'Advanced Permissions cannot yet be verified by this app. '
                  'Use Lark Base directly until delegated authorization is connected.',
            code='BASE_PERMISSION_VERIFICATION_UNAVAILABLE')
        if not path.startswith('/api'):
            response = Response(LOCKED_PAGE, mimetype='text/html')
        response.status_code = 403
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['Vary'] = 'Cookie, Authorization'
        return response
