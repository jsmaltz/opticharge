#!/usr/bin/env python3
"""Read-only intranet web viewer for the OptiCharge systemd journal."""

import argparse
import ipaddress
import json
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


DEFAULT_UNIT = "opticharge.service"
DEFAULT_LINES = 400
MAX_LINES = 2000


PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>OptiCharge Log</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1220; --panel:#111b2e; --line:#24324a;
      --text:#dbe7f7; --muted:#8da2bd; --good:#4ade80; --bad:#fb7185; --accent:#38bdf8; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,sans-serif; }
    header { padding:18px 22px; border-bottom:1px solid var(--line); background:var(--panel); }
    h1 { margin:0 0 12px; font-size:22px; }
    .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
    input,select,button { border:1px solid var(--line); border-radius:7px; background:#0b1425;
      color:var(--text); padding:8px 10px; }
    input { flex:1 1 260px; min-width:180px; }
    button { cursor:pointer; }
    #status { margin-left:auto; color:var(--muted); }
    #status.active { color:var(--good); } #status.failed { color:var(--bad); }
    main { padding:14px; }
    pre { margin:0; min-height:70vh; max-height:calc(100vh - 125px); overflow:auto;
      white-space:pre-wrap; overflow-wrap:anywhere; padding:16px; border:1px solid var(--line);
      border-radius:9px; background:#060b14; font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace; }
    .muted { color:var(--muted); }
  </style>
</head>
<body>
  <header>
    <h1>OptiCharge log</h1>
    <div class="toolbar">
      <input id="filter" type="search" placeholder="Filter visible lines">
      <select id="lines" aria-label="Number of lines">
        <option>200</option><option selected>400</option><option>800</option><option>1500</option>
      </select>
      <button id="pause">Pause</button>
      <button id="refresh">Refresh</button>
      <span id="status">Loading…</span>
    </div>
  </header>
  <main><pre id="log" aria-live="polite">Loading…</pre></main>
  <script>
    const log = document.querySelector('#log'), filter = document.querySelector('#filter');
    const status = document.querySelector('#status'), pause = document.querySelector('#pause');
    let raw = '', paused = false;
    function render() {
      const needle = filter.value.toLowerCase();
      log.textContent = needle ? raw.split('\n').filter(x => x.toLowerCase().includes(needle)).join('\n') : raw;
    }
    async function refresh() {
      if (paused) return;
      try {
        const count = document.querySelector('#lines').value;
        const response = await fetch(`/api/logs?lines=${count}`, {cache:'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
        raw = data.logs; render();
        status.textContent = `${data.service_status} · ${new Date(data.generated_at).toLocaleTimeString()}`;
        status.className = data.service_status === 'active' ? 'active' : 'failed';
        if (nearBottom) log.scrollTop = log.scrollHeight;
      } catch (error) { status.textContent = `viewer error: ${error.message}`; status.className='failed'; }
    }
    filter.addEventListener('input', render);
    document.querySelector('#lines').addEventListener('change', refresh);
    document.querySelector('#refresh').addEventListener('click', refresh);
    pause.addEventListener('click', () => { paused=!paused; pause.textContent=paused?'Resume':'Pause'; if(!paused) refresh(); });
    refresh(); setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


def client_is_private(address: str) -> bool:
    """Allow only loopback, private, and link-local source addresses."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def bounded_line_count(raw_value) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_LINES
    return max(1, min(MAX_LINES, value))


def read_journal(unit: str, lines: int) -> str:
    result = subprocess.run(
        [
            "journalctl", "-u", unit, "-n", str(lines), "--no-pager",
            "-o", "short-iso",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "journalctl failed")
    return result.stdout


def service_status(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def make_handler(unit: str):
    class LogHandler(BaseHTTPRequestHandler):
        server_version = "OptiChargeLog/1"

        def _headers(self, status, content_type, content_length):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()

        def _write(self, status, content_type, body):
            encoded = body.encode("utf-8")
            self._headers(status, content_type, len(encoded))
            self.wfile.write(encoded)

        def do_GET(self):
            if not client_is_private(self.client_address[0]):
                self._write(403, "text/plain; charset=utf-8", "Forbidden\n")
                return

            request = urlparse(self.path)
            if request.path == "/":
                self._write(200, "text/html; charset=utf-8", PAGE)
                return
            if request.path == "/healthz":
                self._write(200, "text/plain; charset=utf-8", "ok\n")
                return
            if request.path == "/api/logs":
                query = parse_qs(request.query)
                lines = bounded_line_count(query.get("lines", [DEFAULT_LINES])[0])
                try:
                    payload = {
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "service_status": service_status(unit),
                        "logs": read_journal(unit, lines),
                    }
                    self._write(200, "application/json; charset=utf-8", json.dumps(payload))
                except Exception as exc:
                    self._write(
                        500,
                        "application/json; charset=utf-8",
                        json.dumps({"error": str(exc)}),
                    )
                return
            self._write(404, "text/plain; charset=utf-8", "Not found\n")

        def log_message(self, format, *args):
            return

    return LogHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.unit))
    print(f"OptiCharge log viewer listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
