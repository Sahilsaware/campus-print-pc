"""Campus Print PC agent (Windows).

Keeps one outbound WebSocket connection to the server. When a paid job is pushed,
it downloads the ready-to-print PDF, prints it on the default Windows printer with
SumatraPDF, waits for the Windows print queue to empty, and reports the result.

The server already applied paper size, orientation and pages-per-sheet, so this
script only handles copies, duplex and color.
"""
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler

import requests
import websocket  # pip package: websocket-client
import win32print
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

SERVER_URL = os.environ.get('SERVER_URL', '').rstrip('/')
AGENT_KEY = os.environ.get('AGENT_KEY', '')
PRINTER_NAME = os.environ.get('PRINTER_NAME', '').strip()  # empty = Windows default printer
SUMATRA = os.environ.get('SUMATRA_PATH', os.path.join(BASE_DIR, 'SumatraPDF.exe'))

STATE_FILE = os.path.join(BASE_DIR, 'agent_state.json')
WORK_DIR = os.path.join(tempfile.gettempdir(), 'campus_print_agent')
PING_EVERY = 10          # seconds
MAX_COPIES = 100
JOB_ID_RE = re.compile(r'^[0-9a-f]{32}$')

log = logging.getLogger('agent')


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s  %(levelname)s  %(message)s')
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logfile = RotatingFileHandler(os.path.join(BASE_DIR, 'agent.log'),
                                  maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    logfile.setFormatter(fmt)
    log.addHandler(console)
    log.addHandler(logfile)


class PrintError(Exception):
    """Message is safe to show to the student."""


# ------------------------------------------------------------ printer state

STATUS_PROBLEMS = {
    0x1: 'Printer is paused',
    0x2: 'Printer error',
    0x8: 'Paper jam',
    0x10: 'Out of paper',
    0x40: 'Paper problem',
    0x80: 'Printer is offline',
    0x1000: 'Printer not available',
    0x40000: 'Out of toner',
    0x100000: 'Printer needs attention',
    0x400000: 'Printer door is open',
}
ATTR_WORK_OFFLINE = 0x400


def target_printer():
    if PRINTER_NAME:
        return PRINTER_NAME
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        raise PrintError('No default printer is set on the print PC')


def printer_state():
    """Returns (ok, reason)."""
    try:
        name = target_printer()
        handle = win32print.OpenPrinter(name)
        try:
            info = win32print.GetPrinter(handle, 2)
        finally:
            win32print.ClosePrinter(handle)
        if info['Attributes'] & ATTR_WORK_OFFLINE:
            return False, 'Printer is set to offline'
        for flag, reason in STATUS_PROBLEMS.items():
            if info['Status'] & flag:
                return False, reason
        return True, ''
    except PrintError as exc:
        return False, str(exc)
    except Exception:
        return False, 'Printer not found'


def wait_for_spooler(name, hard_timeout=1800):
    """Wait until Windows has finished printing. Fails if the printer stays in an error state."""
    time.sleep(2)  # give the spooler a moment to register the job
    handle = win32print.OpenPrinter(name)
    started = time.time()
    bad_since = None
    try:
        while time.time() - started < hard_timeout:
            if not win32print.EnumJobs(handle, 0, 100, 1):
                return
            ok, reason = printer_state()
            if ok:
                bad_since = None
            else:
                bad_since = bad_since or time.time()
                if time.time() - bad_since > 60:
                    raise PrintError(reason)
            time.sleep(2)
    finally:
        win32print.ClosePrinter(handle)
    raise PrintError('The printer took too long to finish')


# ------------------------------------------------------------------ printing

def sumatra_command(job, path, name):
    paper = job.get('paper_size') if job.get('paper_size') in ('A4', 'A3') else 'A4'
    settings = ['color' if job.get('color_mode') == 'color' else 'monochrome']
    if job.get('duplex'):
        settings.append('duplexshort' if job.get('orientation') == 'landscape' else 'duplexlong')
    else:
        settings.append('simplex')
    settings += [f'paper={paper}', 'shrink']  # shrink only if a page is bigger than the printable area
    target = ['-print-to', name] if PRINTER_NAME else ['-print-to-default']
    return [SUMATRA, *target, '-print-settings', ','.join(settings), '-silent', path]


def print_job(job, path):
    name = target_printer()
    ok, reason = printer_state()
    if not ok:
        raise PrintError(reason)
    try:
        copies = max(1, min(int(job.get('copies', 1)), MAX_COPIES))
    except (TypeError, ValueError):
        copies = 1
    cmd = sumatra_command(job, path, name)
    log.info('Printing %s copies on "%s": %s', copies, name, ' '.join(cmd[1:-1]))
    # One print job per copy: works on old printers and keeps duplex copies separate.
    for _ in range(copies):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise PrintError('The print command timed out')
        if result.returncode != 0:
            log.error('SumatraPDF exit code %s: %s', result.returncode, result.stderr[:300])
            raise PrintError('Could not send the document to the printer')
    wait_for_spooler(name)


def download(job_id):
    url = f'{SERVER_URL}/agent/files/{job_id}'
    resp = requests.get(url, headers={'Authorization': f'Bearer {AGENT_KEY}'}, timeout=60)
    if resp.status_code != 200 or not resp.content.startswith(b'%PDF'):
        raise PrintError('Could not download the document')
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, f'{job_id}.pdf')
    with open(path, 'wb') as fh:
        fh.write(resp.content)
    return path


