import os, hmac, hashlib, time, math, random, urllib.parse
from typing import Dict, Any
import requests
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
load_dotenv()


# --- Config via env vars (never hardcode keys) ---
API_KEY    = os.getenv("BINANCE_FUTURES_TESTNET_KEY", "")
API_SECRET = os.getenv("BINANCE_FUTURES_TESTNET_SECRET", "").encode()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "")     # shared with TradingView
SYMBOL        = os.getenv("SYMBOL", "BTCUSDT")
MARGIN_TYPE   = os.getenv("MARGIN_TYPE", "ISOLATED")
LEVERAGE      = int(os.getenv("LEVERAGE", "3"))
NOTIONAL_USD  = float(os.getenv("NOTIONAL_USD", "10"))
COOLDOWN_SEC  = int(os.getenv("COOLDOWN_SEC", "60"))
BASE          = os.getenv("BINANCE_BASE", "https://testnet.binancefuture.com")
TIMEOUT_SEC   = int(os.getenv("TIMEOUT_SEC", "10"))
MAX_RETRIES   = int(os.getenv("MAX_RETRIES", "6"))

# --- HTTP Session with retries ---
SESSION = requests.Session()
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
retry = Retry(
    total=MAX_RETRIES, connect=MAX_RETRIES, read=MAX_RETRIES,
    backoff_factor=0.5, status_forcelist=[429,500,502,503,504],
    allowed_methods=None, raise_on_status=False
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=50)
SESSION.mount("https://", adapter); SESSION.mount("http://", adapter)

def headers():
    return {"X-MBX-APIKEY": API_KEY}

def now_ms(): return int(time.time() * 1000)

