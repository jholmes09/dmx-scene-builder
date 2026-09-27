"""Run DMX Scene Builder:  python3 -m scenebuilder [--port 8080] [--sim] [--data DIR]"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .node import ArtNetController, local_interfaces
from .server import App, make_server
from .store import Store

DEMO = Path(__file__).resolve().parent.parent / "data" / "demo_project.json"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scenebuilder")
    ap.add_argument("--port", type=int, default=8080, help="web port (default 8080)")
    ap.add_argument("--artnet-port", type=int, default=6454)
    ap.add_argument("--data", default=None, help="data folder (default ~/Library/Application Support/DMX Scene Builder)")
    ap.add_argument("--sim", action="store_true", help="start with the virtual E-Box running")
    ap.add_argument("--sim-port", type=int, default=6455, help="UDP port for the virtual E-Box")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--seed", default=os.environ.get("SCENEBUILDER_SEED"),
                    help="project file to load on first run (default: the demo floats)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    store = Store(Path(args.data) if args.data else None)
    seed = Path(args.seed) if args.seed else DEMO
    if not store.data["floats"] and seed.exists():
        store.replace_project(json.loads(seed.read_text()))
        store.flush()
        print("Loaded starting floats from %s." % seed.name)

    ctl = ArtNetController(port=args.artnet_port).start()
    app = App(store, ctl, sim_port=args.sim_port)
    if args.sim:
        app.sim_on()
    try:
        srv = make_server(app, port=args.port)
    except OSError as e:
        print("\nCould not open web port %d: %s\nIs DMX Scene Builder already running in another window?\n" % (args.port, e))
        return 1

    host = socket.gethostname()
    if not host.endswith(".local"):
        host = host.split(".")[0] + ".local"
    bar = "=" * 64
    print("\n" + bar)
    print("  DMX SCENE BUILDER %s  by Jeff Holmes Presents" % __version__)
    print(bar)
    print("  On this Mac:   http://localhost:%d" % args.port)
    print("  On the iPad:   http://%s:%d" % (host, args.port))
    for itf in local_interfaces():
        if not itf["ip"].startswith("127."):
            print("                 http://%s:%d   (%s)" % (itf["ip"], args.port, itf["name"]))
    if ctl.bind_error:
        print("\n  WARNING: " + ctl.bind_error)
    print("\n  Leave this window open. Close it (or press Ctrl+C) to quit.")
    print(bar + "\n", flush=True)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open("http://localhost:%d" % args.port)).start()

    def shutdown(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.engine.release()
        store.close()
        app.sim_off()
        ctl.stop()
        print("DMX Scene Builder stopped. Your floats are saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
