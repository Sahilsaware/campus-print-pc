"""Admin panel: login, dashboard data, failed-job actions and sub-admin management."""
import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from functools import wraps

from flask import jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

SUPER_ROLE = 'Super Admin'
SUB_ROLE = 'Sub-Admin'
MONTH_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
USERNAME_RE = re.compile(r'^[A-Za-z0-9_.-]{3,30}$')
MAX_FAILS = 5
LOCK_SECONDS = 600
failed_logins = {}  # ip -> [count, first_failure_time]


def register(app, db, *, get_job, set_status, dispatch, remove_files, file_path, printer_summary,
             refund_payment):
    super_user = os.environ.get('ADMIN_USER', '')
    super_pass = os.environ.get('ADMIN_PASSWORD', '')
    if not super_user or len(super_pass) < 10:
        raise RuntimeError('Set ADMIN_USER and ADMIN_PASSWORD (10+ characters) in the environment.')

    with db() as con:
        con.execute('CREATE TABLE IF NOT EXISTS admins ('
                    'username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created_at REAL NOT NULL)')

    # ---------------------------------------------------------- helpers

    def same(a, b):
        return hmac.compare_digest(a.encode(), b.encode())

    def authenticate(username, password):
        user_ok = same(username, super_user)
        pass_ok = same(password, super_pass)
        if user_ok and pass_ok:
            return SUPER_ROLE
        with db() as con:
            row = con.execute('SELECT password_hash FROM admins WHERE username = ?',
                              (username,)).fetchone()
        if row and check_password_hash(row['password_hash'], password):
            return SUB_ROLE
        return None

    def is_locked(ip):
        entry = failed_logins.get(ip)
        if not entry:
            return False
        if time.time() - entry[1] > LOCK_SECONDS:
            failed_logins.pop(ip, None)
            return False
        return entry[0] >= MAX_FAILS

    def record_failure(ip):
        entry = failed_logins.get(ip)
        if not entry or time.time() - entry[1] > LOCK_SECONDS:
            failed_logins[ip] = [1, time.time()]
        else:
            entry[0] += 1

    def csrf_token():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_hex(16)
        return session['csrf']

    def session_valid():
        if not session.get('admin'):
            return False
        if session.get('role') == SUPER_ROLE:
            return True
        with db() as con:  # a revoked sub-admin is logged out immediately
            return con.execute('SELECT 1 FROM admins WHERE username = ?',
                               (session.get('username', ''),)).fetchone() is not None

    def admin_required(super_only=False):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                if not session_valid():
                    session.clear()
                    if request.path.startswith('/admin/api/'):
                        return jsonify({'error': 'Please log in again.'}), 401
                    return redirect('/admin/login')
                if super_only and session.get('role') != SUPER_ROLE:
                    return jsonify({'error': 'Only the super admin can do this.'}), 403
                if request.method not in ('GET', 'HEAD'):
                    sent = request.headers.get('X-CSRF-Token') or request.form.get('csrf', '')
                    if not same(sent, session.get('csrf', '')):
                        return jsonify({'error': 'Invalid request token. Reload the page.'}), 403
                return view(*args, **kwargs)
            return wrapped
        return decorator

    def month_bounds(month):
        year, mon = int(month[:4]), int(month[5:])
        start = datetime(year, mon, 1)
        end = datetime(year + (mon == 12), mon % 12 + 1, 1)
        return start.timestamp(), end.timestamp()

    def job_out(r):
        return {
            'id': r['id'],
            'filename': r['filename'],
            'status': r['status'],
            'error': r['error'],
            'time': datetime.fromtimestamp(r['updated_at']).strftime('%d %b %Y, %H:%M'),
            'pages': r['pages_selected'],
            'copies': r['copies'],
            'color_mode': r['color_mode'],
            'paper_size': r['paper_size'],
            'pages_per_sheet': r['pages_per_sheet'],
            'duplex': bool(r['duplex']),
            'amount': r['total_price'],
            'attempt': r['attempt'],
            'payment_id': r['razorpay_payment_id'],
        }

    # ------------------------------------------------------------ login

    @app.route('/admin/login', methods=['GET', 'POST'])
    def admin_login():
        if request.method == 'GET':
            return render_template('admin_login.html', error=None)
        ip = request.remote_addr or '?'
        if is_locked(ip):
            return render_template(
                'admin_login.html', error='Too many failed attempts. Try again in 10 minutes.'), 429
        username = request.form.get('username', '').strip()[:60]
        password = request.form.get('password', '')[:200]
        role = authenticate(username, password)
        if not role:
            record_failure(ip)
            return render_template('admin_login.html', error='Invalid username or password.'), 401
        failed_logins.pop(ip, None)
        session.clear()
        session.permanent = True
        session.update(admin=True, username=username, role=role)
        csrf_token()
        return redirect('/admin')

    @app.post('/admin/logout')
    @admin_required()
    def admin_logout():
        session.clear()
        return redirect('/admin/login')

    # -------------------------------------------------------- dashboard

    @app.get('/admin')
    @admin_required()
    def admin_dashboard():
        return render_template('admin.html', username=session['username'], role=session['role'],
                               is_super=session['role'] == SUPER_ROLE, csrf=csrf_token())

    @app.get('/admin/api/summary')
    @admin_required()
    def admin_summary():
        month = request.args.get('month', '')
        rng, args = '', []
        if month:
            if not MONTH_RE.match(month):
                return jsonify({'error': 'Invalid month.'}), 400
            start, end = month_bounds(month)
            rng, args = ' AND updated_at >= ? AND updated_at < ?', [start, end]
        with db() as con:
            stats = con.execute(
                "SELECT COUNT(*) AS prints, COALESCE(SUM(total_price), 0) AS earnings, "
                "COALESCE(SUM(sides * copies), 0) AS pages FROM jobs WHERE status = 'done'" + rng,
                args).fetchone()
            queue = con.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'sent', 'printing', 'failed') "
                'ORDER BY created_at').fetchall()
            history = con.execute(
                "SELECT * FROM jobs WHERE status IN ('done', 'refunded')" + rng +
                ' ORDER BY updated_at DESC LIMIT 200', args).fetchall()
        return jsonify({
            'stats': {'prints': stats['prints'], 'earnings': stats['earnings'], 'pages': stats['pages']},
            'printer': printer_summary(),
            'queue': [job_out(r) for r in queue],
            'history': [job_out(r) for r in history],
        })

    # ------------------------------------------------------ job actions

    @app.post('/admin/api/jobs/<job_id>/retry')
    @admin_required()
    def admin_retry(job_id):
        row = get_job(job_id)
        if not row or row['status'] != 'failed':
            return jsonify({'error': 'Only failed jobs can be retried.'}), 409
        if not os.path.exists(file_path(job_id, 'print.pdf')):
            return jsonify({'error': 'The document was already deleted. Refund this job instead.'}), 409
        with db() as con:
            cur = con.execute("UPDATE jobs SET attempt = attempt + 1 WHERE id = ? AND status = 'failed'",
                              (job_id,))
            if not cur.rowcount:
                return jsonify({'error': 'Only failed jobs can be retried.'}), 409
        set_status(job_id, 'queued')
        dispatch(job_id)
        return jsonify({'success': True})

    @app.post('/admin/api/jobs/<job_id>/refund')
    @admin_required()
    def admin_refund(job_id):
        """Refunds the money through Razorpay (live mode) and marks the job as refunded."""
        row = get_job(job_id)
        if not row or row['status'] != 'failed':
            return jsonify({'error': 'Only failed jobs can be refunded.'}), 409
        ok, message = refund_payment(row)
        if not ok:
            return jsonify({'error': message}), 502
        set_status(job_id, 'refunded', row['error'])
        remove_files(job_id)
        return jsonify({'success': True})

    # -------------------------------------------------------- sub-admins

    @app.get('/admin/api/admins')
    @admin_required(super_only=True)
    def admin_list():
        with db() as con:
            rows = con.execute('SELECT username, created_at FROM admins ORDER BY created_at').fetchall()
        return jsonify({'admins': [
            {'username': r['username'],
             'created': datetime.fromtimestamp(r['created_at']).strftime('%d %b %Y')} for r in rows]})

    @app.post('/admin/api/admins')
    @admin_required(super_only=True)
    def admin_add():
        data = request.get_json(silent=True) or {}
        username = str(data.get('username', '')).strip()
        password = str(data.get('password', ''))
        if not USERNAME_RE.match(username):
            return jsonify({'error': 'Username: 3-30 characters, letters, numbers, . _ - only.'}), 400
        if len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters.'}), 400
        if username.lower() == super_user.lower():
            return jsonify({'error': 'That username is reserved.'}), 409
        try:
            with db() as con:
                con.execute('INSERT INTO admins (username, password_hash, created_at) VALUES (?, ?, ?)',
                            (username, generate_password_hash(password), time.time()))
        except sqlite3.IntegrityError:
            return jsonify({'error': 'That username already exists.'}), 409
        return jsonify({'success': True})

    @app.delete('/admin/api/admins/<username>')
    @admin_required(super_only=True)
    def admin_delete(username):
        with db() as con:
            cur = con.execute('DELETE FROM admins WHERE username = ?', (username,))
        if not cur.rowcount:
            return jsonify({'error': 'Admin not found.'}), 404
        return jsonify({'success': True})
