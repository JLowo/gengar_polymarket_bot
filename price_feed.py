"""Real-time BTC price feed from Binance WebSocket.

Subscribes to BTCUSDT trade stream for tick-by-tick price updates.
Falls back to REST API polling if WebSocket fails.
"""

import json
import time
import threading
import urllib.request
from dataclasses import dataclass, field
from typing import Optional, Callable


BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"


@dataclass
class PriceState:
    """Thread-safe container for current BTC price."""
    price: float = 0.0
    timestamp: float = 0.0
    source: str = "none"
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, price: float, source: str = "ws"):
        with self._lock:
            self.price = price
            self.timestamp = time.time()
            self.source = source

    def get(self) -> tuple[float, float]:
        """Returns (price, age_in_seconds)."""
        with self._lock:
            return self.price, time.time() - self.timestamp

    @property
    def is_fresh(self) -> bool:
        """Price is considered fresh if < 5 seconds old."""
        _, age = self.get()
        return age < 5.0 and self.price > 0


class BinancePriceFeed:
    def __init__(self):
        self.state = PriceState()
        self._ws_thread: Optional[threading.Thread] = None
        self._running = False
        self._on_price: Optional[Callable] = None

    def start(self, on_price: Callable = None):
        """Start the price feed. Tries WebSocket first, falls back to REST polling."""
        self._on_price = on_price
        self._running = True

        # Try WebSocket first
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()

        # Also start REST poller as backup
        threading.Thread(target=self._rest_poll_loop, daemon=True).start()

        print("[price] BTC price feed starting...")

    def stop(self):
        self._running = False

    def _ws_loop(self):
        """WebSocket connection to Binance for real-time trades."""
        try:
            import websockets
            import asyncio

            async def connect():
                while self._running:
                    try:
                        async with websockets.connect(BINANCE_WS_URL) as ws:
                            print("[price] WebSocket connected to Binance")
                            while self._running:
                                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                                data = json.loads(msg)
                                price = float(data.get("p", 0))
                                if price > 0:
                                    self.state.update(price, source="ws")
                                    if self._on_price:
                                        self._on_price(price)
                    except Exception as e:
                        if self._running:
                            print(f"[price] WebSocket error: {e}, reconnecting in 3s...")
                            await asyncio.sleep(3)

            asyncio.run(connect())
        except ImportError:
            print("[price] websockets not available, using REST polling only")

    def _rest_poll_loop(self):
        """Fallback: poll Binance REST API every 2 seconds."""
        time.sleep(3)  # Give WebSocket a head start
        while self._running:
            try:
                # Only poll if WebSocket data is stale
                if not self.state.is_fresh:
                    req = urllib.request.Request(
                        BINANCE_REST_URL,
                        headers={"User-Agent": "PolyBot/1.0"},
                    )
                    resp = urllib.request.urlopen(req, timeout=5)
                    data = json.loads(resp.read().decode())
                    price = float(data.get("price", 0))
                    if price > 0:
                        self.state.update(price, source="rest")
                        if self._on_price:
                            self._on_price(price)
            except Exception:
                pass
            time.sleep(2)

    def get_price(self) -> tuple[float, bool]:
        """Get current BTC price and whether it's fresh.
        
        Returns (price, is_fresh).
        """
        price, age = self.state.get()
        return price, self.state.is_fresh

    def wait_for_price(self, timeout: float = 30) -> float:
        """Block until we have a valid price. Returns price or 0 on timeout."""
        start = time.time()
        while time.time() - start < timeout:
            if self.state.is_fresh:
                return self.state.price
            time.sleep(0.1)
        return 0.0


if __name__ == "__main__":
    feed = BinancePriceFeed()
    feed.start()
    
    print("Waiting for first price...")
    price = feed.wait_for_price(timeout=15)
    if price:
        print(f"BTC price: ${price:,.2f} (source: {feed.state.source})")
    else:
        print("Timeout waiting for price")
    
    # Watch for 10 seconds
    for i in range(10):
        time.sleep(1)
        p, fresh = feed.get_price()
        print(f"  [{i+1}s] ${p:,.2f} {'✓' if fresh else '✗'}")
    
    feed.stop()
