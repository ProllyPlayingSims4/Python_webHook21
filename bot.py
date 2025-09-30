# bot.py
import os, hmac, hashlib, time, math, urllib.parse, random, string, json, logging, traceback
from typing import Dict, Any, Tuple, Optional, List
import contextvars
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from dotenv import load_dotenv

load_dotenv()

# ---------- Config ----------
API_KEY        = os.getenv("BINANCE_FUTURES_TESTNET_KEY", "")
API_SECRET_RAW = os.getenv("BINANCE_FUTURES_TESTNET_SECRET", "")
API_SECRET     = API_SECRET_RAW.encode() if API_SECRET_RAW else b""
WEBHOOK_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "")

DEFAULT_SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
DEFAULT_MARGIN = os.getenv("MARGIN_TYPE", "ISOLATED")
DEFAULT_LEV    = int(os.getenv("LEVERAGE", "3"))
DEFAULT_NOTION = float(os.getenv("NOTIONAL_USD", "10"))

COOLDOWN_SEC   = int(os.getenv("COOLDOWN_SEC", "60"))
BASE           = os.getenv("BINANCE_BASE", "https://testnet.binancefuture.com")
TIMEOUT_SEC    = int(os.getenv("TIMEOUT_SEC", "10"))
MAX_RETRIES    = int(os.getenv("MAX_RETRIES", "6"))
# Testnet + CF + Render often need a wider window; default to 45s (can override via env).
DEFAULT_RECV_WINDOW = int(os.getenv("RECV_WINDOW_MS", "45000"))

# ---------- Logging (JSON) ----------
logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(message)s'))
if not logger.handlers:
    logger.addHandler(handler)

REQ_ID: contextvars.ContextVar[str] = contextvars.ContextVar("req_id", default="-")

def log_json(event: str, **kv):
    try:
        logger.info(json.dumps({"event": event, "ts": int(time.time()*1000), "req_id": REQ_ID.get("-"), **kv}, ensure_ascii=False))
    except Exception:
        print(f"[LOG_FAIL] {event}: {kv}")

# ---------- HTTP Session w/ retries ----------
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

def rget(path, **kw): kw.setdefault("timeout", TIMEOUT_SEC); return SESSION.get(f"{BASE}{path}", **kw)
def rpost(path, **kw): kw.setdefault("timeout", TIMEOUT_SEC); return SESSION.post(f"{BASE}{path}", **kw)

