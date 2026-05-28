import socket
import time

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.connect(("127.0.0.1", 4242))

# Enable specific data fields
sock.sendall(b'<SET ID="ENABLE_SEND_POG_FIX" STATE="1" />\r\n')
sock.sendall(b'<SET ID="ENABLE_SEND_CURSOR" STATE="1" />\r\n')
sock.sendall(b'<SET ID="ENABLE_SEND_DATA" STATE="1" />\r\n')

print("Connected! Reading gaze data for 5 seconds...")

sock.settimeout(1.0)
start = time.time()
count = 0
while time.time() - start < 5:
    try:
        data = sock.recv(4096).decode("utf-8")
        if data:
            count += 1
            print(f"Received: {data[:200]}")
    except socket.timeout:
        continue

print(f"Done. Received {count} packets.")
sock.close()