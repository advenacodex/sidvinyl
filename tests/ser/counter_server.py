import socket, sys, time
port = int(sys.argv[1]); secs = float(sys.argv[2]) if len(sys.argv) > 2 else 120
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", port)); s.listen(1)
c, _ = s.accept(); print("connected", flush=True)
i = 0; t0 = time.time()
while time.time() - t0 < secs:
    blk = bytes((i + k) % 256 for k in range(1024)); i += 1024
    c.sendall(blk)
print("sent", i, flush=True)
