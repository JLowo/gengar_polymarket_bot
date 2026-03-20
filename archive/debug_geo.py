#!/usr/bin/env python3
"""Debug: test if fetching a Polymarket geo token fixes the order 403.

Polymarket's web UI fetches a token from their geoblock endpoint and
passes it as a header with trading requests. py-clob-client may not
do this automatically, which could explain why GET requests work 
(no token needed) but POST /order fails (token required).
"""

import json
import urllib.request

# ── Step 1: Check what the geoblock endpoint returns ──
print("=" * 60)
print("Step 1: Fetch geoblock token (direct, no proxy)")
print("=" * 60)
try:
    req = urllib.request.Request(
        "https://polymarket.com/api/geoblock",
        headers={"User-Agent": "Mozilla/5.0"}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read().decode())
    print(f"Response: {json.dumps(data, indent=2)}")
    # Look for any token-like field
    for key, val in data.items():
        print(f"  {key}: {val}")
except Exception as e:
    print(f"Error: {e}")

print()

# ── Step 2: Check via Tor ──
print("=" * 60)
print("Step 2: Fetch geoblock token (through Tor)")
print("=" * 60)
try:
    import socks
    import socket
    
    # Temporarily route through Tor for this test
    default_socket = socket.socket
    socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", 9050, rdns=True)
    socket.socket = socks.socksocket
    
    req = urllib.request.Request(
        "https://polymarket.com/api/geoblock",
        headers={"User-Agent": "Mozilla/5.0"}
    )
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read().decode())
    print(f"Response: {json.dumps(data, indent=2)}")
    
    # Restore default socket
    socket.socket = default_socket
except Exception as e:
    print(f"Error: {e}")
    try:
        socket.socket = default_socket
    except:
        pass

print()

# ── Step 3: Check py-clob-client for geo token support ──
print("=" * 60)
print("Step 3: Inspect py-clob-client for geo/header options")
print("=" * 60)
try:
    import inspect
    from py_clob_client.client import ClobClient
    
    sig = inspect.signature(ClobClient.__init__)
    print(f"ClobClient.__init__ params:")
    for name, param in sig.parameters.items():
        if name == 'self':
            continue
        default = param.default if param.default != inspect.Parameter.empty else "REQUIRED"
        print(f"  {name}: {default}")
except Exception as e:
    print(f"Error: {e}")

print()

# ── Step 4: Check if ClobClient has header injection ──
print("=" * 60)
print("Step 4: Check for header/geo methods")
print("=" * 60)
try:
    from py_clob_client.client import ClobClient
    methods = [m for m in dir(ClobClient) if 'geo' in m.lower() or 'header' in m.lower() or 'token' in m.lower()]
    print(f"Geo/header/token related methods: {methods}")
    
    # Also check the http helpers module
    try:
        from py_clob_client import http_helpers
        src = inspect.getsource(http_helpers)
        if 'geo' in src.lower():
            # Find lines with 'geo'
            for i, line in enumerate(src.split('\n')):
                if 'geo' in line.lower():
                    print(f"  http_helpers.py:{i}: {line.strip()}")
        else:
            print("  No 'geo' references in http_helpers")
            
        # Check for header construction
        if 'header' in src.lower():
            for i, line in enumerate(src.split('\n')):
                if 'header' in line.lower() and ('def ' in line or 'HEADER' in line or '{' in line):
                    print(f"  http_helpers.py:{i}: {line.strip()}")
    except Exception as e2:
        print(f"  Could not inspect http_helpers: {e2}")
        
except Exception as e:
    print(f"Error: {e}")

print()

# ── Step 5: Check the actual request headers py-clob-client sends ──
print("=" * 60)
print("Step 5: Check what headers the CLOB client sends")
print("=" * 60)
try:
    from py_clob_client import headers as clob_headers
    src = inspect.getsource(clob_headers)
    print("py_clob_client/headers.py:")
    print(src[:1500])
except Exception as e:
    try:
        # Try alternate location
        from py_clob_client.headers import create_level_2_headers
        src = inspect.getsource(create_level_2_headers)
        print("create_level_2_headers:")
        print(src[:1000])
    except Exception as e2:
        print(f"Could not find headers module: {e}, {e2}")