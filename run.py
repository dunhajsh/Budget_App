"""Start the budget app:  python run.py   (then open http://127.0.0.1:5000)

Options:
  --port 5000     choose a port
  --lan           also listen on your home network so you can open it from your phone
                  (http://<this-computer's-IP>:5000) - handy for snapping receipt photos
  --no-browser    don't open a browser tab automatically
"""
import argparse
import threading
import webbrowser

from budget_app import create_app

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--lan", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    app = create_app()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  Budget app running at {url}")
    if args.lan:
        import socket

        try:
            ip = socket.gethostbyname(socket.gethostname())
            print(f"  On your phone (same Wi-Fi): http://{ip}:{args.port}")
        except OSError:
            pass
    print("  Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=args.port, debug=False, threaded=True)
