# Campus Print

Students upload a file on a website, choose print settings, pay, and the document prints on a
campus printer. No login for students.

```
Student browser  ──►  backend (Flask server)  ──WebSocket──►  agent (Windows print PC)  ──►  printer
      frontend served by the backend                          uses SumatraPDF
```

## Folder layout

```
CampusPrint/
├── README.md
├── backend/                 Server (Python / Flask)
│   ├── app.py               Main server: upload, price, payment, queue, WebSockets
│   ├── admin.py             Admin panel logic (login, dashboard, retry/refund, sub-admins)
│   ├── processing.py        Converts files to PDF, page ranges, paper size / pages-per-sheet layout
│   ├── make_keys.py         Prints random secrets for the .env files
│   ├── requirements.txt
│   └── .env.example         Copy to .env and fill in
├── frontend/                Everything the browser shows (served by the backend)
│   ├── templates/           index.html, privacy.html, admin.html, admin_login.html
│   └── static/              style.css, app.js, admin.css, admin.js
├── agent/                   Runs on the Windows PC that is connected to the printer
│   ├── agent.py
│   ├── start_agent.bat      Starts the agent and restarts it if it stops
│   ├── requirements.txt
│   └── .env.example         Copy to .env and fill in
└── deploy/                  Optional: nginx + systemd files for a Linux server
```

## 1. Backend (server)

Needs Python 3.10+. For Word / PowerPoint / Excel files the server also needs **LibreOffice**
(`sudo apt install libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress`
on Ubuntu). PDF and images work without it.

```bash
cd backend
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
python make_keys.py                 # prints AGENT_KEY and SECRET_KEY
cp .env.example .env                # Windows: copy .env.example .env
```

Open `backend/.env` and fill in `AGENT_KEY`, `SECRET_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD`.
Then start it:

```bash
python app.py                                             # local testing -> http://127.0.0.1:5000
gunicorn -w 1 --threads 100 -b 127.0.0.1:5000 app:app     # real server (Linux). Keep -w 1 !
```

Pages: `/` (students), `/admin` (admin), `/privacy`.
Uploaded files and the database live in `backend/data/` (created automatically, never commit it).

## 2. Agent (print PC, Windows)

1. Install Python 3.10+ on the print PC.
2. Download **SumatraPDF** (portable) and put `SumatraPDF.exe` inside the `agent` folder.
3. In the `agent` folder: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and set:
   - `SERVER_URL` = your server address (local test: `http://127.0.0.1:5000`)
   - `AGENT_KEY`  = **exactly the same** value as in `backend/.env`
   - `PRINTER_NAME` = leave empty to use the Windows default printer
5. Double-click `start_agent.bat`. The site's printer pill turns green ("Printer is ready").

## 3. Payments

- `PAYMENT_TEST_MODE=1` (default): nobody pays, orders are accepted directly. **Testing only.**
- Going live: create a Razorpay account, then in `backend/.env` set `PAYMENT_TEST_MODE=0`,
  `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`. The server then creates the Razorpay order itself,
  verifies the payment signature, and the admin **Refund** button refunds through Razorpay.
  Use Razorpay *test keys* first to try the whole flow.

## 4. Going online (optional)

`deploy/nginx.conf` and `deploy/campus-print.service` are ready-made examples. Behind nginx
or Cloudflare over HTTPS set `BEHIND_PROXY=1` and `COOKIE_SECURE=1` in `backend/.env`.

## What was fixed compared to the original files

- **Retry works**: the agent used to remember a failed job forever and never printed it again.
  Jobs now carry an attempt number; Retry raises it and the agent prints again. Old results from a
  previous attempt are ignored.
- **Real payment verification** (Razorpay order + signature check + refund) instead of a TODO.
  Test mode still exists but prints a warning at startup.
- **Admin panel connected** to the server (it was never registered), with the missing
  `attempt` column, `SECRET_KEY`, `printer_summary` and admin pages added.
- **Missing files written**: `app.js`, `admin.html`, `admin_login.html`, `admin.css`, `admin.js`,
  `privacy.html`.
- Older `campus.db` files are upgraded automatically (new columns are added).
- Extra safety: upload rate limit, security headers, session cookie settings, real visitor IP
  behind a proxy, and a clear error if the layout step fails.

## Limits to know

- The server keeps live state in memory, so it must run with **one worker** (`-w 1`). One worker
  with 100 threads is plenty for a single campus printer.
- One print PC (one agent) at a time.