# -------------------------------------------------------- local job memory
# Remembers every job so a reconnect (or a repeated push) never prints twice.

state = {}          # job_id -> {'state': 'printing' | 'done' | 'failed', 'error': str}
state_lock = threading.Lock()
in_progress = set()  # job ids waiting in job_queue or being printed
job_queue = queue.Queue()


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as fh:
            state.update(json.load(fh))
    except (OSError, ValueError):
        pass
    for entry in state.values():  # crashed in the middle of a print: never re-print automatically
        if entry.get('state') == 'printing':
            entry.update(state='failed', error='The print PC restarted while printing')


def set_state(job_id, **entry):
    with state_lock:
        state[job_id] = entry
        while len(state) > 200:
            state.pop(next(iter(state)))
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as fh:
                json.dump(state, fh)
        except OSError:
            log.warning('Could not save %s', STATE_FILE)


# ------------------------------------------------------------- connection

conn = {'ws': None}
send_lock = threading.Lock()


def send(message):
    with send_lock:
        ws = conn['ws']
        if ws is None:
            return False
        try:
            ws.send(json.dumps(message))
            return True
        except Exception:
            return False


def report(job_id):
    entry = state.get(job_id)
    if entry:
        send({'type': 'status', 'job_id': job_id, 'state': entry['state'],
              'attempt': entry.get('attempt', 0), 'error': entry.get('error', '')})


def send_ping():
    ok, reason = printer_state()
    try:
        name = target_printer()
    except PrintError:
        name = ''
    send({'type': 'ping', 'printer': name, 'printer_ok': ok, 'reason': reason})


def on_job(job):
    job_id = str(job.get('id', ''))
    if not JOB_ID_RE.match(job_id):
        return
    try:
        attempt = int(job.get('attempt', 0))
    except (TypeError, ValueError):
        attempt = 0
    job['attempt'] = attempt
    with state_lock:
        if job_id in in_progress:
            return
        known = state.get(job_id)
        if known is not None and known.get('attempt', 0) >= attempt:
            duplicate = known          # already handled this attempt: never print twice
        else:
            duplicate = None           # new job, or a Retry with a higher attempt number
            in_progress.add(job_id)
    if duplicate is not None:
        if duplicate['state'] in ('done', 'failed'):
            report(job_id)
        return
    log.info('New job %s (attempt %s)', job_id, attempt)
    job_queue.put(job)


def worker():
    while True:
        job = job_queue.get()
        job_id = job['id']
        attempt = job['attempt']
        path = None
        try:
            set_state(job_id, state='printing', attempt=attempt)
            report(job_id)
            path = download(job_id)
            print_job(job, path)
            set_state(job_id, state='done', attempt=attempt)
            log.info('Job %s printed', job_id)
        except PrintError as exc:
            log.error('Job %s failed: %s', job_id, exc)
            set_state(job_id, state='failed', attempt=attempt, error=str(exc)[:200])
        except Exception:
            log.exception('Job %s crashed', job_id)
            set_state(job_id, state='failed', attempt=attempt, error='Print failed')
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            with state_lock:
                in_progress.discard(job_id)
        report(job_id)


def run():
    ws_url = SERVER_URL.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1) + '/ws/agent'
    delay = 2
    while True:
        try:
            ws = websocket.create_connection(
                ws_url, header=[f'Authorization: Bearer {AGENT_KEY}'], timeout=10)
        except Exception as exc:
            log.warning('Cannot connect to server (%s). Retrying in %ss', exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue

        delay = 2
        with send_lock:
            conn['ws'] = ws
        log.info('Connected to server')
        try:
            send_ping()
            for job_id in list(state):
                if state[job_id]['state'] in ('done', 'failed'):
                    report(job_id)  # tell the server about results it may have missed
            last_ping = time.time()
            while True:
                try:
                    raw = ws.recv()
                    if not raw:
                        raise ConnectionError('closed by server')
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        msg = {}
                    if msg.get('type') == 'job' and isinstance(msg.get('job'), dict):
                        on_job(msg['job'])
                except websocket.WebSocketTimeoutException:
                    pass
                if time.time() - last_ping >= PING_EVERY:
                    send_ping()
                    last_ping = time.time()
        except Exception as exc:
            log.warning('Disconnected: %s', exc)
        finally:
            with send_lock:
                conn['ws'] = None
            try:
                ws.close()
            except Exception:
                pass
        time.sleep(2)


def main():
    setup_logging()
    problems = []
    if not SERVER_URL.startswith(('http://', 'https://')):
        problems.append('SERVER_URL is missing in .env (example: https://print.yourdomain.com)')
    if len(AGENT_KEY) < 16:
        problems.append('AGENT_KEY is missing in .env (must match the server key)')
    if not os.path.exists(SUMATRA):
        problems.append(f'SumatraPDF.exe not found at {SUMATRA}')
    if problems:
        for p in problems:
            log.error(p)
        sys.exit(1)

    load_state()
    ok, reason = printer_state()
    log.info('Campus Print agent started. Printer: %s (%s)',
             PRINTER_NAME or 'Windows default', 'ready' if ok else reason)
    threading.Thread(target=worker, daemon=True).start()
    try:
        run()
    except KeyboardInterrupt:
        log.info('Stopped')


if __name__ == '__main__':
    main()
