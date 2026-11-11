#!/usr/bin/env python3
"""Minimal Linux driver for the Deftun MSRX6 (MSR605X-compatible protocol).

No dependencies: talks to /dev/hidrawN (USB) or an RFCOMM socket (Bluetooth SPP, ch 1).
Auto-detects USB first, then a paired Bluetooth device named MSRX6*.
  --dev /dev/hidraw3 | --dev AA:BB:CC:DD:EE:FF | --dev bt

  msrx6.py info                      firmware / model / coercivity
  msrx6.py read                      swipe a card, print tracks 1-3
  msrx6.py write -1 ABC -2 123 -3 4  arm write, then swipe the card
  msrx6.py erase [-t 1,2,3]          arm erase, then swipe the card
  msrx6.py hico | loco               set write coercivity
"""
import argparse, functools, glob, os, re, select, socket, subprocess, sys, threading
print = functools.partial(print, flush=True)


class MSRError(Exception):
    pass


class Cancelled(Exception):
    pass

VID, PID = 0x0801, 0x0003
ESC, FS = b"\x1b", b"\x1c"
STATUS = {b"0": "OK", b"1": "read/write error", b"2": "command format error",
          b"4": "invalid command", b"9": "invalid card swipe in write mode"}


def find_dev():
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        with open(f"{node}/device/uevent") as f:
            if f"HID_ID=0003:{VID:08X}:{PID:08X}" in f.read():
                return "/dev/" + os.path.basename(node)
    return None


MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def find_bt():
    """First paired Bluetooth device whose name starts with MSRX6."""
    try:
        out = subprocess.run(["bluetoothctl", "devices", "Paired"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3 and parts[2].startswith("MSRX6") and MAC_RE.match(parts[1]):
            return parts[1]
    return None


class MSR:
    def __new__(cls, path=None):
        if cls is MSR:
            if path is None:
                path = find_dev() or find_bt()
                if path is None:
                    raise MSRError("MSRX6 not found (not on USB, and no paired MSRX6* Bluetooth device)")
            elif path == "bt":
                path = find_bt()
                if path is None:
                    raise MSRError("No paired MSRX6* Bluetooth device (pair it with bluetoothctl first)")
            cls = MSRBluetooth if MAC_RE.match(path) else MSR
        obj = super().__new__(cls)
        obj._path_arg = path
        return obj

    def __init__(self, path=None):
        path = self._path_arg
        try:
            self.fd = os.open(path, os.O_RDWR)
        except PermissionError:
            raise MSRError(f"No permission on {path}; install /etc/udev/rules.d/70-msrx6.rules or use sudo")
        self.path = path
        self.cancel = threading.Event()

    def close(self):
        os.close(self.fd)

    def send(self, data: bytes):
        # MSR605X framing: 64-byte reports, header = 0x80 first | 0x40 last | payload len (<=63)
        chunks = [data[i:i + 63] for i in range(0, len(data), 63)] or [b""]
        for i, c in enumerate(chunks):
            hdr = (0x80 if i == 0 else 0) | (0x40 if i == len(chunks) - 1 else 0) | len(c)
            os.write(self.fd, b"\x00" + bytes([hdr]) + c.ljust(63, b"\x00"))  # 0x00 = report ID

    def recv(self, timeout=None) -> bytes:
        """timeout=None waits for a swipe until self.cancel is set (then resets the reader)."""
        out = b""
        while True:
            r, _, _ = select.select([self.fd], [], [], 0.2 if timeout is None else timeout)
            if not r:
                if timeout is not None:
                    raise MSRError("no response from reader")
                if self.cancel.is_set():
                    self.cancel.clear()
                    self.reset()
                    raise Cancelled()
                continue
            pkt = os.read(self.fd, 64)
            out += pkt[1:1 + (pkt[0] & 0x3F)]
            if pkt[0] & 0x40:
                return out

    def cmd(self, data, timeout=3):
        self.send(data)
        return self.recv(timeout)

    def reset(self):
        self.send(ESC + b"a")

    # --- high-level operations (shared by CLI and GUI) ---
    def info(self):
        co = self.cmd(ESC + b"d")[-1:]
        return {
            "comm": "OK" if self.cmd(ESC + b"e").endswith(b"y") else "FAIL",
            "firmware": self.cmd(ESC + b"v")[1:].decode(errors="replace"),
            "model": self.cmd(ESC + b"t")[1:].decode(errors="replace"),
            "coercivity": {b"h": "Hi-Co", b"l": "Lo-Co"}.get(co, repr(co)),
        }

    def read(self):
        """Blocks until a swipe. Returns (tracks dict, status str, raw bytes)."""
        resp = self.cmd(ESC + b"r", timeout=None)
        return parse_tracks(resp), status(resp), resp

    def write(self, t1="", t2="", t3=""):
        payload = (ESC + b"s" + ESC + b"\x01" + t1.encode() + ESC + b"\x02" + t2.encode()
                   + ESC + b"\x03" + t3.encode() + b"?" + FS)
        return status(self.cmd(ESC + b"w" + payload, timeout=None))

    def erase(self, tracks=(1, 2, 3)):
        mask = sum({1: 1, 2: 2, 3: 4}[t] for t in set(tracks))
        sel = {1: 0x00, 2: 0x02, 4: 0x04, 3: 0x03, 5: 0x05, 6: 0x06, 7: 0x07}[mask]
        return status(self.cmd(ESC + b"c" + bytes([sel]), timeout=None))

    def set_coercivity(self, hico: bool):
        return status(self.cmd(ESC + (b"x" if hico else b"y")))


class MSRBluetooth(MSR):
    """Same protocol, but raw unframed bytes over RFCOMM (Bluetooth SPP)."""
    CHANNEL = 1
    IDLE = 0.3  # a response is complete once the reader goes quiet this long

    def __init__(self, path=None):
        self.path = self._path_arg
        self.cancel = threading.Event()
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self.sock.settimeout(10)
        try:
            self.sock.connect((self.path, self.CHANNEL))
        except OSError as e:
            self.sock.close()
            raise MSRError(f"Bluetooth connect to {self.path} failed: {e} (is the reader on?)")
        self.sock.setblocking(False)
        self.fd = self.sock.fileno()

    def close(self):
        self.sock.close()

    def _drain(self):
        while select.select([self.sock], [], [], 0)[0]:
            if not self.sock.recv(4096):
                raise MSRError("Bluetooth connection closed")

    def send(self, data: bytes):
        self._drain()  # drop stray bytes so they don't prefix the next response
        self.sock.sendall(data)

    def recv(self, timeout=None) -> bytes:
        out = b""
        while True:
            wait = self.IDLE if out else (0.2 if timeout is None else timeout)
            r, _, _ = select.select([self.sock], [], [], wait)
            if r:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise MSRError("Bluetooth connection closed")
                out += chunk
                continue
            if out:
                return out
            if timeout is not None:
                raise MSRError("no response from reader")
            if self.cancel.is_set():
                self.cancel.clear()
                self.reset()
                raise Cancelled()


def status(resp):
    code = resp[-1:]
    return STATUS.get(code, f"unknown status {code!r}")


def parse_tracks(resp):
    # ESC s ESC 01 <t1> ESC 02 <t2> ESC 03 <t3> ? FS ESC <status>
    body = resp.split(ESC + b"s", 1)[-1].split(b"?" + FS)[0]
    tracks = {}
    for n in (1, 2, 3):
        start = body.find(ESC + bytes([n]))
        if start < 0:
            continue
        rest = body[start + 2:]
        nxt = rest.find(ESC)
        tracks[n] = (rest if nxt < 0 else rest[:nxt]).decode(errors="replace")
    return tracks


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dev", help="hidraw node, Bluetooth MAC, or 'bt' (auto-detected)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sub.add_parser("read")
    w = sub.add_parser("write")
    for n in (1, 2, 3):
        w.add_argument(f"-{n}", dest=f"t{n}", default="", help=f"track {n} data (no sentinels)")
    e = sub.add_parser("erase")
    e.add_argument("-t", default="1,2,3", help="tracks to erase, e.g. 1,3")
    sub.add_parser("hico")
    sub.add_parser("loco")
    a = p.parse_args()

    m = MSR(a.dev)
    m.reset()

    if a.cmd == "info":
        for k, v in m.info().items():
            print(f"{k + ':':12}{v}")
    elif a.cmd == "read":
        print("Swipe card... (Ctrl-C to cancel)")
        tracks, st, raw = m.read()
        if os.environ.get("MSR_DEBUG"):
            print("raw:", raw.hex(" "))
        for n, t in tracks.items():
            print(f"Track {n}: {t}")
        print("Status:", st)
    elif a.cmd == "write":
        print("Swipe card to write... (Ctrl-C to cancel)")
        print("Status:", m.write(a.t1, a.t2, a.t3))
    elif a.cmd == "erase":
        print("Swipe card to erase... (Ctrl-C to cancel)")
        print("Status:", m.erase([int(t) for t in a.t.split(",")]))
    elif a.cmd in ("hico", "loco"):
        print("Status:", m.set_coercivity(a.cmd == "hico"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
    except MSRError as e:
        sys.exit(str(e))
