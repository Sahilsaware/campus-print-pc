"""Campus Print server.

Flow:  upload -> (convert to PDF) -> order (settings + price) -> pay -> queue
       -> pushed to the PC agent over WebSocket -> agent prints -> status back.

Run (single worker is required, state is kept in memory):
    Linux server :  gunicorn -w 1 --threads 100 -b 127.0.0.1:5000 app:app
    Local testing:  python app.py
"""
import glob
import hashlib
import hmac
import io
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from flask_sock import Sock
from werkzeug.middleware.proxy_fix import ProxyFix

import admin
import processing as proc

# ------------------------------------------------------------------ config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
FRONTEND_DIR = os.path.join(ROOT_DIR, 'frontend')
load_dotenv(os.path.join(BASE_DIR, '.env'))

DATA_DIR = os.path.join(BASE_DIR, 'data')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
DB_PATH = os.path.join(DATA_DIR, 'campus.db')
os.makedirs(UPLOAD_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)s  %(message)s')
log = logging.getLogger('campus')

AGENT_KEY = os.environ.get('AGENT_KEY', '')
SECRET_KEY = os.environ.get('SECRET_KEY', '')
if len(AGENT_KEY) < 16:
    raise RuntimeError('Set AGENT_KEY (16+ characters) in backend/.env before starting.')
if len(SECRET_KEY) < 32:
    raise RuntimeError('Set SECRET_KEY (32+ random characters) in backend/.env before starting.')

PAYMENT_TEST_MODE = os.environ.get('PAYMENT_TEST_MODE', '1') == '1'
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', '')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '')
if not PAYMENT_TEST_MODE and not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
    raise RuntimeError('PAYMENT_TEST_MODE=0 needs RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in backend/.env.')
if PAYMENT_TEST_MODE:
    log.warning('PAYMENT_TEST_MODE is ON: orders are accepted WITHOUT real payment. '
                'Set PAYMENT_TEST_MODE=0 before going live.')

RATE_BW = int(os.environ.get('RATE_BW', 2))
RATE_COLOR = int(os.environ.get('RATE_COLOR', 10))
MAX_COPIES = 100
MAX_PAGES = 500
MAX_UPLOAD_MB = 100
PPS_OPTIONS = sorted(proc.GRIDS)
RAZORPAY_API = 'https://api.razorpay.com/v1'

app = Flask(__name__,
            static_folder=os.path.join(FRONTEND_DIR, 'static'),
            template_folder=os.path.join(FRONTEND_DIR, 'templates'))
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
)
if os.environ.get('BEHIND_PROXY', '0') == '1':
    # nginx / Cloudflare in front: use the real visitor IP (needed for the admin login lock)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
sock = Sock(app)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    if request.path.startswith(('/api/', '/admin')):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    total_pages INTEGER NOT NULL,
    status TEXT NOT NULL,            -- draft, awaiting_payment, queued, sent, printing, done, failed, refunded
    copies INTEGER NOT NULL DEFAULT 1,
    color_mode TEXT NOT NULL DEFAULT 'bw',
    paper_size TEXT NOT NULL DEFAULT 'A4',
    orientation TEXT NOT NULL DEFAULT 'portrait',
    duplex INTEGER NOT NULL DEFAULT 0,
    page_range TEXT NOT NULL DEFAULT '',
    pages_per_sheet INTEGER NOT NULL DEFAULT 1,
    fit TEXT NOT NULL DEFAULT 'fit',
    pages_selected INTEGER NOT NULL DEFAULT 0,
    sides INTEGER NOT NULL DEFAULT 0,
    total_price INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,   -- raised by admin "Retry" so the agent prints again
    razorpay_order_id TEXT,
    razorpay_payment_id TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

# columns added after the first release: (name, definition)
MIGRATIONS = [
    ('attempt', 'INTEGER NOT NULL DEFAULT 0'),
    ('razorpay_order_id', 'TEXT'),
    ('razorpay_payment_id', 'TEXT'),
]


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


with db() as _con:
    _con.execute('PRAGMA journal_mode=WAL')
    _con.executescript(SCHEMA)
    _have = {r['name'] for r in _con.execute('PRAGMA table_info(jobs)')}
    for _name, _definition in MIGRATIONS:  # upgrade an older campus.db in place
        if _name not in _have:
            _con.execute(f'ALTER TABLE jobs ADD COLUMN {_name} {_definition}')


def get_job(job_id):
    with db() as con:
        return con.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()


def public_job(row):
    return {
        'id': row['id'],
        'status': row['status'],
        'filename': row['filename'],
        'pages_selected': row['pages_selected'],
        'total_price': row['total_price'],
        'error': row['error'] if row['status'] == 'failed' else None,
    }


