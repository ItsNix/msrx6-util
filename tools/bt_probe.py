import socket, sys, time
if len(sys.argv) != 2:
    sys.exit("usage: bt_probe.py <MSRX6 Bluetooth MAC>")
addr = sys.argv[1]
for ch in range(1, 6):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(8)
    try:
        s.connect((addr, ch))
    except Exception as e:
        print(f"ch{ch}: connect failed: {e}"); s.close(); continue
    print(f"ch{ch}: connected")
    for name, cmd in [("comm test ESC e", b"\x1be"), ("firmware ESC v", b"\x1bv"), ("model ESC t", b"\x1bt")]:
        s.send(cmd)
        time.sleep(0.8)
        try:
            s.settimeout(2); data = s.recv(256)
        except socket.timeout:
            data = b""
        print(f"  {name}: {data!r}")
    s.close()
    break
