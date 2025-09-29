# app.py
import os, hmac, hashlib, time, math, urllib.parse
from typing import Dict, Any, Tuple
import requests
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, validator
from dotenv import load_dotenv

load_dotenv()

# === Config via env vars (fallback defaults only) ===
API_KEY        = os.getenv("BINANCE_FUTURES_TESTNET_KEY", "")
API_SECRET_RAW = os.getenv("BINANCE_FUTURES_TESTNET_SECRET", "")
API_SECRET     = API_SECRET_RAW.encode() if API_SECRET_RAW else b""
WEBHOOK_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "")           # shared with TradingView

DEFAULT_SYMBOL = os.getenv("SYMBOL", "BTCUSDT")                   # used if alert omits symbol
DEFAULT_MARGIN = os.getenv("MARGIN_TYPE", "ISOLATED")             # ISOLATED or CROSSED
DEFAULT_LEV    = int(os.getenv("LEVERAGE", "3"))                  # fallback leverage
DEFAULT_NOTION = float(os.getenv("NOTIONAL_USD", "10"))           # fallback notional (USD)

COOLDOWN_SEC   = int(os.getenv("COOLDOWN_SEC", "60"))
BASE           = os.getenv("BINANCE_BASE", "https://testnet.binancefuture.com")
TIMEOUT_SEC    = int(os.getenv("TIMEOUT_SEC", "10"))
MAX_RETRIES    = int(os.getenv("MAX_RETRIES", "6"))
DEFAULT_RECV_WINDOW = int(os.getenv("RECV_WINDOW_MS", "60000"))   # generous 60s

# === HTTP Session with retries ===
SESSION = requests.Session()
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
retry = Retry(
    total=MAX_RETRIES, connect=MAX_RETRIES, read=MAX_RETRIES,
    backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=None, raise_on_status=False
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=50)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

def headers() -> Dict[str, str]:
    return {"X-MBX-APIKEY": API_KEY}

def rget(path, **kw):
    kw.setdefault("timeout", TIMEOUT_SEC)
    return SESSION.get(f"{BASE}{path}", **kw)

def rpost(path, **kw):
    kw.setdefault("timeout", TIMEOUT_SEC)
    return SESSION.post(f"{BASE}{path}", **kw)