# ------------------------------------------------------------------- files

def file_path(job_id, suffix):
    # job ids are generated by us (uuid hex), never taken from user filenames
    return os.path.join(UPLOAD_DIR, f'{job_id}.{suffix}')


def remove_files(job_id):
    for p in glob.glob(os.path.join(UPLOAD_DIR, f'{job_id}.*')):
        try:
            os.remove(p)
        except OSError:
            pass


def err(message, code=400):
    return jsonify({'error': message}), code


def valid_id(job_id):
    return len(job_id) == 32 and all(c in '0123456789abcdef' for c in job_id)


# ------------------------------------------------------------ rate limiting

_hits = {}
_hits_lock = threading.Lock()


def rate_limited(key, limit, window):
    """True if `key` already made `limit` calls in the last `window` seconds."""
    now = time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(key, []) if now - t < window]
        if len(recent) >= limit:
            _hits[key] = recent
            return True
        recent.append(now)
        _hits[key] = recent
    return False


# ----------------------------------------------- live updates to browsers

subscribers = {}  # job_id -> set of queue.Queue
subs_lock = threading.Lock()


def notify(job_id):
    row = get_job(job_id)
    if not row:
        return
    payload = public_job(row)
    with subs_lock:
        for q in subscribers.get(job_id, ()):
            q.put(payload)


def set_status(job_id, status, error=None):
    with db() as con:
        con.execute('UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?',
                    (status, error, time.time(), job_id))
    notify(job_id)


# ------------------------------------------------------------ agent state

agent = {'ws': None, 'printer': '', 'printer_ok': False, 'last_seen': 0.0}
agent_send_lock = threading.Lock()


def agent_connected():
    return agent['ws'] is not None and time.time() - agent['last_seen'] < 60


def printer_online():
    return agent_connected() and agent['printer_ok']


def printer_message():
    if not agent_connected():
        return 'Printer is offline'
    if not agent['printer_ok']:
        return 'Printer needs attention'
    return 'Printer is ready'


def printer_summary():
    return {'online': printer_online(), 'message': printer_message(), 'name': agent['printer']}


def agent_send(message):
    ws = agent['ws']
    if ws is None:
        return False
    try:
        with agent_send_lock:
            ws.send(json.dumps(message))
        return True
    except Exception:
        return False


def dispatch(job_id):
    """Push a queued job to the agent. If no agent is connected it stays queued."""
    row = get_job(job_id)
    if not row or row['status'] not in ('queued', 'sent'):
        return
    sent = agent_send({
        'type': 'job',
        'job': {
            'id': row['id'],
            'attempt': row['attempt'],
            'copies': row['copies'],
            'duplex': bool(row['duplex']),
            'color_mode': row['color_mode'],
            'paper_size': row['paper_size'],
            'orientation': row['orientation'],
            'file_path': f"/agent/files/{row['id']}",
        },
    })
    if sent and row['status'] == 'queued':
        set_status(job_id, 'sent')


def dispatch_pending():
    with db() as con:
        ids = [r['id'] for r in con.execute(
            "SELECT id FROM jobs WHERE status IN ('queued', 'sent') ORDER BY created_at")]
    for job_id in ids:
        dispatch(job_id)


def agent_authorized():
    header = request.headers.get('Authorization', '')
    token = header[7:] if header.startswith('Bearer ') else ''
    return hmac.compare_digest(token.encode(), AGENT_KEY.encode())


def handle_agent_message(msg):
    kind = msg.get('type')
    if kind in ('hello', 'ping'):
        agent['printer'] = str(msg.get('printer', ''))[:100]
        agent['printer_ok'] = bool(msg.get('printer_ok', False))
        agent_send({'type': 'pong'})
    elif kind == 'status':
        job_id = str(msg.get('job_id', ''))
        state = msg.get('state')
        row = get_job(job_id) if valid_id(job_id) else None
        if not row or row['status'] not in ('sent', 'printing'):
            return
        try:
            attempt = int(msg.get('attempt', 0))
        except (TypeError, ValueError):
            return
        if attempt != row['attempt']:
            return  # an old result of a previous try (before the admin pressed Retry)
        if state == 'printing':
            set_status(job_id, 'printing')
        elif state == 'done':
            set_status(job_id, 'done')
            remove_files(job_id)  # privacy: delete the document right after printing
        elif state == 'failed':
            set_status(job_id, 'failed', str(msg.get('error', 'Print failed'))[:200])