def sign_qs(params: Dict[str, Any]) -> str:
    qs  = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(API_SECRET, qs.encode(), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"

def _headers_form(): return {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
def _headers_json(): return {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/json"}

def _mbx_header_snapshot(h: Dict[str, str]) -> Dict[str, str]:
    keys = ["x-mbx-used-weight-1m", "x-mbx-order-count-10s", "x-mbx-order-count-1m", "x-mbx-order-count-1d", "date", "server"]
    out = {}
    for k in keys:
        v = h.get(k) or h.get(k.title()) or h.get(k.upper())
        if v: out[k] = v
    return out

def _safe_json(r: requests.Response):
    try: return r.json()
    except Exception: return {"raw": (r.text[:500] if r.text else ""), "parse_error": True}

# ---------- Time sync ----------
TIME_OFFSET_MS = 0
def local_ms() -> int: return int(time.time() * 1000)

def binance_server_time_ms() -> int:
    r = rget("/fapi/v1/time"); r.raise_for_status()
    return int(r.json()["serverTime"])

def sync_time():
    global TIME_OFFSET_MS
    try:
        srv = binance_server_time_ms()
        TIME_OFFSET_MS = srv - local_ms()
        log_json("time_sync_ok", time_offset_ms=TIME_OFFSET_MS)
    except Exception as e:
        log_json("time_sync_fail", err=str(e))

def now_ms_server() -> int: return local_ms() + TIME_OFFSET_MS

# ---------- Signed helpers w/ escalations ----------
def _attach_common(params: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(params)
    # Prefer server clock; fallback to local+offset
    try: p["timestamp"] = binance_server_time_ms()
    except Exception: p["timestamp"] = now_ms_server()
    p.setdefault("recvWindow", DEFAULT_RECV_WINDOW)
    return p

def signed_get(path: str, params: Dict[str, Any]) -> requests.Response:
    p = _attach_common(params)
    full = f"{path}?{sign_qs(p)}"
    log_json("http_signed_get", path=path, params=p)
    r = rget(full, headers={"X-MBX-APIKEY": API_KEY})
    j = _safe_json(r)
    if r.status_code in (400, 401) and isinstance(j, dict) and j.get("code") in (-1021, -1000):
        # Escalate recvWindow + resync + server timestamp
        log_json("http_signed_get_retry", path=path, status=r.status_code, code=j.get("code"), msg=j.get("msg"))
        time.sleep(0.25); sync_time()
        p2 = _attach_common(params); p2["recvWindow"] = max(DEFAULT_RECV_WINDOW, 45000)
        full2 = f"{path}?{sign_qs(p2)}"
        r = rget(full2, headers={"X-MBX-APIKEY": API_KEY}); j = _safe_json(r)
    log_json("http_signed_get_done", path=path, status=r.status_code, code=(j.get("code") if isinstance(j, dict) else None), msg=(j.get("msg") if isinstance(j, dict) else None))
    return r

def signed_post(path: str, params: Dict[str, Any]) -> requests.Response:
    """
    Body-first (form), then query; escalate on -1021 with server time + 45s recvWindow.
    """
    # Attempt 1: BODY
    p = _attach_common(params)
    qs = sign_qs(p)
    log_json("http_signed_post", mode="body", path=path, params=p)
    r = rpost(path, headers=_headers_form(), data=qs); j = _safe_json(r)

    if r.status_code in (400, 401, 500) and isinstance(j, dict) and j.get("code") in (-1021, -1000):
        # Attempt 2: QUERY (basic flip)
        log_json("http_signed_post_retry", mode="query", path=path, status=r.status_code, code=j.get("code"), msg=j.get("msg"))
        time.sleep(0.35); sync_time()
        p2 = _attach_common(params)
        r = rpost(f"{path}?{sign_qs(p2)}", headers={"X-MBX-APIKEY": API_KEY}); j = _safe_json(r)

    if r.status_code in (400, 401) and isinstance(j, dict) and j.get("code") == -1021:
        # Attempt 3: escalate recvWindow + enforce server ts, try body then query
        log_json("recv_window_escalate", prev_recv=DEFAULT_RECV_WINDOW)
        time.sleep(0.25); sync_time()
        p3 = _attach_common(params); p3["recvWindow"] = max(DEFAULT_RECV_WINDOW, 45000)
        r = rpost(path, headers=_headers_form(), data=sign_qs(p3)); j = _safe_json(r)
        if r.status_code in (400, 401) and isinstance(j, dict) and j.get("code") == -1021:
            r = rpost(f"{path}?{sign_qs(p3)}", headers={"X-MBX-APIKEY": API_KEY}); j = _safe_json(r)

    log_json("http_signed_post_done", path=path, status=r.status_code,
             code=(j.get("code") if isinstance(j, dict) else None),
             msg=(j.get("msg") if isinstance(j, dict) else None),
             headers=_mbx_header_snapshot(r.headers))
    return r

# Initial sync on import
sync_time()

# ---------- Binance helpers ----------
def get_exchange_filters(symbol: str) -> Tuple[float, float]:
    r = rget("/fapi/v1/exchangeInfo"); r.raise_for_status()
    sym = next(s for s in r.json()["symbols"] if s["symbol"] == symbol)
    lot = next(f for f in sym["filters"] if f["filterType"] in ("MARKET_LOT_SIZE", "LOT_SIZE"))
    return float(lot["stepSize"]), float(lot["minQty"])

def snap_qty(qty: float, step: float, min_qty: float) -> str:
    q = max(min_qty, math.floor(qty / step) * step)
    s = ("%.8f" % q).rstrip("0").rstrip(".")
    return s if s else "0"

def get_mark_price(symbol: str) -> float:
    r = rget("/fapi/v1/premiumIndex", params={"symbol": symbol}); r.raise_for_status()
    return float(r.json()["markPrice"])

def is_hedge_mode() -> bool:
    r = signed_get("/fapi/v1/positionSide/dual", {})
    if r.status_code != 200:
        log_json("hedge_mode_read_fail", status=r.status_code, body=r.text[:300])
        return False
    try:
        return bool(r.json().get("dualSidePosition", False))
    except Exception as e:
        log_json("hedge_mode_parse_fail", err=str(e), body=r.text[:300])
        return False

def get_account_overview() -> Dict[str, Any]:
    r = signed_get("/fapi/v2/account", {}); return _safe_json(r)

def get_leverage_brackets(symbol: str) -> Any:
    r = signed_get("/fapi/v1/leverageBracket", {"symbol": symbol}); return _safe_json(r)

def get_current_leverage(symbol: str) -> Optional[int]:
    r = signed_get("/fapi/v2/positionRisk", {"symbol": symbol})
    if r.status_code != 200: log_json("lev_read_fail", status=r.status_code, body=r.text[:300]); return None
    try:
        data = r.json()
        if isinstance(data, list) and data: return int(float(data[0].get("leverage", "0")))
    except Exception as e:
        log_json("lev_read_parse_fail", err=str(e))
    return None

def set_margin_type(symbol: str, margin_type: str):
    params = {"symbol": symbol, "marginType": margin_type}
    r = signed_post("/fapi/v1/marginType", params)
    if r.status_code == 200: return {"msg": "OK"}
    j = _safe_json(r)
    if isinstance(j, dict) and j.get("code") == -4046: return {"msg": "No need to change margin type."}
    _raise_with_diagnosis("marginType", r, extra={"symbol": symbol, "margin_type": margin_type})

def set_leverage(symbol: str, leverage: int):
    cur = get_current_leverage(symbol)
    if cur == leverage: return {"leverage": cur, "skipped": True}
    params = {"symbol": symbol, "leverage": leverage}
    r = signed_post("/fapi/v1/leverage", params)
    if r.status_code == 200: return _safe_json(r)
    j = _safe_json(r)
    if isinstance(j, dict) and j.get("code") == -1000:
        log_json("lev_retry_on_1000", symbol=symbol, leverage=leverage)
        time.sleep(0.4); sync_time()
        r2 = signed_post("/fapi/v1/leverage", params)
        if r2.status_code == 200: return _safe_json(r2)
        _raise_with_diagnosis("leverage", r2, extra={"symbol": symbol, "leverage": leverage})
    _raise_with_diagnosis("leverage", r, extra={"symbol": symbol, "leverage": leverage})

def test_market_order(symbol: str, side: str, qty_str: str, position_side: Optional[str]):
    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty_str}
    if position_side: params["positionSide"] = position_side
    return signed_post("/fapi/v1/order/test", params)

def _rand_id(prefix: str = "tv") -> str:
    sfx = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    return f"{prefix}_{int(time.time()*1000)}_{sfx}"

def _diagnose(symbol: str, side: str, qty_str: str, leverage: int, margin_type: str) -> Dict[str, Any]:
    try: step, min_qty = get_exchange_filters(symbol)
    except Exception: step, min_qty = None, None
    try: mark = get_mark_price(symbol)
    except Exception: mark = None
    try: hedge = is_hedge_mode()
    except Exception: hedge = None

    acct = get_account_overview()
    lev_now = get_current_leverage(symbol)
    bracket = get_leverage_brackets(symbol)

    try: notional_now = (float(qty_str) * float(mark)) if (qty_str and mark) else None
    except Exception: notional_now = None

    suspicions: List[str] = []
    if isinstance(acct, dict) and acct.get("canTrade") is False:
        suspicions.append("Account cannot trade (canTrade=false)")
    if notional_now is not None and notional_now < 5.0:
        suspicions.append("Order notional < 5 USDT min for many symbols")
    if step and min_qty and qty_str:
        try:
            if float(qty_str) < float(min_qty): suspicions.append("Quantity below minQty")
        except Exception: pass

    return {
        "symbol": symbol, "side": side,
        "requested_leverage": leverage, "current_leverage": lev_now,
        "margin_type": margin_type, "hedge_mode": hedge,
        "stepSize": step, "minQty": min_qty, "mark": mark,
        "qty": qty_str, "notional_now": notional_now,
        "account_canTrade": acct.get("canTrade") if isinstance(acct, dict) else None,
        "availableBalance": acct.get("availableBalance") if isinstance(acct, dict) else None,
        "recvWindow": DEFAULT_RECV_WINDOW, "timeOffsetMs": TIME_OFFSET_MS,
        "bracket": bracket, "suspicions": suspicions,
    }

def _raise_with_diagnosis(where: str, r: requests.Response, extra: Optional[Dict[str, Any]] = None):
    j = _safe_json(r)
    diag = {}
    try:
        if extra:
            diag = _diagnose(
                extra.get("symbol", "?"),
                extra.get("side", "?"),
                extra.get("qty", "0"),
                extra.get("leverage", 0),
                extra.get("margin_type", "?"),
            )
    except Exception as e:
        diag = {"diag_error": str(e)}

    payload = {
        "error": f"{where}_failed",
        "status_code": r.status_code,
        "binance": j,
        "headers": _mbx_header_snapshot(r.headers),
        "diagnosis": diag,
        "req_id": REQ_ID.get("-"),
    }
    log_json("error", where=where, **payload)
    raise HTTPException(status_code=400 if r.status_code in (400, 401) else 500, detail=payload)

def place_market_order(symbol: str, side: str, qty_str: str):
    # Decide positionSide
    position_side = None
    try:
        if is_hedge_mode():
            position_side = "LONG" if side == "BUY" else "SHORT"
        else:
            position_side = "BOTH"  # explicit in one-way mode
    except Exception as e:
        log_json("hedge_mode_check_fail", err=str(e))

    # Preflight
    rt = test_market_order(symbol, side, qty_str, None if position_side == "BOTH" else position_side)
    log_json("order_test_resp", status=rt.status_code, body=_safe_json(rt))
    if rt.status_code not in (200, 201):
        _raise_with_diagnosis("order_test", rt, extra={"symbol": symbol, "side": side, "qty": qty_str, "leverage": 0, "margin_type": "?"})

    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty_str, "newClientOrderId": _rand_id("tv")}
    if position_side and position_side != "BOTH":
        params["positionSide"] = position_side

    # Up to 3 attempts: alternate body/query; resync & backoff on -1000/-1021
    attempt = 0
    last_r = None
    while attempt < 3:
        attempt += 1
        p = _attach_common(params)
        mode = "body" if attempt % 2 == 1 else "query"
        log_json("order_attempt", attempt=attempt, mode=mode, params=p)

        if mode == "body":
            r = rpost("/fapi/v1/order", headers=_headers_form(), data=sign_qs(p))
        else:
            r = rpost(f"/fapi/v1/order?{sign_qs(p)}", headers={"X-MBX-APIKEY": API_KEY})

        j = _safe_json(r)
        log_json("order_attempt_done", attempt=attempt, mode=mode, status=r.status_code,
                 code=(j.get("code") if isinstance(j, dict) else None),
                 msg=(j.get("msg") if isinstance(j, dict) else None),
                 headers=_mbx_header_snapshot(r.headers))

        if r.status_code == 200:
            return j

        code = (j.get("code") if isinstance(j, dict) else None)
        if code not in (-1000, -1021):
            _raise_with_diagnosis("order", r, extra={"symbol": symbol, "side": side, "qty": qty_str, "leverage": 0, "margin_type": "?"})

        # backoff + resync before next try
        time.sleep(0.35 + attempt * 0.15); sync_time()
        last_r = r

    # all attempts failed → raise with full diagnosis
    _raise_with_diagnosis("order", last_r, extra={"symbol": symbol, "side": side, "qty": qty_str, "leverage": 0, "margin_type": "?"})

# ---------- App state ----------
FILTERS_CACHE: Dict[str, Tuple[float, float]] = {}
LAST_ORDER_TS: Dict[Tuple[str, str], float] = {}

app = FastAPI()

# ---------- Security ----------
def verify_signature(body_bytes: bytes, header_sig: str) -> bool:
    if not WEBHOOK_SECRET: return True
    digest = hmac.new(WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header_sig or "")

# ---------- Model ----------
class TradeSignal(BaseModel):
    symbol: Optional[str] = Field(default=None)
    side: Optional[str] = Field(default=None)                 # BUY or SELL
    notional_usd: Optional[float] = Field(default=None)       # e.g. 25.0
    leverage: Optional[int] = Field(default=None)             # e.g. 2,3,5,10
    margin_type: Optional[str] = Field(default=None)          # ISOLATED or CROSSED

    @validator("side")
    def validate_side(cls, v):
        if v is None: return v
        vu = v.upper()
        if vu not in ("BUY", "SELL"): raise ValueError("side must be BUY or SELL")
        return vu

    @validator("margin_type")
    def validate_margin(cls, v):
        if v is None: return v
        vu = v.upper()
        if vu not in ("ISOLATED", "CROSSED"): raise ValueError("margin_type must be ISOLATED or CROSSED")
        return vu

# ---------- Routes ----------
@app.get("/")
def health():
    return {"ok": True, "base": BASE, "recvWindow": DEFAULT_RECV_WINDOW, "timeOffsetMs": TIME_OFFSET_MS}

@app.get("/whoami")
def whoami():
    return {"api_key_tail": (API_KEY[-6:] if API_KEY else ""), "base": BASE}

@app.on_event("startup")
async def _startup_sync():
    sync_time()
    try: log_json("routes", routes=[r.path for r in app.routes])
    except Exception: pass

@app.get("/diag")
def diag(symbol: str = "BTCUSDT", notional: float = 10.0, leverage: int = 3, margin_type: str = "ISOLATED"):
    step, min_qty = get_exchange_filters(symbol)
    mark = get_mark_price(symbol)
    raw_qty = max(0.0, notional / mark)
    qty = snap_qty(raw_qty, step, min_qty)
    hedge = False
    try: hedge = is_hedge_mode()
    except Exception as e: log_json("diag_hedge_fail", err=str(e))
    return {
        "symbol": symbol, "mark": mark, "stepSize": step, "minQty": min_qty,
        "notional": notional, "rawQty": raw_qty, "snappedQty": qty,
        "leverage": leverage, "margin_type": margin_type, "hedge_mode": hedge,
        "timeOffsetMs": TIME_OFFSET_MS, "recvWindow": DEFAULT_RECV_WINDOW, "base": BASE,
    }

@app.get("/account")
def account(): return get_account_overview()

@app.get("/bracket")
def bracket(symbol: str = "BTCUSDT"): return get_leverage_brackets(symbol)

@app.post("/webhook")
async def webhook(request: Request):
    req_id = _rand_id("req"); REQ_ID.set(req_id)

    raw_body = await request.body()
    qs_secret  = request.query_params.get("secret", "")
    header_sig = request.headers.get("X-Signature", "")

    if WEBHOOK_SECRET:
        if not (qs_secret == WEBHOOK_SECRET or verify_signature(raw_body, header_sig)):
            log_json("auth_fail", reason="invalid_signature"); raise HTTPException(401, "Invalid signature")

    # Parse JSON
    try:
        body_str = raw_body.decode("utf-8", errors="replace")[:2000]
        log_json("webhook_body", body=body_str)
        parsed = await request.json()
        log_json("webhook_parsed", parsed=parsed)
        payload = TradeSignal(**parsed)
    except Exception as e:
        log_json("json_parse_fail", err=str(e))
        raise HTTPException(400, f"Invalid JSON payload: {e}")

    symbol       = (payload.symbol or DEFAULT_SYMBOL).upper()
    side         = (payload.side or "BUY").upper()
    notional_usd = payload.notional_usd if payload.notional_usd and payload.notional_usd > 0 else DEFAULT_NOTION
    leverage     = int(payload.leverage) if payload.leverage and payload.leverage > 0 else DEFAULT_LEV
    margin_type  = payload.margin_type or DEFAULT_MARGIN

    # Cooldown
    k = (symbol, side); now = time.time(); last = LAST_ORDER_TS.get(k, 0.0)
    if now - last < COOLDOWN_SEC:
        rem = int(COOLDOWN_SEC - (now - last))
        log_json("cooldown", symbol=symbol, side=side, seconds_left=rem)
        return {"status": "cooldown", "symbol": symbol, "side": side, "seconds_left": rem, "req_id": req_id}

    # Filters
    if symbol not in FILTERS_CACHE:
        step, min_qty = get_exchange_filters(symbol); FILTERS_CACHE[symbol] = (step, min_qty)
    else:
        step, min_qty = FILTERS_CACHE[symbol]

    # Margin & leverage (idempotent)
    try: set_margin_type(symbol, margin_type)
    except Exception as e: log_json("set_margin_type_nonfatal", err=str(e))
    set_leverage(symbol, leverage)

    # Size
    mark = get_mark_price(symbol)
    raw_qty = max(0.0, notional_usd / mark)
    qty = snap_qty(raw_qty, step, min_qty)
    if qty == "0": raise HTTPException(400, f"Computed quantity < minQty for {symbol}; increase notional_usd")

    # Enforce ~5 USDT min notional (common)
    if (float(qty) * mark) < 5.0:
        steps_needed = math.ceil((5.0 / mark) / step)
        qty = snap_qty(steps_needed * step, step, min_qty)

    # Place order
    resp = place_market_order(symbol, side, qty)
    LAST_ORDER_TS[k] = now
    log_json("order_ok", symbol=symbol, side=side, qty=qty, resp=resp)
    return {
        "status": "ok", "symbol": symbol, "side": side, "margin_type": margin_type, "leverage": leverage,
        "notional_usd": notional_usd, "mark": mark, "qty": qty,
        "orderId": resp.get("orderId"), "executedQty": resp.get("executedQty"), "req_id": req_id,
    }

# Global error handler → JSON with req_id
@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    req_id = REQ_ID.get("-")
    log_json("unhandled_exception", err=str(exc), tb=traceback.format_exc())
    return JSONResponse(status_code=500, content={"error": "unhandled_exception", "detail": str(exc), "req_id": req_id})








# # app.py
# import os, hmac, hashlib, time, math, urllib.parse
# from typing import Dict, Any, Tuple
# import requests
# from fastapi import FastAPI, Request, HTTPException
# from pydantic import BaseModel, Field, validator
# from dotenv import load_dotenv

# load_dotenv()

# # === Config via env vars (fallback defaults only) ===
# API_KEY        = os.getenv("BINANCE_FUTURES_TESTNET_KEY", "")
# API_SECRET_RAW = os.getenv("BINANCE_FUTURES_TESTNET_SECRET", "")
# API_SECRET     = API_SECRET_RAW.encode() if API_SECRET_RAW else b""
# WEBHOOK_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "")           # shared with TradingView

# DEFAULT_SYMBOL = os.getenv("SYMBOL", "BTCUSDT")                   # used if alert omits symbol
# DEFAULT_MARGIN = os.getenv("MARGIN_TYPE", "ISOLATED")             # ISOLATED or CROSSED
# DEFAULT_LEV    = int(os.getenv("LEVERAGE", "3"))                  # fallback leverage
# DEFAULT_NOTION = float(os.getenv("NOTIONAL_USD", "10"))           # fallback notional (USD)

# COOLDOWN_SEC   = int(os.getenv("COOLDOWN_SEC", "60"))
# BASE           = os.getenv("BINANCE_BASE", "https://testnet.binancefuture.com")
# TIMEOUT_SEC    = int(os.getenv("TIMEOUT_SEC", "10"))
# MAX_RETRIES    = int(os.getenv("MAX_RETRIES", "6"))
# DEFAULT_RECV_WINDOW = int(os.getenv("RECV_WINDOW_MS", "60000"))   # generous 60s

# # === HTTP Session with retries ===
# SESSION = requests.Session()
# from requests.adapters import HTTPAdapter
# from urllib3.util.retry import Retry
# retry = Retry(
#     total=MAX_RETRIES, connect=MAX_RETRIES, read=MAX_RETRIES,
#     backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
#     allowed_methods=None, raise_on_status=False
# )
# adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=50)
# SESSION.mount("https://", adapter)
# SESSION.mount("http://", adapter)

# def headers() -> Dict[str, str]:
#     return {"X-MBX-APIKEY": API_KEY}

# def rget(path, **kw):
#     kw.setdefault("timeout", TIMEOUT_SEC)
#     return SESSION.get(f"{BASE}{path}", **kw)

# def rpost(path, **kw):
#     kw.setdefault("timeout", TIMEOUT_SEC)
#     return SESSION.post(f"{BASE}{path}", **kw)

# def sign(params: Dict[str, Any]) -> str:
#     qs  = urllib.parse.urlencode(params, doseq=True)
#     sig = hmac.new(API_SECRET, qs.encode(), hashlib.sha256).hexdigest()
#     return f"{qs}&signature={sig}"

# # === Time sync & signed requests (fixes -1021 / stabilizes -1000) ===
# TIME_OFFSET_MS = 0  # server_time - local_time

# def local_ms() -> int:
#     return int(time.time() * 1000)

# def binance_server_time_ms() -> int:
#     r = rget("/fapi/v1/time")
#     r.raise_for_status()
#     return int(r.json()["serverTime"])

# def sync_time():
#     """Sync TIME_OFFSET_MS with Binance server time (addresses -1021)."""
#     global TIME_OFFSET_MS
#     try:
#         srv = binance_server_time_ms()
#         TIME_OFFSET_MS = srv - local_ms()
#         print(f"[TIME SYNC] offset set to {TIME_OFFSET_MS} ms")
#     except Exception as e:
#         print("[TIME SYNC] failed:", e)

# def now_ms_server() -> int:
#     return local_ms() + TIME_OFFSET_MS

# def signed_get(path: str, params: Dict[str, Any]) -> requests.Response:
#     """Signed GET with timestamp, recvWindow, and 1 retry on -1021/-1000."""
#     def _do(params_):
#         params_["timestamp"] = now_ms_server()
#         params_.setdefault("recvWindow", DEFAULT_RECV_WINDOW)
#         full = f"{path}?{sign(params_)}"
#         return rget(full, headers=headers())

#     r = _do(dict(params))
#     try:
#         j = r.json()
#     except Exception:
#         j = None

#     if r.status_code in (400, 401) and isinstance(j, dict) and j.get("code") in (-1021, -1000):
#         print("[SIGNED GET] error", j, "— resyncing and retrying once")
#         sync_time()
#         r = _do(dict(params))
#     return r

# def signed_post(path: str, params: Dict[str, Any]) -> requests.Response:
#     """
#     Signed POST with timestamp, recvWindow.
#     Retries on:
#       -1021 (timestamp/recvWindow) -> resync & retry
#       -1000 (unknown)             -> brief backoff, resync & up to 2 retries
#     """
#     def _do(params_):
#         params_["timestamp"] = now_ms_server()
#         params_.setdefault("recvWindow", DEFAULT_RECV_WINDOW)
#         full = f"{path}?{sign(params_)}"
#         return rpost(full, headers=headers())

#     r = _do(dict(params))
#     try:
#         j = r.json()
#     except Exception:
#         j = None

#     if r.status_code in (400, 401) and isinstance(j, dict) and j.get("code") in (-1021, -1000):
#         print("[SIGNED POST] error", j, "— handling…")
#         if j.get("code") == -1000:
#             time.sleep(0.25)  # tiny backoff for transient errors
#         sync_time()
#         r = _do(dict(params))
#         try:
#             j2 = r.json()
#         except Exception:
#             j2 = None

#         if r.status_code in (400, 401) and isinstance(j2, dict) and j2.get("code") == -1000:
#             print("[SIGNED POST] persistent -1000, final short backoff & retry")
#             time.sleep(0.5)
#             sync_time()
#             r = _do(dict(params))
#     return r

# # Initial time sync on import
# sync_time()

# # === Binance helpers ===
# def get_exchange_filters(symbol: str) -> Tuple[float, float]:
#     """Return (stepSize, minQty) for a symbol."""
#     r = rget("/fapi/v1/exchangeInfo")
#     r.raise_for_status()
#     sym = next(s for s in r.json()["symbols"] if s["symbol"] == symbol)
#     lot = next(f for f in sym["filters"] if f["filterType"] in ("MARKET_LOT_SIZE", "LOT_SIZE"))
#     return float(lot["stepSize"]), float(lot["minQty"])

# def snap_qty(qty: float, step: float, min_qty: float) -> str:
#     q = max(min_qty, math.floor(qty / step) * step)
#     s = ("%.8f" % q).rstrip("0").rstrip(".")
#     return s if s else "0"

# def get_mark_price(symbol: str) -> float:
#     r = rget("/fapi/v1/premiumIndex", params={"symbol": symbol})
#     r.raise_for_status()
#     return float(r.json()["markPrice"])

# def set_margin_type(symbol: str, margin_type: str):
#     params = {"symbol": symbol, "marginType": margin_type}
#     r = signed_post("/fapi/v1/marginType", params)
#     if r.status_code == 200:
#         return {"msg": "OK"}
#     try:
#         err = r.json()
#     except Exception:
#         err = {}
#     if isinstance(err, dict) and err.get("code") == -4046:
#         return {"msg": "No need to change margin type."}
#     raise HTTPException(500, f"marginType failed: {r.status_code} {r.text}")

# # def set_leverage(symbol: str, leverage: int):
# #     params = {"symbol": symbol, "leverage": leverage}
# #     r = signed_post("/fapi/v1/leverage", params)
# #     if r.status_code != 200:
# #         raise HTTPException(500, f"leverage failed: {r.status_code} {r.text}")
# #     return r.json()

# def get_current_leverage(symbol: str) -> int | None:
#     r = signed_get("/fapi/v2/positionRisk", {"symbol": symbol})
#     if r.status_code != 200:
#         print("[LEV READ] failed:", r.status_code, r.text[:300])
#         return None
#     try:
#         data = r.json()
#         # positionRisk returns a list; leverage is a string number
#         if isinstance(data, list) and data:
#             return int(float(data[0].get("leverage", "0")))
#     except Exception as e:
#         print("[LEV READ] parse error:", e)
#     return None

# def set_leverage(symbol: str, leverage: int):
#     # skip if already set
#     cur = get_current_leverage(symbol)
#     if cur == leverage:
#         return {"leverage": cur, "skipped": True}

#     params = {"symbol": symbol, "leverage": leverage}
#     r = signed_post("/fapi/v1/leverage", params)
#     if r.status_code == 200:
#         return r.json()

#     # One tolerant retry for -1000 after short backoff
#     try:
#         j = r.json()
#     except Exception:
#         j = {}
#     if isinstance(j, dict) and j.get("code") == -1000:
#         time.sleep(0.4)
#         r2 = signed_post("/fapi/v1/leverage", params)
#         if r2.status_code == 200:
#             return r2.json()

#     raise HTTPException(500, f"leverage failed: {r.status_code} {r.text}")


# def is_hedge_mode() -> bool:
#     """Detect if account is in Hedge Mode (dualSidePosition)."""
#     r = signed_get("/fapi/v1/positionSide/dual", {})
#     if r.status_code != 200:
#         print("[HEDGE MODE] read failed:", r.status_code, r.text)
#         return False
#     try:
#         data = r.json()
#         return bool(data.get("dualSidePosition", False))
#     except Exception as e:
#         print("[HEDGE MODE] parse failed:", e, r.text)
#         return False

# def get_account_overview() -> Dict[str, Any]:
#     """Return /fapi/v2/account (balances, permissions)."""
#     r = signed_get("/fapi/v2/account", {})
#     try:
#         return r.json()
#     except Exception:
#         return {"status_code": r.status_code, "text": r.text[:500]}

# # def get_leverage_brackets(symbol: str) -> Any:
# #     r = rget("/fapi/v1/leverageBracket", params={"symbol": symbol})
# #     try:
# #         return r.json()
# #     except Exception:
# #         return {"status_code": r.status_code, "text": r.text[:500]}
# def get_leverage_brackets(symbol: str) -> Any:
#     # Either signed (safest):
#     r = signed_get("/fapi/v1/leverageBracket", {"symbol": symbol})
#     try:
#         return r.json()
#     except Exception:
#         return {"status_code": r.status_code, "text": r.text[:500]}

# def test_market_order(symbol: str, side: str, qty_str: str, position_side: str | None):
#     """Preflight: /fapi/v1/order/test — validates params with no fill."""
#     params = {
#         "symbol": symbol,
#         "side": side,
#         "type": "MARKET",
#         "quantity": qty_str,
#     }
#     if position_side:
#         params["positionSide"] = position_side
#     r = signed_post("/fapi/v1/order/test", params)
#     return r

# def place_market_order(symbol: str, side: str, qty_str: str):
#     # Add positionSide in hedge mode
#     position_side = None
#     try:
#         if is_hedge_mode():
#             position_side = "LONG" if side == "BUY" else "SHORT"
#     except Exception as e:
#         print("[ORDER] hedge mode check failed:", e)

#     # Preflight test — catches most param issues cleanly
#     rt = test_market_order(symbol, side, qty_str, position_side)
#     try:
#         print("[ORDER TEST RESP]", rt.status_code, rt.text[:500])
#     except Exception:
#         pass
#     if rt.status_code not in (200, 201):
#         raise HTTPException(500, f"order test failed: {rt.status_code} {rt.text}")

#     # Real order
#     params = {
#         "symbol": symbol,
#         "side": side,
#         "type": "MARKET",
#         "quantity": qty_str,
#     }
#     if position_side:
#         params["positionSide"] = position_side

#     r = signed_post("/fapi/v1/order", params)
#     try:
#         print("[ORDER RESP]", r.status_code, r.text[:500])
#     except Exception:
#         pass
#     if r.status_code != 200:
#         raise HTTPException(500, f"order failed: {r.status_code} {r.text}")
#     return r.json()

# # === App state ===
# FILTERS_CACHE: Dict[str, Tuple[float, float]] = {}   # per-symbol (stepSize, minQty)
# LAST_ORDER_TS: Dict[Tuple[str, str], float] = {}     # cooldown per (symbol, side)

# app = FastAPI()

# # === Security ===
# def verify_signature(body_bytes: bytes, header_sig: str) -> bool:
#     """HMAC-SHA256 over raw body with shared WEBHOOK_SECRET (alt to ?secret=)."""
#     if not WEBHOOK_SECRET:
#         return True
#     digest = hmac.new(WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()
#     return hmac.compare_digest(digest, header_sig or "")

# # === Pydantic model ===
# class TradeSignal(BaseModel):
#     symbol: str | None = Field(default=None, description="e.g., BTCUSDT")
#     side: str | None = Field(default=None, description="BUY or SELL")
#     notional_usd: float | None = Field(default=None, description="USD amount to trade, e.g., 25.0")
#     leverage: int | None = Field(default=None, description="e.g., 2,3,5,10")
#     margin_type: str | None = Field(default=None, description="ISOLATED or CROSSED (optional)")

#     @validator("side")
#     def validate_side(cls, v):
#         if v is None: return v
#         v_up = v.upper()
#         if v_up not in ("BUY", "SELL"):
#             raise ValueError("side must be BUY or SELL")
#         return v_up

#     @validator("margin_type")
#     def validate_margin(cls, v):
#         if v is None: return v
#         v_up = v.upper()
#         if v_up not in ("ISOLATED", "CROSSED"):
#             raise ValueError("margin_type must be ISOLATED or CROSSED")
#         return v_up

# # === Routes ===
# @app.get("/")
# def health():
#     return {
#         "ok": True,
#         "base": BASE,
#         "recvWindow": DEFAULT_RECV_WINDOW,
#         "timeOffsetMs": TIME_OFFSET_MS
#     }

# @app.on_event("startup")
# async def _startup_sync():
#     sync_time()

# @app.get("/diag")
# def diag(symbol: str = "BTCUSDT", notional: float = 10.0, leverage: int = 3, margin_type: str = "ISOLATED"):
#     """Preflight: compute filters, price, qty, detect hedge mode; no order is placed."""
#     step, min_qty = get_exchange_filters(symbol)
#     mark = get_mark_price(symbol)
#     raw_qty = max(0.0, notional / mark)
#     qty = snap_qty(raw_qty, step, min_qty)
#     hedge = False
#     try:
#         hedge = is_hedge_mode()
#     except Exception as e:
#         print("[DIAG] hedge mode check failed:", e)

#     return {
#         "symbol": symbol,
#         "mark": mark,
#         "stepSize": step,
#         "minQty": min_qty,
#         "notional": notional,
#         "rawQty": raw_qty,
#         "snappedQty": qty,
#         "leverage": leverage,
#         "margin_type": margin_type,
#         "hedge_mode": hedge,
#         "timeOffsetMs": TIME_OFFSET_MS,
#         "recvWindow": DEFAULT_RECV_WINDOW,
#         "base": BASE,
#     }

# @app.get("/account")
# def account():
#     """See balances, permissions, and positions from /fapi/v2/account."""
#     return get_account_overview()

# @app.get("/bracket")
# def bracket(symbol: str = "BTCUSDT"):
#     """Leverage brackets for a symbol (useful to know max leverage by notional tiers)."""
#     return get_leverage_brackets(symbol)

# @app.post("/webhook")
# async def webhook(request: Request):
#     # --- Signature check (supports either header X-Signature or ?secret= token) ---
#     raw_body = await request.body()
#     qs_secret  = request.query_params.get("secret", "")
#     header_sig = request.headers.get("X-Signature", "")

#     if WEBHOOK_SECRET:
#         if not (qs_secret == WEBHOOK_SECRET or verify_signature(raw_body, header_sig)):
#             raise HTTPException(401, "Invalid signature")

#     # --- Robust JSON parse + DEBUG (so 400s are easy to diagnose) ---
#     try:
#         print("DEBUG Raw body:", raw_body.decode("utf-8", errors="replace")[:500])
#         parsed = await request.json()
#         print("DEBUG Parsed JSON:", parsed)
#         payload = TradeSignal(**parsed)
#     except Exception as e:
#         raise HTTPException(400, f"Invalid JSON payload: {e}")

#     # Fill from payload or fallback to .env defaults
#     symbol       = (payload.symbol or DEFAULT_SYMBOL).upper()
#     side         = (payload.side or "BUY").upper()  # default BUY if side omitted
#     notional_usd = payload.notional_usd if payload.notional_usd and payload.notional_usd > 0 else DEFAULT_NOTION
#     leverage     = int(payload.leverage) if payload.leverage and payload.leverage > 0 else DEFAULT_LEV
#     margin_type  = payload.margin_type or DEFAULT_MARGIN

#     # --- Cooldown per (symbol, side) ---
#     k = (symbol, side)
#     now = time.time()
#     last = LAST_ORDER_TS.get(k, 0.0)
#     if now - last < COOLDOWN_SEC:
#         return {"status": "cooldown", "symbol": symbol, "side": side,
#                 "seconds_left": int(COOLDOWN_SEC - (now - last))}

#     # --- Ensure filters cached for this symbol ---
#     if symbol not in FILTERS_CACHE:
#         step, min_qty = get_exchange_filters(symbol)
#         FILTERS_CACHE[symbol] = (step, min_qty)
#     else:
#         step, min_qty = FILTERS_CACHE[symbol]

#     # --- Ensure margin type & leverage (idempotent) ---
#     try:
#         set_margin_type(symbol, margin_type)
#     except Exception:
#         pass  # ok if already set

#     set_leverage(symbol, leverage)  # raises if invalid for symbol

#     # --- Live mark price & qty calc ---
#     mark = get_mark_price(symbol)
#     raw_qty = max(0.0, notional_usd / mark)
#     qty = snap_qty(raw_qty, step, min_qty)
#     if qty == "0":
#         raise HTTPException(400, f"Computed quantity < minQty for {symbol}; increase notional_usd")

#     # --- Place the order exactly as requested (with test first) ---
#     resp = place_market_order(symbol, side, qty)
#     LAST_ORDER_TS[k] = now

#     return {
#         "status": "ok",
#         "symbol": symbol,
#         "side": side,
#         "margin_type": margin_type,
#         "leverage": leverage,
#         "notional_usd": notional_usd,
#         "mark": mark,
#         "qty": qty,
#         "orderId": resp.get("orderId"),
#         "executedQty": resp.get("executedQty"),
#     }


