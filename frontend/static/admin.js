/* Campus Print: admin dashboard */
(() => {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const isSuper = !!$('admins-body');
    let busy = false;

    async function api(url, options = {}) {
        const headers = Object.assign({ 'X-CSRF-Token': csrf }, options.headers || {});
        const res = await fetch(url, Object.assign({}, options, { headers }));
        if (res.status === 401) { location.href = '/admin/login'; throw new Error('Logged out'); }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Something went wrong.');
        return data;
    }

    function message(text, isError) {
        const el = $('admin-msg');
        el.textContent = text || '';
        el.classList.toggle('error', !!isError);
    }

    function cell(text, className) {
        const td = document.createElement('td');
        if (className) td.className = className;
        td.textContent = text;
        return td;
    }

    function details(j) {
        const parts = [
            j.pages + (j.pages === 1 ? ' page' : ' pages'),
            j.copies + (j.copies === 1 ? ' copy' : ' copies'),
            j.color_mode === 'color' ? 'color' : 'B&W',
            j.paper_size,
            j.pages_per_sheet + '-up',
        ];
        if (j.duplex) parts.push('duplex');
        return parts.join(' \u00B7 ');
    }

    function badge(status) {
        const td = document.createElement('td');
        const span = document.createElement('span');
        span.className = 'badge ' + status;
        span.textContent = status;
        td.appendChild(span);
        return td;
    }

    function jobRow(j, withActions) {
        const tr = document.createElement('tr');
        tr.appendChild(cell(j.time));

        const file = cell(j.filename, 'file');
        file.title = j.filename;
        const id = document.createElement('div');
        id.className = 'sub';
        id.textContent = j.id.slice(0, 8) + (j.attempt ? ' \u00B7 try ' + (j.attempt + 1) : '');
        file.appendChild(id);
        tr.appendChild(file);

        const info = cell(details(j));
        if (j.error) {
            const e = document.createElement('div');
            e.className = 'sub';
            e.textContent = j.error;
            info.appendChild(e);
        }
        if (j.payment_id) {
            const p = document.createElement('div');
            p.className = 'sub';
            p.textContent = 'Payment: ' + j.payment_id;
            info.appendChild(p);
        }
        tr.appendChild(info);
        tr.appendChild(cell('\u20B9' + j.amount));
        tr.appendChild(badge(j.status));

        if (withActions) {
            const td = document.createElement('td');
            if (j.status === 'failed') {
                const box = document.createElement('div');
                box.className = 'row-actions';
                const retry = document.createElement('button');
                retry.className = 'btn';
                retry.textContent = 'Retry';
                retry.onclick = () => jobAction(j, 'retry');
                const refund = document.createElement('button');
                refund.className = 'btn danger';
                refund.textContent = 'Refund';
                refund.onclick = () => jobAction(j, 'refund');
                box.append(retry, refund);
                td.appendChild(box);
            }
            tr.appendChild(td);
        }
        return tr;
    }

    function fillTable(body, jobs, withActions, emptyText) {
        body.innerHTML = '';
        if (!jobs.length) {
            const tr = document.createElement('tr');
            const td = cell(emptyText, 'empty');
            td.colSpan = withActions ? 6 : 5;
            tr.appendChild(td);
            body.appendChild(tr);
            return;
        }
        jobs.forEach((j) => body.appendChild(jobRow(j, withActions)));
    }

    async function refresh() {
        if (busy) return;
        const month = $('month').value;
        try {
            const d = await api('/admin/api/summary' + (month ? '?month=' + encodeURIComponent(month) : ''));
            $('stat-prints').textContent = d.stats.prints;
            $('stat-pages').textContent = d.stats.pages;
            $('stat-earnings').textContent = '\u20B9' + d.stats.earnings;
            const pill = $('printer-pill');
            pill.textContent = d.printer.message + (d.printer.name ? ' \u00B7 ' + d.printer.name : '');
            pill.className = 'pill ' + (d.printer.online ? 'online' : 'offline');
            fillTable($('queue-body'), d.queue, true, 'No jobs waiting.');
            fillTable($('history-body'), d.history, false, 'Nothing printed in this period yet.');
        } catch (e) {
            if (e.message !== 'Logged out') message(e.message, true);
        }
    }

    async function jobAction(job, action) {
        const question = action === 'refund'
            ? 'Refund \u20B9' + job.amount + ' for "' + job.filename + '"? The file will be deleted.'
            : 'Retry printing "' + job.filename + '"?';
        if (!window.confirm(question)) return;
        busy = true;
        try {
            await api('/admin/api/jobs/' + job.id + '/' + action, { method: 'POST' });
            message(action === 'refund' ? 'Refund done.' : 'Job sent to the printer again.');
        } catch (e) {
            message(e.message, true);
        } finally {
            busy = false;
            refresh();
        }
    }

    /* ----------------------------------------------------------- sub-admins */

    async function loadAdmins() {
        if (!isSuper) return;
        try {
            const d = await api('/admin/api/admins');
            const body = $('admins-body');
            body.innerHTML = '';
            if (!d.admins.length) {
                const tr = document.createElement('tr');
                const td = cell('No sub-admins yet.', 'empty');
                td.colSpan = 3;
                tr.appendChild(td);
                body.appendChild(tr);
                return;
            }
            d.admins.forEach((a) => {
                const tr = document.createElement('tr');
                tr.appendChild(cell(a.username));
                tr.appendChild(cell(a.created));
                const td = document.createElement('td');
                const del = document.createElement('button');
                del.className = 'btn danger small-btn';
                del.textContent = 'Remove';
                del.onclick = () => removeAdmin(a.username);
                td.appendChild(del);
                tr.appendChild(td);
                body.appendChild(tr);
            });
        } catch (e) {
            message(e.message, true);
        }
    }

    async function addAdmin() {
        const username = $('new-username').value.trim();
        const password = $('new-password').value;
        try {
            await api('/admin/api/admins', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password }),
            });
            $('new-username').value = '';
            $('new-password').value = '';
            message('Sub-admin "' + username + '" added.');
            loadAdmins();
        } catch (e) {
            message(e.message, true);
        }
    }

    async function removeAdmin(username) {
        if (!window.confirm('Remove sub-admin "' + username + '"? They are logged out immediately.')) return;
        try {
            await api('/admin/api/admins/' + encodeURIComponent(username), { method: 'DELETE' });
            message('Sub-admin removed.');
            loadAdmins();
        } catch (e) {
            message(e.message, true);
        }
    }

    /* ----------------------------------------------------------------- init */

    $('month').addEventListener('change', refresh);
    $('month-clear').addEventListener('click', () => { $('month').value = ''; refresh(); });
    if (isSuper) $('add-admin').addEventListener('click', addAdmin);

    refresh();
    loadAdmins();
    setInterval(refresh, 10000);
})();