@sock.route('/ws/agent')
def ws_agent(ws):
    if not agent_authorized():
        ws.close(1008, 'unauthorized')
        return
    old = agent['ws']
    agent.update(ws=ws, last_seen=time.time(), printer_ok=False)
    if old is not None and old is not ws:
        try:
            old.close()
        except Exception:
            pass
    try:
        dispatch_pending()
        while True:
            raw = ws.receive(timeout=45)   # the agent pings every ~10 s
            if raw is None:
                break
            agent['last_seen'] = time.time()
            try:
                handle_agent_message(json.loads(raw))
            except (ValueError, TypeError):
                continue
            dispatch_pending()
    except Exception:
        pass
    finally:
        if agent['ws'] is ws:
            agent.update(ws=None, printer_ok=False)


@sock.route('/ws/job/<job_id>')
def ws_job(ws, job_id):
    row = get_job(job_id) if valid_id(job_id) else None
    if not row:
        ws.close(1008, 'not found')
        return
    q = queue.Queue()
    with subs_lock:
        subscribers.setdefault(job_id, set()).add(q)
    try:
        ws.send(json.dumps(public_job(row)))
        while True:
            try:
                payload = q.get(timeout=25)
                ws.send(json.dumps(payload))
                if payload['status'] in ('done', 'failed', 'refunded'):
                    break
            except queue.Empty:
                ws.send(json.dumps({'type': 'ping'}))  # keep-alive; also detects a closed tab
    except Exception:
        pass
    finally:
        with subs_lock:
            subscribers.get(job_id, set()).discard(q)
            if not subscribers.get(job_id):
                subscribers.pop(job_id, None)


# ---------------------------------------------------------------- settings

def parse_settings(data, total_pages):
    color = data.get('color_mode', 'bw')
    paper = data.get('paper_size', 'A4')
    orientation = data.get('orientation', 'portrait')
    fit = data.get('fit', 'fit')
    if color not in ('bw', 'color'):
        raise ValueError('Invalid color mode')
    if paper not in proc.PAPER_SIZES:
        raise ValueError('Invalid paper size')
    if orientation not in ('portrait', 'landscape'):
        raise ValueError('Invalid orientation')
    if fit not in ('fit', 'actual'):
        raise ValueError('Invalid scaling option')
    try:
        copies = int(data.get('copies', 1))
        pps = int(data.get('pages_per_sheet', 1))
    except (TypeError, ValueError):
        raise ValueError('Invalid number')
    if not 1 <= copies <= MAX_COPIES:
        raise ValueError(f'Copies must be between 1 and {MAX_COPIES}')
    if pps not in PPS_OPTIONS:
        raise ValueError('Invalid pages-per-sheet option')
    page_range = str(data.get('page_range', ''))[:100]
    pages = proc.parse_page_range(page_range, total_pages)
    return {
        'color_mode': color, 'paper_size': paper, 'orientation': orientation,
        'fit': fit if pps == 1 else 'fit', 'copies': copies, 'pps': pps,
        'duplex': bool(data.get('duplex', False)), 'page_range': page_range.strip(),
        'pages': pages,
    }


def price_for(settings):
    sides = math.ceil(len(settings['pages']) / settings['pps'])
    rate = RATE_COLOR if settings['color_mode'] == 'color' else RATE_BW
    return sides, sides * settings['copies'] * rate


def load_draft(job_id, statuses=('draft', 'awaiting_payment')):
    row = get_job(job_id) if valid_id(job_id) else None
    if not row or row['status'] not in statuses:
        return None
    return row


def render_print(job_id, s):
    """Print-ready PDF bytes for these settings, or None if the layout step failed."""
    src = file_path(job_id, 'pdf')
    if not os.path.exists(src):
        return None
    try:
        return proc.build_print_pdf(src, s['pages'], s['paper_size'],
                                    s['orientation'], s['pps'], s['fit'])
    except Exception:
        log.exception('Layout failed for job %s', job_id)
        return None


# ---------------------------------------------------------------- payments

def razorpay_refund(row):
    """Refund the customer through Razorpay. Returns (ok, message)."""
    payment_id = row['razorpay_payment_id']
    if PAYMENT_TEST_MODE or not payment_id:
        return True, 'No online payment to refund.'
    try:
        resp = requests.post(f'{RAZORPAY_API}/payments/{payment_id}/refund',
                             auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=20,
                             json={'amount': row['total_price'] * 100,
                                   'notes': {'job_id': row['id']}})
    except requests.RequestException:
        log.exception('Razorpay refund request failed for job %s', row['id'])
        return False, 'Could not reach Razorpay. Try again in a moment.'
    if resp.status_code == 200:
        return True, 'Refund started.'
    try:
        detail = resp.json().get('error', {}).get('description', '')
    except ValueError:
        detail = ''
    log.error('Razorpay refund failed for job %s: %s %s', row['id'], resp.status_code, detail)
    return False, f'Razorpay refused the refund: {detail or resp.status_code}'


