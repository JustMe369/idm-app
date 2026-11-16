"""Windows launcher for the IDM-like download manager.

Starts the server, prints the URL, and opens the browser.
Run the built .exe (or `python launcher.py`) and use the web UI.
"""
import os
import socket
import threading
import webbrowser

import uvicorn

from app import app


def free_port(start=8090):
    for p in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    return start


def main():
    port = free_port(8090)
    url = "http://127.0.0.1:%d" % port
    print("=" * 56)
    print("  IDM-Like Download Manager")
    print("  Open this address in your browser:")
    print("  " + url)
    print("  Close this window (or press Ctrl+C) to stop.")
    print("=" * 56)
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
