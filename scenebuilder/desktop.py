"""Double-click desktop app: the same server as `python -m scenebuilder`, started in-process,
with a small window (address for the iPad, Open in browser, Quit) instead of a terminal.

    python -m scenebuilder.desktop [--port 8080] [--selftest]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .node import ArtNetController, local_interfaces
from .paths import resource_root
from .server import App, make_server
from .store import DEFAULT_DIR, Store

BG, GOLD, CHAMPAGNE, MUTED, WARN = "#070605", "#D4A84A", "#F6E3AE", "#9C8F6B", "#E8B04A"
TITLE = "DMX Scene Builder"


class Backend:
    """The server, store and Art-Net controller, started and stopped in-process."""

    def __init__(self, port, artnet_port, data_dir, seed):
        self.port = port
        self.store = Store(Path(data_dir) if data_dir else None)
        seed = Path(seed) if seed else resource_root() / "data" / "demo_project.json"
        if not self.store.data["floats"] and seed.exists():
            self.store.replace_project(json.loads(seed.read_text()))
            self.store.flush()
        self.ctl = ArtNetController(port=artnet_port).start()
        preferred_ip = self.store.settings.get("network_interface")
        self.ctl.set_preferred_interface(preferred_ip)
        self.app = App(self.store, self.ctl)
        try:
            self.srv = make_server(self.app, port=port)
        except BaseException:
            self.stop()
            raise
        self.port = self.srv.server_address[1]
        self.error = None  # set if the server loop dies unexpectedly
        self.thread = threading.Thread(target=self._serve, name="web", daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            self.srv.serve_forever()
        except BaseException as e:  # pragma: no cover
            logging.getLogger("scenebuilder.desktop").exception("web server stopped")
            self.error = "%s: %s" % (type(e).__name__, e)

    def is_live(self) -> bool:
        st = self.app.engine.status()
        return bool(st.get("active_float")) and bool(st.get("output"))

    def stop(self):
        """Stop serving, release the lights, flush the store. Safe to call twice."""
        srv = getattr(self, "srv", None)
        if srv is not None:
            if self.thread.is_alive():
                srv.shutdown()
            srv.server_close()
            self.srv = None
        for step in (self.app.engine.release if hasattr(self, "app") else None,
                     self.store.close, self.app.sim_off if hasattr(self, "app") else None, self.ctl.stop):
            if step is None:
                continue
            try:
                step()
            except Exception:
                logging.getLogger("scenebuilder.desktop").exception("shutdown step failed")


def addresses(port):
    host = socket.gethostname()
    if not host.endswith(".local"):
        host = host.split(".")[0] + ".local"
    out = ["http://%s:%d" % (host, port)]
    for itf in local_interfaces():
        if not itf["ip"].startswith("127."):
            out.append("http://%s:%d" % (itf["ip"], port))
    return out


def is_scenebuilder(port) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=2) as r:
            return "engine" in json.loads(r.read().decode())
    except Exception:
        return False


def setup_logging(data_dir, to_file=True):
    handlers = []
    try:
        if not to_file:
            raise OSError

        d = Path(data_dir) if data_dir else DEFAULT_DIR
        d.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(d / "desktop.log", encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers or [logging.NullHandler()],
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S", force=True)


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read()


def selftest(args) -> int:
    """Start everything on a free port with a throwaway data folder, fetch the pages, exit 0/1.
    The result is also written to --selftest-out because the windowed Windows exe has no console."""
    lines, code = [], 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            be = Backend(0, args.artnet_port, tmp, args.seed)
            try:
                base = "http://127.0.0.1:%d" % be.port
                st, body = get(base + "/api/status")
                assert st == 200 and "engine" in json.loads(body), "/api/status"
                lines.append("status ok")
                st, body = get(base + "/index.html")
                assert st == 200 and b"<html" in body.lower(), "/index.html"
                lines.append("index.html ok (%d bytes)" % len(body))
                st, body = get(base + "/api/state")
                floats = json.loads(body)["project"]["floats"]
                assert floats, "demo floats were not loaded (data/demo_project.json missing?)"
                lines.append("demo floats ok (%d)" % len(floats))
                for f in ("app.js", "app.css", "img/logo.png"):
                    assert get(base + "/" + f)[0] == 200, f
                lines.append("assets ok")
            finally:
                be.stop()
        lines.append("SELFTEST OK %s" % __version__)
    except BaseException as e:
        code = 1
        lines.append("SELFTEST FAILED: %s: %s" % (type(e).__name__, e))
    text = "\n".join(lines)
    if args.selftest_out:
        Path(args.selftest_out).write_text(text + "\n")
    if sys.stdout is not None:
        print(text)
    return code


def run_window(args) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception as e:  # pragma: no cover
        print("The window toolkit (tkinter) isn't available: %s" % e)
        return 1

    root = tk.Tk()
    root.withdraw()
    try:  # crisp text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    def fail(msg):
        messagebox.showerror(TITLE, msg, parent=root)
        return 1

    try:
        be = Backend(args.port, args.artnet_port, args.data, args.seed)
    except OSError as e:
        if is_scenebuilder(args.port):  # another copy is already running: just show it
            webbrowser.open("http://localhost:%d" % args.port)
            return 0
        return fail("DMX Scene Builder could not open web port %d.\n\n%s\n\nSomething else on this computer "
                    "may be using that port." % (args.port, e))
    except Exception as e:
        logging.getLogger("scenebuilder.desktop").exception("startup failed")
        return fail("DMX Scene Builder could not start.\n\n%s: %s" % (type(e).__name__, e))

    port, url = be.port, "http://localhost:%d" % be.port
    root.title(TITLE)
    root.configure(bg=BG)
    root.resizable(False, False)
    try:
        icon = resource_root() / "packaging" / "icon.png"
        if icon.exists():
            root._icon = tk.PhotoImage(file=str(icon)).subsample(4)
            root.iconphoto(True, root._icon)
    except Exception:
        pass

    def label(text, size=13, color=CHAMPAGNE, bold=False, **kw):
        return tk.Label(root, text=text, bg=BG, fg=color, font=("Helvetica", size, "bold" if bold else "normal"), **kw)

    label("DMX SCENE BUILDER", 11, GOLD, True).pack(padx=28, pady=(24, 0))
    label("Jeff Holmes Presents", 10, MUTED).pack()
    state = label("DMX Scene Builder is running", 18, CHAMPAGNE, True)
    state.pack(padx=28, pady=(18, 4))
    live = label("", 11, WARN)
    live.pack()
    label("On an iPad on the same Wi-Fi, open Safari and go to:", 11, MUTED).pack(padx=28, pady=(14, 4))
    for a in addresses(port):
        e = tk.Entry(root, readonlybackground=BG, fg=GOLD, relief="flat", justify="center", width=34,
                     font=("Courier", 13), highlightthickness=0, borderwidth=0)
        e.insert(0, a)
        e.configure(state="readonly")
        e.pack(pady=1)
    if be.ctl.bind_error:
        tk.Label(root, text=be.ctl.bind_error, bg=BG, fg=WARN, wraplength=380, font=("Helvetica", 10)).pack(padx=28, pady=(10, 0))

    def button(text, cmd, primary):
        b = tk.Label(root, text=text, cursor="hand2", padx=22, pady=9,
                     bg=GOLD if primary else BG, fg=BG if primary else GOLD,
                     font=("Helvetica", 13, "bold"), highlightthickness=1, highlightbackground=GOLD)
        b.bind("<Button-1>", lambda _e: cmd())
        return b

    def open_browser():
        webbrowser.open(url)

    quitting = {"on": False}

    def quit_app(force=False):
        if quitting["on"]:
            return
        if not force and be.is_live():
            if not messagebox.askyesno(TITLE, "A float is live and lights are being controlled.\n\n"
                                       "Quit anyway? The lights will be released.", icon="warning", parent=root):
                return
        quitting["on"] = True
        state.configure(text="Stopping...")
        root.update_idletasks()
        be.stop()
        root.destroy()

    row = tk.Frame(root, bg=BG)
    row.pack(pady=(22, 10))
    button("Open in browser", open_browser, True).pack(side="left", padx=6)
    button("Quit", quit_app, False).pack(side="left", padx=6)
    label("Closing this window quits. Your floats are saved automatically.", 10, MUTED).pack(padx=28, pady=(4, 24))

    def poll():
        if be.error:
            quitting["on"] = True
            be.stop()
            messagebox.showerror(TITLE, "The server stopped unexpectedly:\n\n%s\n\nSee desktop.log in the data folder." % be.error, parent=root)
            root.destroy()
            return
        try:
            live.configure(text="A float is live" if be.is_live() else "")
        except Exception:
            pass
        root.after(1000, poll)

    root.protocol("WM_DELETE_WINDOW", quit_app)
    try:
        root.createcommand("tk::mac::Quit", quit_app)
        root.createcommand("tk::mac::ReopenApplication", lambda: (root.deiconify(), root.lift()))
    except tk.TclError:
        pass
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry("+%d+%d" % ((root.winfo_screenwidth() - w) // 2, (root.winfo_screenheight() - h) // 3))
    root.deiconify()
    root.lift()
    if not args.no_browser:
        root.after(800, open_browser)
    root.after(1000, poll)
    try:
        root.mainloop()
    finally:
        be.stop()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scenebuilder.desktop")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--artnet-port", type=int, default=6454)
    ap.add_argument("--data", default=None)
    ap.add_argument("--seed", default=os.environ.get("SCENEBUILDER_SEED"))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="start the server, fetch the pages, exit 0 if all is well")
    ap.add_argument("--selftest-out", default=None, help="also write the selftest result to this file")
    args, _unknown = ap.parse_known_args(argv)  # macOS may add -psn_... when launched from Finder
    setup_logging(args.data, to_file=not args.selftest)
    if args.selftest:
        return selftest(args)
    try:
        return run_window(args)
    except BaseException as e:  # fail loud: show it, never vanish
        logging.getLogger("scenebuilder.desktop").exception("fatal")
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(TITLE, "DMX Scene Builder hit a problem and has to close.\n\n%s: %s" % (type(e).__name__, e))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