# ------------------------------------------------------------------ routes

@app.get('/')
def home():
    return render_template('index.html')


@app.get('/privacy')
def privacy():
    return render_template('privacy.html')


@app.get('/api/config')
def api_config():
    return jsonify({
        'rates': {'bw': RATE_BW, 'color': RATE_COLOR},
        'max_copies': MAX_COPIES,
        'max_upload_mb': MAX_UPLOAD_MB,
        'pages_per_sheet': PPS_OPTIONS,
        'extensions': sorted(proc.ALLOWED_EXTS),
        'payment_test_mode': PAYMENT_TEST_MODE,
    })


@app.get('/api/printer-status')
def api_printer_status():
    return jsonify({'online': printer_online(), 'message': printer_message()})


@app.post('/api/upload')
def api_upload():
    if rate_limited(('upload', request.remote_addr), 20, 60):
        return err('Too many uploads. Please wait a minute and try again.', 429)
    f = request.files.get('file')
    if not f or not f.filename:
        return err('Please choose a file.')
    name = os.path.basename(f.filename)[:120]
    ext = os.path.splitext(name)[1].lower()
    if ext not in proc.ALLOWED_EXTS:
        return err('Supported formats: PDF, JPG, PNG, Word, PowerPoint, Excel.')

    job_id = uuid.uuid4().hex
    original = file_path(job_id, 'orig' + ext)
    f.save(original)
    try:
        pages = proc.prepare_pdf(original, file_path(job_id, 'pdf'), ext)
    except proc.ProcessingError as exc:
        remove_files(job_id)
        return err(str(exc))
    if pages > MAX_PAGES:
        remove_files(job_id)
        return err(f'Files longer than {MAX_PAGES} pages are not supported.')
    try:
        os.remove(original)  # only the converted PDF is needed from here on
    except OSError:
        pass

    now = time.time()
    with db() as con:
        con.execute(
            'INSERT INTO jobs (id, filename, ext, total_pages, status, created_at, updated_at) '
            "VALUES (?, ?, ?, ?, 'draft', ?, ?)", (job_id, name, ext, pages, now, now))
    return jsonify({'file_id': job_id, 'filename': name, 'pages': pages})


@app.post('/api/files/<job_id>/preview')
def api_preview(job_id):
    row = load_draft(job_id)
    if not row:
        return err('File not found or expired.', 404)
    try:
        s = parse_settings(request.get_json(silent=True) or {}, row['total_pages'])
    except ValueError as exc:
        return err(str(exc))
    data = render_print(job_id, s)
    if data is None:
        return err('Could not prepare the preview. Please upload the file again.', 500)
    return send_file(io.BytesIO(data), mimetype='application/pdf')


@app.post('/api/files/<job_id>/order')
def api_order(job_id):
    row = load_draft(job_id)
    if not row:
        return err('File not found or expired.', 404)
    try:
        s = parse_settings(request.get_json(silent=True) or {}, row['total_pages'])
    except ValueError as exc:
        return err(str(exc))

    sides, total = price_for(s)
    data = render_print(job_id, s)
    if data is None:
        return err('Could not prepare the file for printing. Please upload it again.', 500)
    with open(file_path(job_id, 'print.pdf'), 'wb') as fh:
        fh.write(data)

    with db() as con:
        # razorpay_order_id is reset: a changed price needs a fresh payment order
        con.execute(
            "UPDATE jobs SET status = 'awaiting_payment', copies = ?, color_mode = ?, "
            'paper_size = ?, orientation = ?, duplex = ?, page_range = ?, pages_per_sheet = ?, '
            'fit = ?, pages_selected = ?, sides = ?, total_price = ?, razorpay_order_id = NULL, '
            'updated_at = ? WHERE id = ?',
            (s['copies'], s['color_mode'], s['paper_size'], s['orientation'], int(s['duplex']),
             s['page_range'], s['pps'], s['fit'], len(s['pages']), sides, total, time.time(), job_id))
    return jsonify({
        'job_id': job_id, 'pages_selected': len(s['pages']), 'sheets': sides,
        'copies': s['copies'], 'total_price': total, 'printer_online': printer_online(),
    })