def sign(params: Dict[str, Any]) -> str:
    qs  = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(API_SECRET, qs.encode(), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"

# === Time sync & signed requests (fixes -1021 / stabilizes -1000) ===
TIME_OFFSET_MS = 0  # server_time - local_time

def local_ms() -> int:
    return int(time.time() * 1000)

def binance_server_time_ms() -> int:
    r = rget("/fapi/v1/time")
    r.raise_for_status()
    return int(r.json()["serverTime"])

def sync_time():
    """Sync TIME_OFFSET_MS with Binance server time (addresses -1021)."""
    global TIME_OFFSET_MS
    try:
        srv = binance_server_time_ms()
        TIME_OFFSET_MS = srv - local_ms()
        print(f"[TIME SYNC] offset set to {TIME_OFFSET_MS} ms")
    except Exception as e:
        print("[TIME SYNC] failed:", e)

def now_ms_server() -> int:
    return local_ms() + TIME_OFFSET_MS

def signed_get(path: str, params: Dict[str, Any]) -> requests.Response:
    """Signed GET with timestamp, recvWindow, and 1 retry on -1021/-1000."""
    def _do(params_):
        params_["timestamp"] = now_ms_server()
        params_.setdefault("recvWindow", DEFAULT_RECV_WINDOW)
        full = f"{path}?{sign(params_)}"
        return rget(full, headers=headers())

    r = _do(dict(params))
    try:
        j = r.json()
    except Exception:
        j = None

    if r.status_code in (400, 401) and isinstance(j, dict) and j.get("code") in (-1021, -1000):
        print("[SIGNED GET] error", j, "— resyncing and retrying once")
        sync_time()
        r = _do(dict(params))
    return r

def signed_post(path: str, params: Dict[str, Any]) -> requests.Response:
    """
    Signed POST with timestamp, recvWindow.
    Retries on:
      -1021 (timestamp/recvWindow) -> resync & retry
      -1000 (unknown)             -> brief backoff, resync & up to 2 retries
    """
    def _do(params_):
        params_["timestamp"] = now_ms_server()
        params_.setdefault("recvWindow", DEFAULT_RECV_WINDOW)
        full = f"{path}?{sign(params_)}"
        return rpost(full, headers=headers())

    r = _do(dict(params))
    try:
        j = r.json()
    except Exception:
        j = None

    if r.status_code in (400, 401) and isinstance(j, dict) and j.get("code") in (-1021, -1000):
        print("[SIGNED POST] error", j, "— handling…")
        if j.get("code") == -1000:
            time.sleep(0.25)  # tiny backoff for transient errors
        sync_time()
        r = _do(dict(params))
        try:
            j2 = r.json()
        except Exception:
            j2 = None

        if r.status_code in (400, 401) and isinstance(j2, dict) and j2.get("code") == -1000:
            print("[SIGNED POST] persistent -1000, final short backoff & retry")
            time.sleep(0.5)
            sync_time()
            r = _do(dict(params))
    return r

# Initial time sync on import
sync_time()

# === Binance helpers ===
def get_exchange_filters(symbol: str) -> Tuple[float, float]:
    """Return (stepSize, minQty) for a symbol."""
    r = rget("/fapi/v1/exchangeInfo")
    r.raise_for_status()
    sym = next(s for s in r.json()["symbols"] if s["symbol"] == symbol)
    lot = next(f for f in sym["filters"] if f["filterType"] in ("MARKET_LOT_SIZE", "LOT_SIZE"))
    return float(lot["stepSize"]), float(lot["minQty"])

def snap_qty(qty: float, step: float, min_qty: float) -> str:
    q = max(min_qty, math.floor(qty / step) * step)
    s = ("%.8f" % q).rstrip("0").rstrip(".")
    return s if s else "0"

def get_mark_price(symbol: str) -> float:
    r = rget("/fapi/v1/premiumIndex", params={"symbol": symbol})
    r.raise_for_status()
    return float(r.json()["markPrice"])

def set_margin_type(symbol: str, margin_type: str):
    params = {"symbol": symbol, "marginType": margin_type}
    r = signed_post("/fapi/v1/marginType", params)
    if r.status_code == 200:
        return {"msg": "OK"}
    try:
        err = r.json()
    except Exception:
        err = {}
    if isinstance(err, dict) and err.get("code") == -4046:
        return {"msg": "No need to change margin type."}
    raise HTTPException(500, f"marginType failed: {r.status_code} {r.text}")

def set_leverage(symbol: str, leverage: int):
    params = {"symbol": symbol, "leverage": leverage}
    r = signed_post("/fapi/v1/leverage", params)
    if r.status_code != 200:
        raise HTTPException(500, f"leverage failed: {r.status_code} {r.text}")
    return r.json()

def is_hedge_mode() -> bool:
    """Detect if account is in Hedge Mode (dualSidePosition)."""
    r = signed_get("/fapi/v1/positionSide/dual", {})
    if r.status_code != 200:
        print("[HEDGE MODE] read failed:", r.status_code, r.text)
        return False
    try:
        data = r.json()
        return bool(data.get("dualSidePosition", False))
    except Exception as e:
        print("[HEDGE MODE] parse failed:", e, r.text)
        return False

def get_account_overview() -> Dict[str, Any]:
    """Return /fapi/v2/account (balances, permissions)."""
    r = signed_get("/fapi/v2/account", {})
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text[:500]}

