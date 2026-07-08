#!/usr/bin/env python3
"""
MyMusicBox v2 -- start script

MyMusicBox is a fully client-side app now (IndexedDB + File System Access
API in the browser) -- there's no backend to install or run. This just
serves frontend/ over HTTP, since browsers only allow File System Access
API access from a secure context (http://localhost qualifies; a plain
file:// URL does not).
"""
import http.server
import os
import socketserver
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(ROOT, "frontend")
PORT = int(os.environ.get("PORT", 8765))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND, **kwargs)

    def log_message(self, format, *args):
        pass  # keep the terminal quiet


if __name__ == "__main__":
    print("-" * 48)
    print("  MyMusicBox v2")
    print("-" * 48)
    print(f"  >> Open http://localhost:{PORT} in your browser <<")
    print("  Press Ctrl+C to stop.")
    print("-" * 48)

    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass

    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