@app.post('/api/jobs/<job_id>/create-payment')
def api_create_payment(job_id):
    """Live mode only: create the Razorpay order the browser will pay."""
    if PAYMENT_TEST_MODE:
        return err('Online payment is switched off (test mode).', 400)
    row = load_draft(job_id, ('awaiting_payment',))
    if not row:
        return err('File not found or expired.', 404)
    if not printer_online():
        return err('The printer is offline right now. Please try again in a moment.', 503)
    amount = row['total_price'] * 100  # paise
    try:
        resp = requests.post(f'{RAZORPAY_API}/orders', auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                             json={'amount': amount, 'currency': 'INR', 'receipt': job_id,
                                   'notes': {'job_id': job_id}}, timeout=20)
        resp.raise_for_status()
        order = resp.json()
    except (requests.RequestException, ValueError):
        log.exception('Could not create Razorpay order for job %s', job_id)
        return err('Could not start the payment. Please try again.', 502)
    with db() as con:
        con.execute('UPDATE jobs SET razorpay_order_id = ?, updated_at = ? WHERE id = ?',
                    (order['id'], time.time(), job_id))
    return jsonify({
        'key_id': RAZORPAY_KEY_ID, 'order_id': order['id'], 'amount': amount, 'currency': 'INR',
        'name': 'Campus Print', 'description': f"{row['filename']} ({row['pages_selected']} pages)",
    })


@app.post('/api/jobs/<job_id>/pay')
def api_pay(job_id):
    row = load_draft(job_id, ('awaiting_payment',))
    if not row:
        return err('This order cannot be paid (expired or already paid).', 409)

    payment_id = None
    if PAYMENT_TEST_MODE:
        if not printer_online():
            return err('The printer is offline right now. Please try again in a moment.', 503)
    else:
        data = request.get_json(silent=True) or {}
        order_id = str(data.get('razorpay_order_id', ''))
        payment_id = str(data.get('razorpay_payment_id', ''))
        signature = str(data.get('razorpay_signature', ''))
        stored = row['razorpay_order_id'] or ''
        if not stored or not hmac.compare_digest(order_id.encode(), stored.encode()):
            return err('Payment could not be verified.', 400)
        expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), f'{order_id}|{payment_id}'.encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            log.warning('Bad Razorpay signature for job %s', job_id)
            return err('Payment could not be verified.', 400)
        # A paid job is always queued, even if the printer went offline meanwhile:
        # it is sent as soon as the agent reconnects.

    with db() as con:
        cur = con.execute(
            "UPDATE jobs SET status = 'queued', razorpay_payment_id = ?, updated_at = ? "
            "WHERE id = ? AND status = 'awaiting_payment'", (payment_id, time.time(), job_id))
        changed = cur.rowcount
    if not changed:
        return err('This order cannot be paid (expired or already paid).', 409)
    notify(job_id)
    dispatch(job_id)
    return jsonify({'success': True})


@app.get('/api/jobs/<job_id>')
def api_job(job_id):
    row = get_job(job_id) if valid_id(job_id) else None
    if not row:
        return err('Job not found.', 404)
    return jsonify(public_job(row))


@app.get('/agent/files/<job_id>')
def agent_file(job_id):
    if not agent_authorized():
        return err('Unauthorized', 401)
    row = get_job(job_id) if valid_id(job_id) else None
    path = file_path(job_id, 'print.pdf') if row else ''
    if not row or row['status'] not in ('sent', 'printing') or not os.path.exists(path):
        return err('Not available', 404)
    return send_file(path, mimetype='application/pdf')


# ------------------------------------------------------------------- admin

admin.register(app, db, get_job=get_job, set_status=set_status, dispatch=dispatch,
               remove_files=remove_files, file_path=file_path,
               printer_summary=printer_summary, refund_payment=razorpay_refund)


# ----------------------------------------------------------------- cleanup

def cleanup_loop():
    while True:
        time.sleep(600)
        try:
            now = time.time()
            with _hits_lock:
                _hits.clear()
            with db() as con:
                stale = [r['id'] for r in con.execute(
                    "SELECT id FROM jobs WHERE status IN ('draft', 'awaiting_payment') AND updated_at < ?",
                    (now - 3600,))]
                failed = [r['id'] for r in con.execute(
                    "SELECT id FROM jobs WHERE status = 'failed' AND updated_at < ?", (now - 86400,))]
                for job_id in stale:
                    con.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
            for job_id in stale + failed:
                remove_files(job_id)
        except Exception:
            log.exception('Cleanup failed')


threading.Thread(target=cleanup_loop, daemon=True).start()

if __name__ == '__main__':
    app.run('127.0.0.1', int(os.environ.get('PORT', 5000)))
