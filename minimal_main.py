#!/usr/bin/env python3
"""Minimal server to test if Python can bind to port 8080"""

import sys
print(f"[MINIMAL] Starting... Python {sys.version}", flush=True)

try:
    import socket
    
    # Try to bind to port 8080
    print("[MINIMAL] Creating socket...", flush=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    print("[MINIMAL] Binding to 0.0.0.0:8080...", flush=True)
    s.bind(('0.0.0.0', 8080))
    
    print("[MINIMAL] Listening...", flush=True)
    s.listen(5)
    
    print("[MINIMAL] ✓ Successfully bound to port 8080", flush=True)
    print("[MINIMAL] Accepting connections...", flush=True)
    
    while True:
        client, addr = s.accept()
        request = client.recv(1024).decode()
        
        if 'GET /health' in request:
            response = 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"status":"ok","minimal":true}\r\n'
        else:
            response = 'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<h1>Minimal Server Working!</h1>\r\n'
        
        client.send(response.encode())
        client.close()
        print(f"[MINIMAL] Handled request from {addr}", flush=True)

except Exception as e:
    print(f"[MINIMAL] ERROR: {e}", flush=True)
    import traceback
    traceback.print_exc()
    
    # Keep process alive so we can see the error
    import time
    while True:
        print("[MINIMAL] Process still alive but not serving...", flush=True)
        time.sleep(30)