def sign(params: Dict[str, Any]) -> str:
    qs  = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(API_SECRET, qs.encode(), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"

def rget(path, **kw): kw.setdefault("timeout", TIMEOUT_SEC); return SESSION.get(f"{BASE}{path}", **kw)
def rpost(path, **kw): kw.setdefault("timeout", TIMEOUT_SEC); return SESSION.post(f"{BASE}{path}", **kw)

def get_exchange_filters(symbol: str):
    r = rget("/fapi/v1/exchangeInfo"); r.raise_for_status()
    sym = next(s for s in r.json()["symbols"] if s["symbol"] == symbol)
    lot = next(f for f in sym["filters"] if f["filterType"] in ("MARKET_LOT_SIZE","LOT_SIZE"))
    return float(lot["stepSize"]), float(lot["minQty"])

def snap_qty(qty: float, step: float, min_qty: float) -> str:
    q = max(min_qty, math.floor(qty / step) * step)
    s = ("%.8f" % q).rstrip("0").rstrip(".")
    return s if s else "0"

def get_mark_price(symbol: str) -> float:
    r = rget("/fapi/v1/premiumIndex", params={"symbol": symbol}); r.raise_for_status()
    return float(r.json()["markPrice"])

def set_margin_type(symbol: str, margin_type: str):
    params = {"symbol": symbol, "marginType": margin_type, "timestamp": now_ms()}
    r = rpost(f"/fapi/v1/marginType?{sign(params)}", headers=headers())
    if r.status_code == 200: return {"msg":"OK"}
    try: err = r.json()
    except: err = {}
    if isinstance(err, dict) and err.get("code") == -4046:
        return {"msg":"No need to change margin type."}
    raise HTTPException(500, f"marginType failed: {r.status_code} {r.text}")

def set_leverage(symbol: str, leverage: int):
    params = {"symbol": symbol, "leverage": leverage, "timestamp": now_ms()}
    r = rpost(f"/fapi/v1/leverage?{sign(params)}", headers=headers())
    if r.status_code != 200:
        raise HTTPException(500, f"leverage failed: {r.status_code} {r.text}")
    return r.json()

def place_market_buy(symbol: str, qty_str: str):
    params = {"symbol": symbol,"side":"BUY","type":"MARKET","quantity":qty_str,
              "recvWindow":5000,"timestamp": now_ms()}
    r = rpost(f"/fapi/v1/order?{sign(params)}", headers=headers())
    if r.status_code != 200:
        raise HTTPException(500, f"order failed: {r.status_code} {r.text}")
    return r.json()

# one-time cache (in-memory) for filter + cooldown
STEP, MIN_QTY = None, None
LAST_ORDER_TS = 0.0

app = FastAPI()

def verify_signature(body_bytes: bytes, header_sig: str):
    if not WEBHOOK_SECRET:
        # If you don't set a secret, skip validation (NOT recommended)
        return True
    digest = hmac.new(WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header_sig or "")

@app.get("/")
def health():
    return {"ok": True, "symbol": SYMBOL, "base": BASE}

@app.post("/webhook")
async def webhook(request: Request):
    global STEP, MIN_QTY, LAST_ORDER_TS

    # Validate signature
    body = await request.body()
    #header_sig = request.headers.get("X-Signature", "")
    # accept either ?secret=... or X-Signature HMAC
    qs_secret = request.query_params.get("secret", "")
    header_sig = request.headers.get("X-Signature", "")
   # if not verify_signature(body, header_sig):
     #   raise HTTPException(401, "Invalid signature")

    def verify_signature(body_bytes: bytes, header_sig: str):
        if not WEBHOOK_SECRET:
            return True
        digest = hmac.new(WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, header_sig or "")

    if WEBHOOK_SECRET:
        if not (qs_secret == WEBHOOK_SECRET or verify_signature(body, header_sig)):
            raise HTTPException(401, "Invalid signature")

    payload = await request.json()
    # Example TradingView payload: {"ticker":"BINANCE:BTCUSDT.P","price":113000.1,"peak":113600.5,"drop":600.4,"ts":...}

    # Server-side cooldown
    now = time.time()
    if now - LAST_ORDER_TS < COOLDOWN_SEC:
        return {"status":"cooldown", "seconds_left": int(COOLDOWN_SEC - (now - LAST_ORDER_TS))}

    # Ensure filters cached
    if STEP is None or MIN_QTY is None:
        STEP, MIN_QTY = get_exchange_filters(SYMBOL)

    # Ensure margin+leverage applied (idempotent)
    try:
        set_margin_type(SYMBOL, MARGIN_TYPE)
    except Exception as e:
        # Non-fatal if already set
        pass
    try:
        set_leverage(SYMBOL, LEVERAGE)
    except Exception as e:
        pass

    # Fetch live mark price to compute qty precisely now
    mark = get_mark_price(SYMBOL)
    raw_qty = NOTIONAL_USD / mark
    qty    = snap_qty(raw_qty, STEP, MIN_QTY)
    if qty == "0":
        raise HTTPException(400, "Computed quantity < minQty; raise NOTIONAL_USD")

    resp = place_market_buy(SYMBOL, qty)
    LAST_ORDER_TS = now

    return {
        "status": "ok",
        "symbol": SYMBOL,
        "qty": qty,
        "mark": mark,
        "orderId": resp.get("orderId"),
        "executedQty": resp.get("executedQty"),
    }



# """
# Binance USDⓢ-M Futures (TESTNET):
# Buy ~$10 BTC when price drops $600 from a rolling peak.

# Fixes & improvements vs your original:
# - Robust HTTP layer (Session + retries on DNS/connection/5xx + connection pooling)
# - Safer set_margin_type() (no 'err' before assignment; handles -4046)
# - Local retry wrapper for mark price with exponential backoff + jitter
# - Small poll jitter to avoid synchronized hits
# - Clear separation of helpers; better error messages
# - API keys pulled from env by default (avoid hardcoding)

# Requirements:
#   pip install requests
# """

# import os
# import time
# import hmac
# import math
# import json
# import random
# import hashlib
# import urllib.parse
# import requests
# import sys
# from typing import Tuple, Dict, Any
# from requests.adapters import HTTPAdapter
# from urllib3.util.retry import Retry
# from dotenv import load_dotenv



# # Load the environment variables from .env file
# load_dotenv()

# # ======================
# # ---- Configuration ----
# # ======================

# # Get keys from environment (recommended). Or set placeholders here.
# API_KEY    = os.getenv("api_key")
# API_SECRET = os.getenv("secret_key").encode()

# # Testnet base URL (UM Futures). For mainnet, use: https://fapi.binance.com
# BASE          = "https://testnet.binancefuture.com"

# SYMBOL        = "BTCUSDT"
# MARGIN_TYPE   = "ISOLATED"        # or "CROSSED"
# LEVERAGE      = 3                 # your 2x/3x/etc
# DROP_USD      = 600.0             # trigger threshold from rolling peak
# NOTIONAL_USD  = 10.0              # approx $ amount to BUY each trigger
# COOLDOWN_SEC  = 60                # min seconds between orders
# POLL_SEC      = 2                 # mark price polling cadence (base, jitter added)

# TIMEOUT_SEC   = 10                # per-request timeout
# MAX_RETRIES   = 10                # HTTP retries for transient errors

# # =========================
# # ---- HTTP infrastructure
# # =========================

# SESSION = requests.Session()

# # Retry on DNS/connection errors and 429/5xx. allowed_methods=None => retry on any method.
# retry = Retry(
#     total=MAX_RETRIES,
#     connect=MAX_RETRIES,
#     read=MAX_RETRIES,
#     backoff_factor=0.5,  # exponential backoff: 0.5, 1.0, 2.0, ...
#     status_forcelist=[429, 500, 502, 503, 504],
#     allowed_methods=None,
#     raise_on_status=False,
# )
# adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=50)
# SESSION.mount("https://", adapter)
# SESSION.mount("http://", adapter)


# def _headers() -> Dict[str, str]:
#     return {"X-MBX-APIKEY": API_KEY}


# def rget(path: str, **kw) -> requests.Response:
#     kw.setdefault("timeout", TIMEOUT_SEC)
#     return SESSION.get(f"{BASE}{path}", **kw)


# def rpost(path: str, **kw) -> requests.Response:
#     kw.setdefault("timeout", TIMEOUT_SEC)
#     return SESSION.post(f"{BASE}{path}", **kw)

# # ===================
# # ---- Crypto auth
# # ===================

# def now_ms() -> int:
#     return int(time.time() * 1000)


# def sign(params: Dict[str, Any]) -> str:
#     qs  = urllib.parse.urlencode(params, doseq=True)
#     sig = hmac.new(API_SECRET, qs.encode(), hashlib.sha256).hexdigest()
#     return f"{qs}&signature={sig}"

# # ===================
# # ---- Helpers
# # ===================

# def get_exchange_filters(symbol: str) -> Tuple[float, float]:
#     """Return (stepSize, minQty) for MARKET_LOT_SIZE/LOT_SIZE."""
#     r = rget("/fapi/v1/exchangeInfo")
#     r.raise_for_status()
#     data = r.json()
#     symbols = data.get("symbols", [])
#     sym = next((s for s in symbols if s.get("symbol") == symbol), None)
#     if not sym:
#         raise RuntimeError(f"Symbol {symbol} not found in exchangeInfo")

#     lot = next(
#         (f for f in sym.get("filters", [])
#          if f.get("filterType") in ("MARKET_LOT_SIZE", "LOT_SIZE")),
#         None
#     )
#     if not lot:
#         raise RuntimeError(f"LOT_SIZE filter not found for {symbol}")

#     step = float(lot["stepSize"])
#     min_qty = float(lot["minQty"])
#     return step, min_qty


# def snap_qty(qty: float, step: float, min_qty: float) -> str:
#     q = max(min_qty, math.floor(qty / step) * step)
#     # Convert to trimmed string Binance accepts
#     s = ("%.8f" % q).rstrip("0").rstrip(".")
#     return s if s else "0"


# def get_mark_price(symbol: str) -> float:
#     """Mark price via /premiumIndex."""
#     r = rget("/fapi/v1/premiumIndex", params={"symbol": symbol})
#     r.raise_for_status()
#     data = r.json()
#     return float(data["markPrice"])


# def safe_get_mark(symbol: str, tries: int = 3) -> float:
#     """Retry mark price a few times locally (handles transient DNS hiccups)."""
#     last_exc = None
#     for i in range(tries):
#         try:
#             return get_mark_price(symbol)
#         except Exception as e:
#             last_exc = e
#             # Exponential backoff + small jitter
#             sleep_s = 0.5 * (2 ** i) + random.random() * 0.25
#             time.sleep(sleep_s)
#     raise last_exc


# def set_margin_type(symbol: str, margin_type: str) -> Dict[str, Any]:
#     params = {"symbol": symbol, "marginType": margin_type, "timestamp": now_ms()}
#     r = rpost(f"/fapi/v1/marginType?{sign(params)}", headers=_headers())
#     if r.status_code == 200:
#         return {"msg": "OK"}
#     # Try to parse error body
#     try:
#         err = r.json()
#     except Exception:
#         err = {}
#     # -4046 => "No need to change margin type."
#     if isinstance(err, dict) and err.get("code") == -4046:
#         return {"msg": "No need to change margin type."}
#     raise RuntimeError(f"marginType failed: {r.status_code} {r.text}")


# def set_leverage(symbol: str, leverage: int) -> Dict[str, Any]:
#     params = {"symbol": symbol, "leverage": leverage, "timestamp": now_ms()}
#     r = rpost(f"/fapi/v1/leverage?{sign(params)}", headers=_headers())
#     if r.status_code != 200:
#         raise RuntimeError(f"leverage failed: {r.status_code} {r.text}")
#     return r.json()


# def place_market_buy(symbol: str, qty_str: str) -> Dict[str, Any]:
#     params = {
#         "symbol": symbol,
#         "side": "BUY",
#         "type": "MARKET",
#         "quantity": qty_str,
#         "recvWindow": 5000,
#         "timestamp": now_ms(),
#     }
#     r = rpost(f"/fapi/v1/order?{sign(params)}", headers=_headers())
#     if r.status_code != 200:
#         raise RuntimeError(f"order failed: {r.status_code} {r.text}")
#     return r.json()

# # ==========================
# # ---- Bootstrap & main
# # ==========================

# def ensure_account_ready() -> None:
#     print("Setting margin type & leverage…")
#     try:
#         mt = set_margin_type(SYMBOL, MARGIN_TYPE)
#         print("MarginType:", mt)
#     except Exception as e:
#         print("MarginType error (continuing):", e)

#     try:
#         lev = set_leverage(SYMBOL, LEVERAGE)
#         print("Leverage  :", lev)
#     except Exception as e:
#         # Some accounts require positions closed / constraints; print and continue.
#         print("Leverage error (continuing):", e)


# def main():
#     # Quick sanity: keys present?
#     if API_KEY.startswith("YOUR_") or len(API_SECRET) < 10:
#         print("WARNING: API keys not set. Set env BINANCE_FUTURES_TESTNET_KEY/SECRET or edit the script.")
#         # You can return here if you want to force keys.
#         # return

#     ensure_account_ready()

#     # Filters
#     step, min_qty = get_exchange_filters(SYMBOL)
#     print(f"Filters   : stepSize={step}, minQty={min_qty}")

#     # Initialize rolling peak with current mark price
#     peak = safe_get_mark(SYMBOL)
#     print(f"Start mark price = {peak:.2f} | rolling peak = {peak:.2f}")

#     last_order_ts = 0.0

#     while True:
#         try:
#             p = safe_get_mark(SYMBOL)  # robust local retries

#             # Update rolling peak
#             if p > peak:
#                 peak = p

#             drop = peak - p
#             sys.stdout.write(f"\rPrice={p:.2f} | Peak={peak:.2f} | Drop={drop:.2f}    ")
#             sys.stdout.flush()

#             should_trigger = (drop >= DROP_USD) and ((time.time() - last_order_ts) >= COOLDOWN_SEC)
#             if should_trigger:
#                 raw_qty = NOTIONAL_USD / p
#                 qty = snap_qty(raw_qty, step, min_qty)
#                 if qty == "0":
#                     print("\nComputed quantity < minQty. Increase NOTIONAL_USD.")
#                 else:
#                     print(f"\nTrigger! Drop {drop:.2f} ≥ {DROP_USD}. Buying ~${NOTIONAL_USD} (qty {qty}) at ~{p:.2f}")
#                     resp = place_market_buy(SYMBOL, qty)
#                     print("Order OK:", resp.get("orderId"), resp.get("executedQty"))
#                     last_order_ts = time.time()
#                     # Reset peak so we wait for a fresh $600 leg down
#                     peak = p

#         except KeyboardInterrupt:
#             print("\nStopped by user.")
#             break

#         except Exception as e:
#             # Log and keep going (helps with transient DNS/HTTP issues)
#             print("\nError:", e)
#             time.sleep(3)

#         # Base cadence + small jitter to avoid thundering herd / resolver cache sync
#         time.sleep(POLL_SEC + random.random() * 0.4)


# if __name__ == "__main__":
#     main()