# def get_leverage_brackets(symbol: str) -> Any:
#     r = rget("/fapi/v1/leverageBracket", params={"symbol": symbol})
#     try:
#         return r.json()
#     except Exception:
#         return {"status_code": r.status_code, "text": r.text[:500]}
def get_leverage_brackets(symbol: str) -> Any:
    # Either signed (safest):
    r = signed_get("/fapi/v1/leverageBracket", {"symbol": symbol})
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text[:500]}

def test_market_order(symbol: str, side: str, qty_str: str, position_side: str | None):
    """Preflight: /fapi/v1/order/test — validates params with no fill."""
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty_str,
    }
    if position_side:
        params["positionSide"] = position_side
    r = signed_post("/fapi/v1/order/test", params)
    return r

def place_market_order(symbol: str, side: str, qty_str: str):
    # Add positionSide in hedge mode
    position_side = None
    try:
        if is_hedge_mode():
            position_side = "LONG" if side == "BUY" else "SHORT"
    except Exception as e:
        print("[ORDER] hedge mode check failed:", e)

    # Preflight test — catches most param issues cleanly
    rt = test_market_order(symbol, side, qty_str, position_side)
    try:
        print("[ORDER TEST RESP]", rt.status_code, rt.text[:500])
    except Exception:
        pass
    if rt.status_code not in (200, 201):
        raise HTTPException(500, f"order test failed: {rt.status_code} {rt.text}")

    # Real order
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty_str,
    }
    if position_side:
        params["positionSide"] = position_side

    r = signed_post("/fapi/v1/order", params)
    try:
        print("[ORDER RESP]", r.status_code, r.text[:500])
    except Exception:
        pass
    if r.status_code != 200:
        raise HTTPException(500, f"order failed: {r.status_code} {r.text}")
    return r.json()

# === App state ===
FILTERS_CACHE: Dict[str, Tuple[float, float]] = {}   # per-symbol (stepSize, minQty)
LAST_ORDER_TS: Dict[Tuple[str, str], float] = {}     # cooldown per (symbol, side)

app = FastAPI()

# === Security ===
def verify_signature(body_bytes: bytes, header_sig: str) -> bool:
    """HMAC-SHA256 over raw body with shared WEBHOOK_SECRET (alt to ?secret=)."""
    if not WEBHOOK_SECRET:
        return True
    digest = hmac.new(WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header_sig or "")

# === Pydantic model ===
class TradeSignal(BaseModel):
    symbol: str | None = Field(default=None, description="e.g., BTCUSDT")
    side: str | None = Field(default=None, description="BUY or SELL")
    notional_usd: float | None = Field(default=None, description="USD amount to trade, e.g., 25.0")
    leverage: int | None = Field(default=None, description="e.g., 2,3,5,10")
    margin_type: str | None = Field(default=None, description="ISOLATED or CROSSED (optional)")

    @validator("side")
    def validate_side(cls, v):
        if v is None: return v
        v_up = v.upper()
        if v_up not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        return v_up

    @validator("margin_type")
    def validate_margin(cls, v):
        if v is None: return v
        v_up = v.upper()
        if v_up not in ("ISOLATED", "CROSSED"):
            raise ValueError("margin_type must be ISOLATED or CROSSED")
        return v_up

# === Routes ===
@app.get("/")
def health():
    return {
        "ok": True,
        "base": BASE,
        "recvWindow": DEFAULT_RECV_WINDOW,
        "timeOffsetMs": TIME_OFFSET_MS
    }

@app.on_event("startup")
async def _startup_sync():
    sync_time()

@app.get("/diag")
def diag(symbol: str = "BTCUSDT", notional: float = 10.0, leverage: int = 3, margin_type: str = "ISOLATED"):
    """Preflight: compute filters, price, qty, detect hedge mode; no order is placed."""
    step, min_qty = get_exchange_filters(symbol)
    mark = get_mark_price(symbol)
    raw_qty = max(0.0, notional / mark)
    qty = snap_qty(raw_qty, step, min_qty)
    hedge = False
    try:
        hedge = is_hedge_mode()
    except Exception as e:
        print("[DIAG] hedge mode check failed:", e)

    return {
        "symbol": symbol,
        "mark": mark,
        "stepSize": step,
        "minQty": min_qty,
        "notional": notional,
        "rawQty": raw_qty,
        "snappedQty": qty,
        "leverage": leverage,
        "margin_type": margin_type,
        "hedge_mode": hedge,
        "timeOffsetMs": TIME_OFFSET_MS,
        "recvWindow": DEFAULT_RECV_WINDOW,
        "base": BASE,
    }

