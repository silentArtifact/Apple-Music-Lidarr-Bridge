#!/usr/bin/env python3
"""Mint an Apple Music **Music User Token** (MUT) on your Mac.

Apple only hands out a user token through an interactive Apple ID sign-in in a
browser. This script signs a short-lived developer token from your MusicKit
private key, serves a tiny page on http://localhost that runs Apple's MusicKit
JS, and captures the user token your browser returns. Copy that token to
Shard at /docker/applemusic-lidarr/data/mut.txt.

Usage (on the Mac, with the .p8 you downloaded from the Apple Developer portal):

    pip3 install 'pyjwt[crypto]'
    python3 mint_mut.py --p8 ~/Downloads/AuthKey_ABC123DEF4.p8 \
        --key-id ABC123DEF4 --team-id YOURTEAMID

It opens your browser; click "Sign in", authorize, and the token prints here
and is written to ./mut.txt.
"""

import argparse
import http.server
import json
import os
import sys
import threading
import time
import webbrowser

try:
    import jwt  # PyJWT with the cryptography extra
except ImportError:
    sys.exit("Missing dependency. Run:  pip3 install 'pyjwt[crypto]'")

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Apple Music sign-in</title>
<script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js" data-web-components async></script>
<style>body{font-family:-apple-system,system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}
button{font-size:1.1rem;padding:.6rem 1.2rem}pre{white-space:pre-wrap;word-break:break-all;background:#f4f4f4;padding:1rem;border-radius:6px}</style>
</head><body>
<h2>Apple Music &rarr; Lidarr: sign in</h2>
<p id="status">Loading MusicKit&hellip;</p>
<button id="go" disabled>Sign in to Apple Music</button>
<pre id="out"></pre>
<script>
const DEV_TOKEN = "__DEV_TOKEN__";
document.addEventListener('musickitloaded', async () => {
  const status = document.getElementById('status');
  const go = document.getElementById('go');
  try {
    await MusicKit.configure({ developerToken: DEV_TOKEN,
      app: { name: 'AppleMusic Lidarr Bridge', build: '1.0' } });
  } catch (e) { status.textContent = 'MusicKit configure failed: ' + e; return; }
  const music = MusicKit.getInstance();
  status.textContent = 'Ready. Click below and sign in with your Apple ID.';
  go.disabled = false;
  go.onclick = async () => {
    try {
      status.textContent = 'Authorizing…';
      const mut = await music.authorize();
      document.getElementById('out').textContent = mut;
      await fetch('/mut', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: mut }) });
      status.textContent = 'Success! Token captured. You can close this tab.';
    } catch (e) { status.textContent = 'Authorization failed: ' + e; }
  };
});
</script>
</body></html>"""


def sign_developer_token(p8_path, key_id, team_id):
    with open(os.path.expanduser(p8_path)) as fh:
        private_key = fh.read()
    now = int(time.time())
    return jwt.encode(
        {"iss": team_id, "iat": now, "exp": now + 3600},
        private_key,
        algorithm="ES256",
        headers={"alg": "ES256", "kid": key_id},
    )


def main():
    ap = argparse.ArgumentParser(description="Mint an Apple Music user token")
    ap.add_argument("--p8", required=True, help="path to AuthKey_XXXX.p8")
    ap.add_argument("--key-id", required=True, help="MusicKit Key ID (10 chars)")
    ap.add_argument("--team-id", required=True, help="Apple Developer Team ID (10 chars)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", default="mut.txt")
    args = ap.parse_args()

    dev_token = sign_developer_token(args.p8, args.key_id, args.team_id)
    html = PAGE.replace("__DEV_TOKEN__", dev_token)
    captured = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                token = json.loads(body)["token"]
            except (ValueError, KeyError):
                self.send_response(400)
                self.end_headers()
                return
            with open(args.out, "w") as fh:
                fh.write(token)
            print("\n=== Music User Token captured ===")
            print(token)
            print(f"\nSaved to {os.path.abspath(args.out)}")
            print("Copy it to Shard:  /docker/applemusic-lidarr/data/mut.txt")
            self.send_response(200)
            self.end_headers()
            captured.set()

    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://localhost:{args.port}/"
    print(f"Serving sign-in page at {url}")
    print("Opening your browser; if it doesn't open, paste that URL in yourself.")
    webbrowser.open(url)

    try:
        while not captured.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\nCancelled.")
        return
    time.sleep(1)
    server.shutdown()


if __name__ == "__main__":
    main()
