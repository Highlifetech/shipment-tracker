"""Per-user Base reads. Shared-token, legacy, export and mutation paths fail closed."""
import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from flask import jsonify, request, Response, redirect
import lark_auth
from fulfillment_base import BaseStore, BaseError
from fulfillment import inventory


class UserClient:
    def __init__(self, token):
        self.token = token
        self.base_url = lark_auth.BASE_URL
    def _headers(self):
        return {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}


def read_user_rows(token, settings):
    # Never derive readable fields or rows from the shared app snapshot.
    sources = settings.get('sources', [])
    def read(source):
        try:
            return BaseStore(UserClient(token), dict(settings, sources=[source], catalog_only=True)).source_rows()
        except BaseError as exc:
            # Explicit Base permission denial: that table contributes no records.
            # Other errors abort the response rather than falsely claiming a full sync.
            if getattr(exc, 'code', None) in (91403, 1254302):
                return []
            raise
    with ThreadPoolExecutor(max_workers=3) as pool:
        return [row for batch in pool.map(read, sources) for row in batch]


def register(app):
    @app.before_request
    def require_verified_base_access():
        path = request.path
        user = lark_auth.read_session(request.cookies.get(lark_auth.SESSION_COOKIE))
        token = lark_auth.delegated_token(user)
        if path == '/dashboard/health':
            return jsonify(ok=True), 200, {'Cache-Control': 'no-store'}
        if path == '/auth/lark/callback':
            expected = request.cookies.get('shipbot_oauth_state', '')
            given = request.args.get('state', '')
            if not expected or not secrets.compare_digest(expected, given):
                return jsonify(error='Sign-in expired. Reopen the shipping app.'), 403
            return None
        shell = path in ('/dashboard', '/fulfillment')
        api = path == '/api' or path.startswith('/api/')
        protected = shell or api or path.startswith('/dashboard/') or path.startswith('/fulfillment/') or path.startswith('/packing-list')
        if not protected:
            return None
        if shell:
            if not token:
                if not lark_auth.configured():
                    return jsonify(error='Lark sign-in needs administrator setup.'), 503
                public = os.environ.get('DASHBOARD_URL', request.host_url).rstrip('/')
                if public.endswith('/dashboard'):
                    public = public[:-10]
                state = secrets.token_urlsafe(32)
                response = redirect(lark_auth.login_url(public + '/auth/lark/callback', state))
                response.set_cookie('shipbot_oauth_state', state, max_age=600, secure=True, httponly=True, samesite='Lax')
                return response
            from fulfillment_web import page_html
            return Response(page_html(), mimetype='text/html')
        if not token:
            return jsonify(error='Sign in through Lark to load your orders.', code='LARK_SIGN_IN_REQUIRED'), 401
        if path == '/api/me':
            return jsonify(name=user.get('name'), open_id=user.get('open_id'))
        if path == '/api/shipping-workspace/tracking' and request.method == 'GET':
            # Legacy rows have no verified Base record/field linkage. Never expose them.
            return jsonify(data={'shipments': []}, sync={'last_synced': None, 'error': 'Legacy tracking is unavailable under Base permissions.'})
        if path == '/api/fulfillment/sync' and request.method == 'POST':
            return jsonify(queued=True)
        if path in ('/api/fulfillment/catalog', '/api/fulfillment/photo') and request.method == 'GET':
            try:
                settings = json.loads(os.environ.get('FULFILLMENT_CONFIG', '{}'))
                if not settings.get('base_token'):
                    return jsonify(error='Production Base connection is unavailable.'), 503
                if path.endswith('/catalog'):
                    rows = read_user_rows(token, settings)
                    return jsonify(items=inventory(rows, []), shipments=[], can_save=False,
                                   catalog_only=True, warehouse_address='', demo=False,
                                   synced_at=datetime.now(timezone.utc).isoformat(), sync={'error': None})
                if request.args.get('shipment'):
                    return jsonify(error='Saved packing-list access is not enabled.'), 403
                key = request.args.get('key', '')
                table, record = key.split(':', 1)
                source = next((s for s in settings['sources'] if s['table_id'] == table), None)
                if not source:
                    return jsonify(error='Photo unavailable.'), 403
                store = BaseStore(UserClient(token), dict(settings, sources=[source], catalog_only=True))
                from fulfillment_base import ident
                store.records = lambda t: [store.api('GET', store.root + '/tables/' + ident(t) + '/records/' + ident(record))['record']]
                row = next((r for r in store.source_rows() if r['key'] == key), None)
                if not row:
                    return jsonify(error='Photo unavailable.'), 403
                data, mime = store.photo(row)
                return Response(data, mimetype=mime, headers={'X-Content-Type-Options': 'nosniff'})
            except Exception:
                # No raw token/API payloads, app-token retry, shared cache or stale fallback.
                return jsonify(error='Lark could not verify access. Check the app’s user authorization scopes and Base access.'), 403
        return jsonify(error='This action requires verified shipment and export permissions. It is not enabled yet.'), 403

    @app.after_request
    def private_responses(response):
        if request.path.startswith(('/api', '/dashboard', '/fulfillment', '/packing-list', '/auth/')):
            response.headers['Cache-Control'] = 'no-store, private'
            response.headers['Vary'] = 'Cookie, Authorization'
        return response
