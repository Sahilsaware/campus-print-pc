/* Campus Print: customer page (5 steps: Home, Upload, Settings, Payment, Print) */
(() => {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const JOB_KEY = 'campus_print_job';
    const PDFJS_WORKER = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
    const TERMINAL = ['done', 'failed', 'refunded'];
    const MAX_PREVIEW_SHEETS = 30;

    const state = {
        config: null,
        fileId: null,
        filename: '',
        totalPages: 0,
        jobId: null,
        printerOnline: false,
        paying: false,
        ws: null,
        poll: null,
    };

    /* ------------------------------------------------------------ helpers */

    async function api(url, options = {}) {
        const res = await fetch(url, options);
        let data = null;
        if ((res.headers.get('content-type') || '').includes('application/json')) {
            data = await res.json().catch(() => null);
        }
        if (!res.ok) {
            const e = new Error((data && data.error) || 'Something went wrong. Please try again.');
            e.status = res.status;
            throw e;
        }
        return data;
    }

    const postJson = (url, body) => api(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });

    const rupees = (n) => '\u20B9' + n;

    function store(key, value) {
        try {
            if (value === null) localStorage.removeItem(key);
            else localStorage.setItem(key, value);
        } catch (e) { /* private mode: resume feature just won't work */ }
    }

    function load(key) {
        try { return localStorage.getItem(key); } catch (e) { return null; }
    }

    function showStep(n) {
        document.querySelectorAll('.step-content').forEach((s) =>
            s.classList.toggle('active', s.id === 'step-' + n));
        document.querySelectorAll('.step-node').forEach((node, i) =>
            node.classList.toggle('active', i < n));
        window.scrollTo({ top: 0, behavior: 'smooth' });
        updatePayState();
    }

    const currentStep = () => {
        const el = document.querySelector('.step-content.active');
        return el ? Number(el.id.replace('step-', '')) : 1;
    };

    function showAlert(title, message, icon) {
        $('alert-icon').textContent = icon || '\u26A0\uFE0F';
        $('alert-title').textContent = title;
        $('alert-message').textContent = message;
        $('alert-modal').classList.add('open');
        $('alert-close').focus();
    }

    /* ------------------------------------------------------- printer pill */

    async function refreshPrinter() {
        const pill = $('printer-pill');
        try {
            const d = await api('/api/printer-status');
            state.printerOnline = d.online;
            pill.textContent = d.message;
            pill.className = 'pill ' + (d.online ? 'online' : 'offline');
        } catch (e) {
            state.printerOnline = false;
            pill.textContent = 'Server unreachable';
            pill.className = 'pill offline';
        }
        updatePayState();
    }

    function updatePayState() {
        if (!$('pay-btn')) return;
        $('printer-warning').hidden = state.printerOnline;
        $('pay-btn').disabled = state.paying || !state.printerOnline;
    }

    /* ------------------------------------------------------------- upload */

    function setUploadMessage(text, isError) {
        const el = $('upload-status');
        el.textContent = text || '';
        el.classList.toggle('error', !!isError);
    }

    function resetDropZone() {
        $('drop-icon').textContent = '\u2601\uFE0F';
        $('drop-title').innerHTML = 'Drag &amp; Drop a file here or <span class="link">Browse</span>';
        $('drop-desc').textContent = 'PDF, JPG, PNG, Word, PowerPoint, Excel \u00B7 Max ' +
            (state.config ? state.config.max_upload_mb : 100) + 'MB';
    }

    function clearFile() {
        state.fileId = null;
        state.filename = '';
        state.totalPages = 0;
        $('to-settings').disabled = true;
        $('upload-progress').hidden = true;
        $('upload-bar').style.width = '0';
        $('file-input').value = '';
        setUploadMessage('');
        resetDropZone();
    }

    function uploadFile(file) {
        if (!file) return;
        const ext = '.' + file.name.split('.').pop().toLowerCase();
        const maxMb = state.config.max_upload_mb;
        if (!state.config.extensions.includes(ext)) {
            setUploadMessage('Supported formats: PDF, JPG, PNG, Word, PowerPoint, Excel.', true);
            return;
        }
        if (file.size > maxMb * 1024 * 1024) {
            setUploadMessage('This file is larger than ' + maxMb + 'MB.', true);
            return;
        }

        clearFile();
        $('upload-progress').hidden = false;
        setUploadMessage('Uploading\u2026');
        $('to-settings').disabled = true;

        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload');
        xhr.upload.onprogress = (e) => {
            if (!e.lengthComputable) return;
            const pct = Math.round((e.loaded / e.total) * 100);
            $('upload-bar').style.width = pct + '%';
            if (pct >= 100) setUploadMessage('Processing your file\u2026');
        };
        xhr.onerror = () => {
            $('upload-progress').hidden = true;
            setUploadMessage('Upload failed. Check your connection and try again.', true);
        };
        xhr.onload = () => {
            $('upload-progress').hidden = true;
            let data = null;
            try { data = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
            if (xhr.status !== 200 || !data) {
                const tooBig = xhr.status === 413;
                setUploadMessage(tooBig ? 'This file is larger than ' + maxMb + 'MB.'
                    : (data && data.error) || 'Upload failed. Please try again.', true);
                return;
            }
            state.fileId = data.file_id;
            state.filename = data.filename;
            state.totalPages = data.pages;
            $('drop-icon').textContent = '\u2705';
            $('drop-title').textContent = data.filename;
            $('drop-desc').textContent = data.pages + (data.pages === 1 ? ' page' : ' pages') +
                ' \u00B7 click to choose a different file';
            setUploadMessage('');
            $('to-settings').disabled = false;
            updateEstimate();
        };
        const form = new FormData();
        form.append('file', file);
        xhr.send(form);
    }

    function setupUpload() {
        const zone = $('drop-zone');
        ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => {
            e.preventDefault();
            zone.classList.add('dragging');
        }));
        ['dragleave', 'drop'].forEach((ev) => zone.addEventListener(ev, (e) => {
            e.preventDefault();
            zone.classList.remove('dragging');
        }));
        zone.addEventListener('drop', (e) => uploadFile(e.dataTransfer.files[0]));
        $('file-input').addEventListener('change', (e) => uploadFile(e.target.files[0]));
    }

    /* ----------------------------------------------------------- settings */

    function readSettings() {
        const pps = Number($('pages-per-sheet').value);
        return {
            color_mode: $('color-mode').value,
            paper_size: $('paper-size').value,
            orientation: $('orientation').value,
            pages_per_sheet: pps,
            fit: pps > 1 ? 'fit' : $('fit').value,
            duplex: $('duplex').value === '1',
            copies: Number($('copies').value),
            page_range: $('page-range').value.trim(),
        };
    }

    /* same rules as the server: "1-3,5" -> pages; empty = all pages */
    function countPages(text, total) {
        if (!text) return total;
        const pages = new Set();
        for (const raw of text.split(',')) {
            const part = raw.trim();
            if (!part) continue;
            const m = /^(\d+)\s*(?:-\s*(\d+))?$/.exec(part);
            if (!m) throw new Error('Invalid page range: "' + part + '"');
            const a = parseInt(m[1], 10);
            const b = m[2] === undefined ? a : parseInt(m[2], 10);
            if (a < 1 || b < a || b > total) {
                throw new Error('Page range "' + part + '" must be within 1-' + total);
            }
            for (let p = a; p <= b; p++) pages.add(p);
        }
        if (!pages.size) throw new Error('No pages selected');
        return pages.size;
    }

    function estimate() {
        const s = readSettings();
        if (!state.fileId) return { ok: false, error: 'Please upload a file first.' };
        if (!Number.isInteger(s.copies) || s.copies < 1 || s.copies > state.config.max_copies) {
            return { ok: false, error: 'Copies must be between 1 and ' + state.config.max_copies + '.' };
        }
        let pages;
        try { pages = countPages(s.page_range, state.totalPages); }
        catch (e) { return { ok: false, error: e.message }; }
        const sides = Math.ceil(pages / s.pages_per_sheet);
        const rate = state.config.rates[s.color_mode];
        return { ok: true, pages, sides, rate, copies: s.copies, total: sides * s.copies * rate };
    }

    function updateEstimate() {
        const box = $('estimate');
        const fit = $('fit');
        const multi = Number($('pages-per-sheet').value) > 1;
        if (multi) fit.value = 'fit';
        fit.disabled = multi;

        const e = estimate();
        box.classList.toggle('error', !e.ok);
        box.innerHTML = '';
        if (!e.ok) {
            box.textContent = e.error;
            return;
        }
        const strong = (t) => { const el = document.createElement('strong'); el.textContent = t; return el; };
        box.append(strong(e.pages + (e.pages === 1 ? ' page' : ' pages')),
            ' \u2192 ', strong(e.sides + (e.sides === 1 ? ' side' : ' sides')),
            ' \u00D7 ' + e.copies + (e.copies === 1 ? ' copy' : ' copies') + ' \u00D7 ' + rupees(e.rate) + ' = ',
            strong(rupees(e.total)));
    }

    /* ------------------------------------------------------------ preview */

    function closePreview() {
        $('preview-modal').classList.remove('open');
        $('preview-pages').innerHTML = '';
    }

    async function openPreview() {
        const e = estimate();
        if (!e.ok) { showAlert('Check your settings', e.error); return; }
        const btn = $('preview-btn');
        const label = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Preparing preview\u2026';
        try {
            const settings = readSettings();
            const res = await fetch('/api/files/' + state.fileId + '/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(settings),
            });
            if (!res.ok) {
                const data = await res.json().catch(() => null);
                throw new Error((data && data.error) || 'Could not create the preview.');
            }
            const buffer = await res.arrayBuffer();

            if (!window.pdfjsLib) { // pdf.js could not load: let the browser show the PDF instead
                window.open(URL.createObjectURL(new Blob([buffer], { type: 'application/pdf' })), '_blank');
                return;
            }
            window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
            const pdf = await window.pdfjsLib.getDocument({ data: buffer }).promise;

            $('preview-file').textContent = state.filename;
            $('preview-sub').textContent = [
                settings.paper_size, settings.orientation,
                settings.pages_per_sheet + ' per sheet',
                settings.color_mode === 'color' ? 'color' : 'black & white',
                settings.duplex ? 'both sides' : 'one side',
                settings.copies + (settings.copies === 1 ? ' copy' : ' copies'),
            ].join(' \u00B7 ');
            const holder = $('preview-pages');
            holder.innerHTML = '';
            $('preview-modal').classList.add('open');

            const shown = Math.min(pdf.numPages, MAX_PREVIEW_SHEETS);
            for (let i = 1; i <= shown; i++) {
                const page = await pdf.getPage(i);
                const base = page.getViewport({ scale: 1 });
                const viewport = page.getViewport({ scale: (560 * (window.devicePixelRatio || 1)) / base.width });
                const canvas = document.createElement('canvas');
                canvas.className = 'preview-page' + (settings.color_mode === 'bw' ? ' bw-filter' : '');
                canvas.width = viewport.width;
                canvas.height = viewport.height;
                holder.appendChild(canvas);
                await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
            }
            if (pdf.numPages > shown) {
                const note = document.createElement('div');
                note.textContent = 'Showing the first ' + shown + ' of ' + pdf.numPages + ' sheets.';
                holder.appendChild(note);
            }
        } catch (err) {
            showAlert('Preview not available', err.message);
        } finally {
            btn.disabled = false;
            btn.textContent = label;
        }
    }

    /* ----------------------------------------------------------- payment */

    async function goToPayment() {
        const e = estimate();
        if (!e.ok) { showAlert('Check your settings', e.error); return; }
        const btn = $('to-payment');
        btn.disabled = true;
        try {
            const order = await postJson('/api/files/' + state.fileId + '/order', readSettings());
            state.jobId = order.job_id;
            state.printerOnline = order.printer_online;
            $('sum-file').textContent = state.filename;
            $('sum-pages').textContent = state.totalPages;
            $('sum-selected').textContent = order.pages_selected;
            $('sum-sheets').textContent = order.sheets;
            $('sum-copies').textContent = order.copies;
            $('sum-total').textContent = rupees(order.total_price);
            $('test-note').hidden = !state.config.payment_test_mode;
            showStep(4);
            refreshPrinter();
        } catch (err) {
            showAlert('Could not continue', err.message);
        } finally {
            btn.disabled = false;
        }
    }

    function loadRazorpay() {
        return new Promise((resolve, reject) => {
            if (window.Razorpay) { resolve(); return; }
            const s = document.createElement('script');
            s.src = 'https://checkout.razorpay.com/v1/checkout.js';
            s.onload = resolve;
            s.onerror = () => reject(new Error('Could not load the payment window. Check your internet connection.'));
            document.head.appendChild(s);
        });
    }

    async function verifyPayment(jobId, response) {
        try {
            await postJson('/api/jobs/' + jobId + '/pay', response);
            startTracking(jobId);
        } catch (err) {
            showAlert('Payment needs a check',
                err.message + ' If money was deducted, show this payment ID to the print desk: ' +
                (response.razorpay_payment_id || 'n/a'));
        } finally {
            state.paying = false;
            updatePayState();
        }
    }

    async function pay() {
        if (state.paying || !state.jobId) return;
        state.paying = true;
        updatePayState();
        const jobId = state.jobId;
        try {
            if (state.config.payment_test_mode) {
                await postJson('/api/jobs/' + jobId + '/pay', {});
                startTracking(jobId);
                state.paying = false;
                updatePayState();
                return;
            }
            const order = await postJson('/api/jobs/' + jobId + '/create-payment', {});
            await loadRazorpay();
            const checkout = new window.Razorpay({
                key: order.key_id,
                amount: order.amount,
                currency: order.currency,
                name: order.name,
                description: order.description,
                order_id: order.order_id,
                theme: { color: '#0284c7' },
                handler: (response) => verifyPayment(jobId, response),
                modal: { ondismiss: () => { state.paying = false; updatePayState(); } },
            });
            checkout.on('payment.failed', () => {
                showAlert('Payment failed', 'The payment was not completed. You can try again.');
                state.paying = false;
                updatePayState();
            });
            checkout.open();
        } catch (err) {
            state.paying = false;
            updatePayState();
            showAlert('Payment problem', err.message);
        }
    }

    /* ------------------------------------------------------ print status */

    const STATUS_TEXT = {
        queued: ['In queue', 'Your document is waiting for the printer.'],
        sent: ['Sent to printer', 'The print PC has received your document.'],
        printing: ['Printing\u2026', 'Your document is being printed. Please wait near the printer.'],
        done: ['Printed \u2705', 'Collect your pages from the printer tray. Your file has been deleted from our server.'],
        failed: ['Print failed', 'Something went wrong while printing.'],
        refunded: ['Refunded', 'This job could not be printed and your payment has been refunded.'],
    };

    function applyJob(job) {
        const text = STATUS_TEXT[job.status];
        if (!text) return;
        const done = TERMINAL.includes(job.status);
        $('job-title').textContent = text[0];
        $('job-title').className = 'job-title' +
            (job.status === 'done' ? ' good' : (job.status === 'failed' || job.status === 'refunded') ? ' bad' : '');
        let detail = text[1];
        if (job.status === 'failed') {
            detail = (job.error || detail) + ' Please tell the print desk your Job ID below. You will be refunded.';
        }
        $('job-detail').textContent = detail;
        $('job-id').textContent = job.id;
        $('job-spinner').classList.toggle('stopped', done);
        $('reset-btn').hidden = !done;
        if (done) {
            stopTracking();
            store(JOB_KEY, null);
        }
    }

    function stopTracking() {
        if (state.ws) {
            state.ws.onclose = null;
            try { state.ws.close(); } catch (e) { /* already closed */ }
            state.ws = null;
        }
        if (state.poll) { clearInterval(state.poll); state.poll = null; }
    }

    function startPolling(jobId) {
        if (state.poll) return;
        state.poll = setInterval(async () => {
            try { applyJob(await api('/api/jobs/' + jobId)); } catch (e) { /* retry next tick */ }
        }, 4000);
    }

    function startTracking(jobId) {
        stopTracking();
        state.jobId = jobId;
        store(JOB_KEY, jobId);
        $('job-id').textContent = jobId;
        applyJob({ id: jobId, status: 'queued' });
        showStep(5);

        try {
            const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
            const ws = new WebSocket(proto + location.host + '/ws/job/' + jobId);
            state.ws = ws;
            ws.onmessage = (ev) => {
                let msg = null;
                try { msg = JSON.parse(ev.data); } catch (e) { return; }
                if (msg && msg.status) applyJob(msg);
            };
            ws.onclose = () => startPolling(jobId);  // fall back to polling if the socket drops
            ws.onerror = () => startPolling(jobId);
        } catch (e) {
            startPolling(jobId);
        }
    }

    function resetAll() {
        stopTracking();
        store(JOB_KEY, null);
        state.jobId = null;
        clearFile();
        $('color-mode').selectedIndex = 0;
        $('paper-size').value = 'A4';
        $('orientation').value = 'portrait';
        $('pages-per-sheet').selectedIndex = 0;
        $('fit').value = 'fit';
        $('duplex').value = '0';
        $('copies').value = 1;
        $('page-range').value = '';
        updateEstimate();
        showStep(2);
    }

    async function resumeJob() {
        const id = load(JOB_KEY);
        if (!id) return;
        try {
            const job = await api('/api/jobs/' + id);
            if (TERMINAL.includes(job.status)) { store(JOB_KEY, null); return; }
            if (job.status === 'draft' || job.status === 'awaiting_payment') { store(JOB_KEY, null); return; }
            startTracking(id);
            applyJob(job);
        } catch (e) {
            if (e.status === 404) store(JOB_KEY, null);
        }
    }

    /* --------------------------------------------------------------- init */

    function fillSettingsOptions() {
        const cfg = state.config;
        const color = $('color-mode');
        color.innerHTML = '';
        [['bw', 'Black & White (' + rupees(cfg.rates.bw) + ' per side)'],
         ['color', 'Color (' + rupees(cfg.rates.color) + ' per side)']].forEach(([value, label]) => {
            const o = document.createElement('option');
            o.value = value;
            o.textContent = label;
            color.appendChild(o);
        });
        const pps = $('pages-per-sheet');
        pps.innerHTML = '';
        cfg.pages_per_sheet.forEach((n) => {
            const o = document.createElement('option');
            o.value = String(n);
            o.textContent = n === 1 ? '1 page per sheet' : n + ' pages per sheet';
            pps.appendChild(o);
        });
        $('copies').max = cfg.max_copies;
        $('price-bw').textContent = rupees(cfg.rates.bw);
        $('price-color').textContent = rupees(cfg.rates.color);
        resetDropZone();
    }

    function bindEvents() {
        document.querySelectorAll('[data-goto]').forEach((btn) =>
            btn.addEventListener('click', () => showStep(Number(btn.dataset.goto))));
        $('to-settings').addEventListener('click', () => { updateEstimate(); showStep(3); });
        $('to-payment').addEventListener('click', goToPayment);
        $('preview-btn').addEventListener('click', openPreview);
        $('pay-btn').addEventListener('click', pay);
        $('reset-btn').addEventListener('click', resetAll);
        $('alert-close').addEventListener('click', () => $('alert-modal').classList.remove('open'));
        $('preview-close').addEventListener('click', closePreview);
        $('preview-modal').addEventListener('click', (e) => { if (e.target === $('preview-modal')) closePreview(); });
        $('alert-modal').addEventListener('click', (e) => {
            if (e.target === $('alert-modal')) $('alert-modal').classList.remove('open');
        });
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Escape') return;
            closePreview();
            $('alert-modal').classList.remove('open');
        });
        ['color-mode', 'paper-size', 'orientation', 'pages-per-sheet', 'fit', 'duplex']
            .forEach((id) => $(id).addEventListener('change', updateEstimate));
        ['copies', 'page-range'].forEach((id) => $(id).addEventListener('input', updateEstimate));
    }

    async function init() {
        bindEvents();
        setupUpload();
        try {
            state.config = await api('/api/config');
        } catch (e) {
            showAlert('Server not reachable', 'Please refresh the page in a moment.');
            return;
        }
        fillSettingsOptions();
        updateEstimate();
        refreshPrinter();
        setInterval(refreshPrinter, 10000);
        resumeJob();
    }

    init();
})();
