#!/usr/bin/env python3
"""Tkinter GUI for the Deftun MSRX6 — uses msrx6.py (same folder) for the protocol."""
import queue, threading
import tkinter as tk
from tkinter import ttk, messagebox

from msrx6 import MSR, MSRError, Cancelled

# Track limits (ISO 7811) and allowed characters, excluding sentinels.
TRACKS = {
    1: dict(max=76, start="%", ok=lambda c: 0x20 <= ord(c) <= 0x5F and c not in "%?"),
    2: dict(max=37, start=";", ok=lambda c: c in "0123456789=:<>"),
    3: dict(max=104, start=";", ok=lambda c: c in "0123456789=:<>"),
}
MONO = ("monospace", 11)


def strip_sentinels(n, s):
    s = s.strip()
    if s.startswith(TRACKS[n]["start"]):
        s = s[1:]
    if s.endswith("?"):
        s = s[:-1]
    return s


class App(tk.Tk):
    def __init__(self):
        super().__init__(className="MSRX6")
        self.title("MSRX6")
        self.minsize(720, 460)
        ttk.Style(self).theme_use("clam")
        self.msr = None
        self.busy = False
        self.events = queue.Queue()
        self._build()
        self.after(50, self._poll)
        self.connect()

    # ---------- layout ----------
    def _build(self):
        pad = dict(padx=8, pady=4)

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        self.dev_lbl = ttk.Label(top, text="Not connected")
        self.dev_lbl.pack(side="left")
        ttk.Button(top, text="Reconnect", command=self.connect).pack(side="left", padx=8)
        self.coer = tk.StringVar(value="hico")
        ttk.Button(top, text="Apply", command=self.apply_coercivity).pack(side="right")
        ttk.Radiobutton(top, text="Lo-Co", value="loco", variable=self.coer).pack(side="right")
        ttk.Radiobutton(top, text="Hi-Co", value="hico", variable=self.coer).pack(side="right")
        ttk.Label(top, text="Coercivity:").pack(side="right", padx=4)

        tf = ttk.LabelFrame(self, text="Tracks  (sentinels % ; ? are added automatically when writing)")
        tf.pack(fill="x", **pad)
        tf.columnconfigure(2, weight=1)
        self.vars, self.sel, self.counts, self.entries = {}, {}, {}, {}
        for n in TRACKS:
            self.sel[n] = tk.BooleanVar(value=True)
            ttk.Checkbutton(tf, variable=self.sel[n]).grid(row=n, column=0, padx=(6, 0))
            ttk.Label(tf, text=f"Track {n}").grid(row=n, column=1, sticky="w", padx=4)
            v = tk.StringVar()
            v.trace_add("write", lambda *_, n=n: self._validate(n))
            e = tk.Entry(tf, textvariable=v, font=MONO, relief="solid", bd=1)
            e.grid(row=n, column=2, sticky="ew", pady=4)
            self.vars[n], self.entries[n] = v, e
            self.counts[n] = ttk.Label(tf, width=12)
            self.counts[n].grid(row=n, column=3, padx=6)
            self._validate(n)
        ttk.Label(tf, text="☑ = tracks used for Erase", foreground="gray").grid(
            row=4, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        bf = ttk.Frame(self)
        bf.pack(fill="x", **pad)
        self.buttons = []
        for text, cmd in [("Read", self.do_read), ("Write", self.do_write),
                          ("Erase", self.do_erase), ("Clear fields", self.clear)]:
            b = ttk.Button(bf, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.buttons.append(b)
        self.verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(bf, text="Verify after write", variable=self.verify).pack(side="left", padx=12)
        self.cancel_btn = ttk.Button(bf, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="right")

        self.prompt = ttk.Label(self, text="", font=("sans", 13, "bold"), foreground="#c47a00")
        self.prompt.pack(fill="x", padx=8)

        lf = ttk.LabelFrame(self, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.log_txt = tk.Text(lf, height=8, font=("monospace", 9), state="disabled", wrap="word")
        self.log_txt.pack(fill="both", expand=True, padx=4, pady=4)

    # ---------- helpers ----------
    def _validate(self, n):
        s = strip_sentinels(n, self.vars[n].get())
        bad = sorted({c for c in s if not TRACKS[n]["ok"](c)})
        too_long = len(s) > TRACKS[n]["max"]
        self.counts[n].config(text=f"{len(s)}/{TRACKS[n]['max']}",
                              foreground="red" if (bad or too_long) else "")
        self.entries[n].config(bg="#ffd6d6" if (bad or too_long) else "white")
        return bad, too_long

    def log(self, msg):
        self.log_txt.config(state="normal")
        self.log_txt.insert("end", msg + "\n")
        self.log_txt.see("end")
        self.log_txt.config(state="disabled")

    def set_busy(self, busy, prompt=""):
        self.busy = busy
        for b in self.buttons:
            b.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        self.prompt.config(text=prompt)

    def run(self, prompt, fn, done):
        """Run fn() on a worker thread; call done(result) on the UI thread."""
        if not self.msr:
            messagebox.showerror("MSRX6", "Reader not connected.")
            return
        self.set_busy(True, prompt)

        def work():
            try:
                self.events.put(("ok", done, fn()))
            except Cancelled:
                self.events.put(("cancel", None, None))
            except (MSRError, OSError) as e:
                self.events.put(("err", None, e))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, done, val = self.events.get_nowait()
                if kind == "prompt":  # worker changing the prompt mid-operation (write → verify)
                    self.prompt.config(text=val)
                    continue
                self.set_busy(False)
                if kind == "ok":
                    done(val)
                elif kind == "cancel":
                    self.log("Cancelled.")
                else:
                    self.log(f"Error: {val}")
                    if isinstance(val, OSError):  # device unplugged etc.
                        self.msr = None
                        self.dev_lbl.config(text="Disconnected — click Reconnect")
        except queue.Empty:
            pass
        self.after(50, self._poll)

    # ---------- actions ----------
    def connect(self):
        if self.busy:
            return
        if self.msr:
            self.msr.close()
            self.msr = None
        try:
            self.msr = MSR()
            self.msr.reset()
            info = self.msr.info()
        except (MSRError, OSError) as e:
            self.msr = None
            self.dev_lbl.config(text="Not connected")
            self.log(f"Error: {e}")
            return
        self.coer.set("hico" if info["coercivity"] == "Hi-Co" else "loco")
        self.dev_lbl.config(text=f"● {self.msr.path}  fw {info['firmware']}  model {info['model']}  "
                                 f"[{info['coercivity']}]")
        self.log(f"Connected: {info}")

    def apply_coercivity(self):
        hico = self.coer.get() == "hico"
        self.run("", lambda: self.msr.set_coercivity(hico),
                 lambda st: (self.log(f"Set {'Hi-Co' if hico else 'Lo-Co'}: {st}"), self.connect()))

    def do_read(self):
        def done(res):
            tracks, st, _ = res
            for n in TRACKS:
                self.vars[n].set(tracks.get(n, ""))
            self.log(f"Read: {st}  " + "  ".join(f"T{n}={len(t)} chars" for n, t in tracks.items()))
        self.run("Swipe card to READ…", lambda: self.msr.read(), done)

    def do_write(self):
        data = {n: strip_sentinels(n, self.vars[n].get()) for n in TRACKS}
        problems = []
        for n in TRACKS:
            bad, too_long = self._validate(n)
            if bad:
                problems.append(f"Track {n}: invalid characters {''.join(bad)!r}")
            if too_long:
                problems.append(f"Track {n}: longer than {TRACKS[n]['max']}")
        if problems:
            messagebox.showerror("Can't write", "\n".join(problems))
            return
        if not any(data.values()):
            messagebox.showerror("Can't write", "All tracks are empty.")
            return

        def write_then_verify():
            st = self.msr.write(data[1], data[2], data[3])
            if st != "OK" or not self.verify.get():
                return st, None
            self.events.put(("prompt", None, "Swipe card again to VERIFY…"))
            return st, self.msr.read()

        def done(res):
            st, readback = res
            self.log(f"Write: {st}")
            if readback:
                got = {n: strip_sentinels(n, t) for n, t in readback[0].items()}
                bad = [n for n in TRACKS if data[n] and got.get(n) != data[n]]
                if bad:
                    self.log(f"VERIFY FAILED on track(s) {bad}: read back {got}")
                    messagebox.showwarning("Verify", f"Mismatch on track(s) {bad}. See log.")
                else:
                    self.log("Verify: OK, card matches.")
        self.run("Swipe card to WRITE…", write_then_verify, done)

    def do_erase(self):
        tracks = [n for n in TRACKS if self.sel[n].get()]
        if not tracks:
            messagebox.showerror("Erase", "Tick at least one track checkbox.")
            return
        if not messagebox.askyesno("Erase", f"Erase track(s) {tracks} on the next card swiped?"):
            return
        self.run(f"Swipe card to ERASE tracks {tracks}…", lambda: self.msr.erase(tracks),
                 lambda st: self.log(f"Erase {tracks}: {st}"))

    def clear(self):
        for v in self.vars.values():
            v.set("")

    def cancel(self):
        if self.msr:
            self.msr.cancel.set()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