@app.get("/account")
def account():
    """See balances, permissions, and positions from /fapi/v2/account."""
    return get_account_overview()

@app.get("/bracket")
def bracket(symbol: str = "BTCUSDT"):
    """Leverage brackets for a symbol (useful to know max leverage by notional tiers)."""
    return get_leverage_brackets(symbol)

@app.post("/webhook")
async def webhook(request: Request):
    # --- Signature check (supports either header X-Signature or ?secret= token) ---
    raw_body = await request.body()
    qs_secret  = request.query_params.get("secret", "")
    header_sig = request.headers.get("X-Signature", "")

    if WEBHOOK_SECRET:
        if not (qs_secret == WEBHOOK_SECRET or verify_signature(raw_body, header_sig)):
            raise HTTPException(401, "Invalid signature")

    # --- Robust JSON parse + DEBUG (so 400s are easy to diagnose) ---
    try:
        print("DEBUG Raw body:", raw_body.decode("utf-8", errors="replace")[:500])
        parsed = await request.json()
        print("DEBUG Parsed JSON:", parsed)
        payload = TradeSignal(**parsed)
    except Exception as e:
        raise HTTPException(400, f"Invalid JSON payload: {e}")

    # Fill from payload or fallback to .env defaults
    symbol       = (payload.symbol or DEFAULT_SYMBOL).upper()
    side         = (payload.side or "BUY").upper()  # default BUY if side omitted
    notional_usd = payload.notional_usd if payload.notional_usd and payload.notional_usd > 0 else DEFAULT_NOTION
    leverage     = int(payload.leverage) if payload.leverage and payload.leverage > 0 else DEFAULT_LEV
    margin_type  = payload.margin_type or DEFAULT_MARGIN

    # --- Cooldown per (symbol, side) ---
    k = (symbol, side)
    now = time.time()
    last = LAST_ORDER_TS.get(k, 0.0)
    if now - last < COOLDOWN_SEC:
        return {"status": "cooldown", "symbol": symbol, "side": side,
                "seconds_left": int(COOLDOWN_SEC - (now - last))}

    # --- Ensure filters cached for this symbol ---
    if symbol not in FILTERS_CACHE:
        step, min_qty = get_exchange_filters(symbol)
        FILTERS_CACHE[symbol] = (step, min_qty)
    else:
        step, min_qty = FILTERS_CACHE[symbol]

    # --- Ensure margin type & leverage (idempotent) ---
    try:
        set_margin_type(symbol, margin_type)
    except Exception:
        pass  # ok if already set

    set_leverage(symbol, leverage)  # raises if invalid for symbol

    # --- Live mark price & qty calc ---
    mark = get_mark_price(symbol)
    raw_qty = max(0.0, notional_usd / mark)
    qty = snap_qty(raw_qty, step, min_qty)
    if qty == "0":
        raise HTTPException(400, f"Computed quantity < minQty for {symbol}; increase notional_usd")

    # --- Place the order exactly as requested (with test first) ---
    resp = place_market_order(symbol, side, qty)
    LAST_ORDER_TS[k] = now

    return {
        "status": "ok",
        "symbol": symbol,
        "side": side,
        "margin_type": margin_type,
        "leverage": leverage,
        "notional_usd": notional_usd,
        "mark": mark,
        "qty": qty,
        "orderId": resp.get("orderId"),
        "executedQty": resp.get("executedQty"),
    }


