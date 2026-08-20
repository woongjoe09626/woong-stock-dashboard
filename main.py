import requests
import json
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GOOGLE_SHEET_URL = os.environ.get(
    "MY_GOOGLE_SHEET_URL",
    "https://script.google.com/macros/s/AKfycbx1XXKA_GKnIsnaNJqLH0RCCY_iDxSIDv_xalVyuAB6-9gUVYN5r4cy1pNixs1XkSMM/exec"
)
HEADERS_YF = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 종목마다 야후에 새로 접속하면 그때마다 연결을 다시 트느라 오래 걸린다.
# 하나를 같이 쓰면 한 번 튼 연결을 계속 재활용한다.
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=30))


class DataUnavailable(Exception):
    """바깥에서 데이터를 못 가져왔을 때. 절대 조용히 넘기지 않는다.

    예전에는 실패하면 환율을 1380원으로, 보유종목을 빈 목록으로 대신 채웠다.
    그래서 화면은 멀쩡해 보이는데 숫자만 틀린 상태가 오래 갔다. 화면이 비는 게
    틀린 금액을 보여주는 것보다 낫다.
    """

# ─── 캐시 ───────────────────────────────────────────────
_cache = {"data": None, "ts": 0}
_cache_lock = threading.Lock()
CACHE_TTL = 270  # 4분 30초

def get_cached():
    with _cache_lock:
        if _cache["data"] and (time.time() - _cache["ts"] < CACHE_TTL):
            return _cache["data"]
    return None

def set_cache(data):
    with _cache_lock:
        _cache["data"] = data
        _cache["ts"]   = time.time()

# ─── 구글 시트 ──────────────────────────────────────────
_port_cache = {"data": None, "ts": 0}
PORT_TTL = 600  # 10분


def load_portfolio(force=False):
    """구글 시트에서 보유종목을 읽는다. 못 읽으면 예외를 던진다.

    보유종목은 시세와 달리 거의 안 바뀌는데 시트 호출이 3~4초씩 걸려서,
    화면을 열 때마다 그걸 기다리는 게 느림의 절반이었다. 10분 동안 기억해두고,
    종목을 추가·수정·삭제할 때는 force=True로 반드시 새로 읽는다
    (낡은 목록에 덮어쓰면 방금 넣은 종목이 사라진다).
    시트를 손으로 고쳐도 늦어도 10분 안에 반영된다.

    '빈 시트'와 '못 읽음'은 다르다. 예전 코드는 둘 다 빈 목록으로 취급해서,
    시트 호출이 실패하면 보유종목이 하나도 없는 것처럼 보였다.
    구글 앱스스크립트는 가끔 응답이 늦거나 한 번씩 실패해서 두 번 시도한다.
    """
    with _cache_lock:
        if not force and _port_cache["data"] is not None \
           and time.time() - _port_cache["ts"] < PORT_TTL:
            return _port_cache["data"]

    last = None
    for attempt in range(2):
        try:
            res = requests.get(f"{GOOGLE_SHEET_URL}?action=get", timeout=15)
            res.raise_for_status()
            data = res.json()
            if isinstance(data, dict):
                with _cache_lock:
                    _port_cache["data"] = data
                    _port_cache["ts"] = time.time()
                return data
            last = f"예상과 다른 응답: {str(data)[:100]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        print(f"구글 시트 불러오기 실패 ({attempt + 1}/2):", last)
    raise DataUnavailable(f"구글 시트를 못 읽었습니다 — {last}")

def save_portfolio(data):
    """시트에 저장한다. 저장이 안 되면 예외를 던진다.

    저장 실패를 삼키면 화면에는 '성공'이 뜨는데 시트에는 안 들어가 있고,
    다음에 새로고침하면 방금 넣은 종목이 사라져 있다.
    """
    params = {"action": "set", "data": json.dumps(data)}
    res = requests.get(GOOGLE_SHEET_URL, params=params, timeout=15)
    res.raise_for_status()
    with _cache_lock:
        _port_cache["data"] = data
        _port_cache["ts"] = time.time()

# ─── API 모델 ────────────────────────────────────────────
class AddItem(BaseModel):
    owner: str
    code: str
    buy_price: float
    qty: float          # 소수점 매수를 받는다 (0.5주 같은 것)
    note: str = ""


class EditItem(BaseModel):
    """한 칸만 고칠 수 있게 전부 선택 항목이다.

    예전에는 수정하려면 주인·종목·단가·수량을 순서대로 다 다시 넣어야 했다.
    수량만 바꾸고 싶어도 나머지를 또 입력해야 해서 번거로웠다.
    이제 보낸 항목만 바뀌고 나머지는 그대로 둔다.
    """
    id: str
    owner: str | None = None
    code: str | None = None
    buy_price: float | None = None
    qty: float | None = None
    note: str | None = None


class DeleteItem(BaseModel):
    id: str


# ─── 포트폴리오 CRUD ─────────────────────────────────────
@app.post("/api/update")
def update_portfolio(item: EditItem):
    my_port = load_portfolio(force=True)
    if item.id not in my_port:
        return {"error": "종목을 찾을 수 없습니다."}

    entry = dict(my_port[item.id])
    for field in ("owner", "code", "buy_price", "qty", "note"):
        value = getattr(item, field)
        if value is not None:
            entry[field] = value

    # 주인이나 종목코드가 바뀌면 저장 열쇠도 따라 바뀐다.
    # 옛 열쇠를 안 지우면 같은 종목이 두 개로 늘어난다.
    new_id = f"{entry['owner']}_{entry['code']}"
    if new_id != item.id:
        if new_id in my_port:
            return {"error": "같은 주인의 같은 종목이 이미 있습니다."}
        del my_port[item.id]
    my_port[new_id] = entry

    save_portfolio(my_port)
    set_cache(None)
    return {"status": "success", "id": new_id}


@app.post("/api/add")
def add_portfolio(item: AddItem):
    my_port = load_portfolio(force=True)
    key = f"{item.owner}_{item.code}"
    if key in my_port:
        return {"error": "이미 있는 종목입니다. 수정으로 바꿔주세요."}
    my_port[key] = {"owner": item.owner, "code": item.code,
                    "buy_price": item.buy_price, "qty": item.qty, "note": item.note}
    save_portfolio(my_port)
    set_cache(None)
    return {"status": "success"}


@app.post("/api/delete")
def delete_portfolio(item: DeleteItem):
    my_port = load_portfolio(force=True)
    if item.id in my_port:
        del my_port[item.id]
        save_portfolio(my_port)
        set_cache(None)
        return {"status": "success"}
    return {"error": "삭제할 종목이 없습니다."}


# ─── 실제 시세 가져오기 (병렬) ──────────────────────────
def yahoo_quote(symbol):
    """야후에서 현재가와 전일종가를 가져온다. (이름, 현재가, 등락%)

    예전에는 yfinance를 썼는데 두 가지가 문제였다. 하나는 느린 것 —
    yfinance는 값 하나 받으려고 쿠키를 먼저 받아오고 판다스 표를 만든다.
    다른 하나는 그 쿠키 절차가 야후 쪽 사정으로 자주 깨진다는 것. 실제로
    렌더에 올라간 채로 환율을 못 가져오고 있었다. 여기서 쓰는 주소는
    yfinance가 내부적으로 결국 부르는 그 주소이고, 인증 절차가 없다.
    """
    res = _session.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "5d", "interval": "1d"},
        headers=HEADERS_YF, timeout=10,
    )
    res.raise_for_status()
    result = res.json()["chart"]["result"][0]
    meta = result["meta"]

    # 휴장일은 종가가 비어 있어서(null) 걸러낸다
    closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    if price is None:
        raise DataUnavailable(f"{symbol}: 가격이 비어 있음")

    prev = closes[-2] if len(closes) >= 2 else meta.get("chartPreviousClose") or price
    change = (price - prev) / prev * 100 if prev else 0.0
    return meta.get("shortName") or symbol.upper(), float(price), change


def fetch_exchange_rate():
    return yahoo_quote("USDKRW=X")[1]

# 국내 종목코드(6자리)를 야후 심볼로 바꾸려면 .KS(코스피)/.KQ(코스닥)를 붙여야 하는데,
# 어느 쪽인지는 코드만 봐서 알 수 없다. 게다가 야후는 틀린 접미사에도 404를 내지 않고
# 엉뚱한 종목을 돌려준다 — 파마리서치(214450)를 .KS로 물으면 141,300원짜리 다른 게
# 나오고, 실제 393,000원은 .KQ에 있다. 그래서 절대 추측하지 않고 검색으로 확인한다.
# 심볼은 안 바뀌므로 한 번 찾으면 계속 재사용한다.
_symbol_cache = {}


def korean_symbol(code):
    """국내 6자리 코드 -> 야후 심볼(005930.KS). 못 찾으면 예외."""
    with _cache_lock:
        if code in _symbol_cache:
            return _symbol_cache[code]

    res = _session.get("https://query2.finance.yahoo.com/v1/finance/search",
                       params={"q": code, "quotesCount": 6, "newsCount": 0},
                       headers=HEADERS_YF, timeout=10)
    res.raise_for_status()
    for q in res.json().get("quotes", []):
        symbol = q.get("symbol") or ""
        if symbol.startswith(code + "."):
            with _cache_lock:
                _symbol_cache[code] = symbol
            return symbol
    raise DataUnavailable(f"{code}: 야후에서 종목을 못 찾았습니다")


# 야후는 국내 종목 이름을 영어로만 준다 ("HyundaiMtr(2PB)"). 화면에서 읽기 어려우니
# 한글 이름만 네이버 검색에서 가져온다. 시세용 주소와는 다른 곳이고, 이름은 안 바뀌므로
# 한 번만 부른다. 실패해도 영어 이름으로 넘어가면 그만이라 숫자에는 영향이 없다.
_name_cache = {}


def korean_name(code, fallback):
    with _cache_lock:
        if code in _name_cache:
            return _name_cache[code]
    name = fallback
    try:
        res = _session.get("https://m.stock.naver.com/front-api/search/autoComplete",
                           params={"query": code, "target": "stock"},
                           headers=HEADERS_YF, timeout=8)
        res.raise_for_status()
        for item in (res.json().get("result") or {}).get("items") or []:
            if (item.get("code") or "") == code and item.get("name"):
                name = item["name"]
                break
    except Exception as e:
        print(f"{code} 한글 이름 조회 실패(영문명으로 표시):", e)
    with _cache_lock:
        _name_cache[code] = name
    return name


def fetch_one_kr_stock(code):
    try:
        name, price, change = yahoo_quote(korean_symbol(code))
        return code, {"name": korean_name(code, name), "price": price, "change": change}
    except Exception as e:
        print(f"국내 주식 {code} 실패:", e)
    return code, None


def fetch_kr_indices():
    """코스피/코스닥 지수. 하나가 실패해도 나머지는 살린다."""
    out = {}
    for key, symbol in (("kospi", "^KS11"), ("kosdaq", "^KQ11")):
        try:
            _, price, change = yahoo_quote(symbol)
            out[key] = {"price": f"{price:,.2f}", "change": f"{change:.2f}"}
        except Exception as e:
            print(f"{key} 지수 실패:", e)
            out[key] = None
    return out


def fetch_one_us_stock(ticker):
    try:
        name, price, change = yahoo_quote(ticker)
        return ticker, {"name": name, "price": price, "change": change}
    except Exception as e:
        print(f"미국 주식 {ticker} 실패:", e)
    return ticker, None

# ─── 시세 조합 ───────────────────────────────────────────
def build_market_data():
    my_port = load_portfolio()
    kr_tickers = list(set(v["code"] for v in my_port.values() if v["code"].isdigit()))
    us_tickers = list(set(v["code"] for v in my_port.values() if not v["code"].isdigit()))

    price_map = {}
    kospi_info = None
    kosdaq_info = None
    usd_krw = None
    warnings = []

    # 종목 수만큼 한 번에 던진다. 10개씩 끊어 돌리면 두 번 기다리게 된다.
    everything = len(us_tickers) + len(kr_tickers) + 2
    with ThreadPoolExecutor(max_workers=max(4, everything)) as ex:
        futures = {}
        # 환율은 미국 주식이 있을 때만 필요하다. 국내 종목만 있는 날에
        # 환율 때문에 화면 전체가 막히면 곤란하다.
        if us_tickers: futures["fx"] = ex.submit(fetch_exchange_rate)
        futures["idx"] = ex.submit(fetch_kr_indices)
        for t in us_tickers: futures[f"us_{t}"] = ex.submit(fetch_one_us_stock, t)
        for c in kr_tickers: futures[f"kr_{c}"] = ex.submit(fetch_one_kr_stock, c)

        if "fx" in futures:
            usd_krw = futures["fx"].result()   # 실패하면 여기서 멈춘다 (아래 주석 참고)
        idx = futures["idx"].result()
        kospi_info, kosdaq_info = idx["kospi"], idx["kosdaq"]
        if kospi_info is None or kosdaq_info is None:
            warnings.append("지수를 못 받았습니다.")

        # 종목 하나가 빠지면 그 종목만 경고한다. 예전에는 국내 전체를 한 번에 받아서,
        # 응답이 조금만 어긋나도 8종목이 통째로 0원이 되고 경고도 안 떴다.
        for t in us_tickers + kr_tickers:
            ticker, info = futures[f"{'us' if t in us_tickers else 'kr'}_{t}"].result()
            if info: price_map[ticker] = info
            else:    warnings.append(f"{ticker} 시세를 못 받았습니다.")

    portfolio_list = []
    for pid, pdata in my_port.items():
        code = pdata["code"]
        owner = pdata["owner"]
        is_kr = code.isdigit()
        p_info = price_map.get(code, {"name": code, "price": 0.0, "change": 0.0})
        cp = p_info["price"]
        rate = 1.0 if is_kr else usd_krw

        buy_amount  = pdata["buy_price"] * pdata["qty"] * rate
        eval_amount = cp * pdata["qty"] * rate
        my_return   = (cp - pdata["buy_price"]) / pdata["buy_price"] * 100 if pdata["buy_price"] > 0 else 0.0

        portfolio_list.append({
            "id":              pid,
            "owner":           owner,
            "note":            pdata.get("note", ""),
            "type":            "KR" if is_kr else "US",
            "code":            code,
            "name":            p_info["name"],
            "qty":             pdata["qty"],
            "buy_price":       f"{int(pdata['buy_price']):,}원" if is_kr else f"${pdata['buy_price']:.2f}",
            "current_price":   f"{int(cp):,}원"                 if is_kr else f"${cp:.2f}",
            "buy_amount_raw":  buy_amount,
            "eval_amount_raw": eval_amount,
            "today_change":    f"{p_info['change']:.2f}",
            "my_return":       f"{my_return:.2f}",
        })

    return {
        "usd_krw":   f"{usd_krw:,.1f}" if usd_krw else "-",
        "kospi":     kospi_info,
        "kosdaq":    kosdaq_info,
        "portfolio": portfolio_list,
        "warnings":  warnings,
        "cached":    False,
    }

# ─── 메인 API ────────────────────────────────────────────
@app.get("/api/market")
def get_market_data():
    cached = get_cached()
    if cached:
        result = dict(cached)
        result["cached"] = True
        threading.Thread(target=_refresh_cache, daemon=True).start()
        return result
    try:
        data = build_market_data()
        set_cache(data)
        return data
    except Exception as e:
        return {"error": str(e)}

def _refresh_cache():
    with _cache_lock:
        age = time.time() - _cache["ts"]
        if age < CACHE_TTL * 0.5: return
        previous_ts = _cache["ts"]
        _cache["ts"] = time.time()  # Cache Stampede 방지
    try:
        data = build_market_data()
        set_cache(data)
    except Exception as e:
        # 갱신에 실패했으면 시계도 되돌린다. 안 되돌리면 낡은 값이 방금 받아온
        # 값인 척 계속 남고, 다음 방문자도 똑같이 갱신을 건너뛴다.
        with _cache_lock:
            if _cache["ts"] > previous_ts:
                _cache["ts"] = previous_ts
        print("캐시 갱신 실패:", e)

# ─── HTML ────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>조대표 패밀리 오피스</title>
<style>
:root {
    --bg: #0b0f19; --card: #151f32; --border: rgba(255,255,255,0.06);
    --accent: #6366f1; --up: #ef4444; --down: #3b82f6;
    --text: #f3f4f6; --muted: #9ca3af;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; padding: 24px; }

.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
.logo { font-size: 26px; font-weight: 800; background: linear-gradient(to right, #6366f1, #a855f7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.header-right { display: flex; align-items: center; gap: 12px; }
.update-badge { color: var(--muted); font-size: 12px; background: #1e293b; padding: 6px 14px; border-radius: 99px; }
.cache-badge { font-size: 11px; background: rgba(99,102,241,.15); color: #818cf8; padding: 3px 10px; border-radius: 99px; display: none; }

.tabs { display: flex; border-bottom: 2px solid var(--border); margin-bottom: 24px; overflow-x: auto; gap: 4px; }
.tab { padding: 11px 22px; cursor: pointer; color: var(--muted); font-weight: 600; font-size: 14px;
       border-bottom: 3px solid transparent; transition: all .25s; white-space: nowrap; user-select: none; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--text); border-bottom-color: var(--accent); }
.tab-panel { display: none; }
.tab-panel.active { display: block; animation: fadeUp .3s ease; }
@keyframes fadeUp { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }

.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-bottom: 22px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 18px 20px; }
.card-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px; color: var(--muted); margin-bottom: 8px; }
.card-value { font-size: 22px; font-weight: 700; }
.card-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }

.section-title { font-size: 15px; font-weight: 700; color: #e5e7eb; margin: 28px 0 12px; display: flex; align-items: center; gap: 8px; }
.table-wrap { width: 100%; overflow-x: auto; background: var(--card); border: 1px solid var(--border); border-radius: 14px; margin-bottom: 8px; }
table { width: 100%; border-collapse: collapse; text-align: right; min-width: 860px; }
th { background: rgba(255,255,255,0.025); color: var(--muted); font-size: 11px; font-weight: 600;
     text-transform: uppercase; letter-spacing: .4px; padding: 13px 16px; border-bottom: 1px solid var(--border); }
td { padding: 13px 16px; border-bottom: 1px solid var(--border); font-size: 13px; }
tr:last-child td { border-bottom: none; }
th:first-child, td:first-child { text-align: left; position: sticky; left: 0; background: var(--card); }
tfoot th, tfoot td { background: rgba(0,0,0,0.2); color: var(--text); font-weight: 700; font-size: 13px; }

.badge { display: inline-block; padding: 2px 7px; border-radius: 5px; font-size: 10px; font-weight: 700; margin-right: 6px; }
.badge-kr { background: rgba(59,130,246,.15); color: #60a5fa; }
.badge-us { background: rgba(234,179,8,.15);  color: #fde047; }
.up { color: var(--up); } .down { color: var(--down); }

.btn { border: none; padding: 5px 11px; border-radius: 7px; cursor: pointer; font-weight: 700; font-size: 12px; transition: all .2s; }
.btn-edit   { background: rgba(99,102,241,.15); color: #818cf8; }
.btn-edit:hover   { background: var(--accent); color: #fff; }
.btn-delete { background: rgba(239,68,68,.1); color: #f87171; margin-left: 4px; }
.btn-delete:hover { background: var(--up); color: #fff; }
.btn-add { background: linear-gradient(135deg,#6366f1,#7c3aed); color: #fff; padding: 9px 18px; border-radius: 9px; font-size: 13px; box-shadow: 0 4px 12px rgba(99,102,241,.3); }
.btn-report { background: rgba(255,255,255,.07); color: var(--text); padding: 9px 16px;
              border-radius: 9px; font-size: 13px; text-decoration: none; display: inline-block; }
.btn-report:hover { background: rgba(255,255,255,.14); }

/* 칸마다 붙는 연필 — 그 값 하나만 고친다 */
.pen { background: none; border: none; cursor: pointer; opacity: .35; font-size: 11px;
       padding: 0 2px; transition: opacity .15s; }
.pen:hover { opacity: 1; }
td:hover .pen { opacity: .8; }

/* 메모 — 매수·매도 시나리오를 적어두는 자리 */
.name-line { display: flex; align-items: center; gap: 2px; }
.sub { color: var(--muted); font-size: 11px; margin-left: 34px; font-family: 'JetBrains Mono', monospace; }
.memo { margin-top: 6px; font-size: 11.5px; line-height: 1.5; color: #a5b4fc; cursor: pointer;
        white-space: pre-wrap; word-break: keep-all; max-width: 30ch;
        border-left: 2px solid rgba(99,102,241,.4); padding: 2px 0 2px 8px; }
.memo:hover { color: #c7d2fe; }
.memo.empty { color: var(--muted); opacity: .45; border-left-color: transparent; font-style: italic; }

/* 메모 입력창 */
.modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 50;
         align-items: center; justify-content: center; padding: 20px; }
.modal-box { background: var(--card); border: 1px solid var(--border); border-radius: 16px;
             padding: 22px; width: 100%; max-width: 520px; }
.modal-box h3 { font-size: 15px; margin-bottom: 4px; }
.modal-box .hint { font-size: 11.5px; color: var(--muted); margin-bottom: 14px; }
.modal-box textarea { width: 100%; min-height: 190px; background: #0f172a; color: var(--text);
                      border: 1px solid var(--border); border-radius: 10px; padding: 12px;
                      font-size: 13px; line-height: 1.65; resize: vertical; font-family: inherit; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
.btn-ghost { background: rgba(255,255,255,.06); color: var(--muted); padding: 9px 16px; border-radius: 9px; }
.btn-save { background: var(--accent); color: #fff; padding: 9px 18px; border-radius: 9px; }

/* ── 모바일: 표를 카드로 바꾼다 ──────────────────────────
   가로 스크롤로 10칸을 보는 건 폰에서 못 쓴다. 한 종목을 한 장으로 묶고
   중요한 것(종목·수익률)을 첫 줄에, 나머지는 둘째 줄에 두 칸씩 접어 넣는다. */
@media (max-width: 767px) {
  body { padding: 14px; }
  .table-wrap { border: none; background: none; overflow: visible; }
  table { min-width: 0; display: block; }
  thead { display: none; }
  tbody { display: block; }
  tbody tr { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px;
             background: var(--card); border: 1px solid var(--border); border-radius: 12px;
             padding: 14px; margin-bottom: 10px; }
  tbody td { display: flex; justify-content: space-between; align-items: baseline;
             border: none; padding: 0; font-size: 12.5px; }
  tbody td::before { content: attr(data-label); color: var(--muted); font-size: 10.5px;
                     margin-right: 8px; white-space: nowrap; }
  /* 종목명과 수익률은 한 줄을 통째로 쓴다 */
  tbody td[data-label="종목"] { grid-column: 1 / -1; flex-direction: column; align-items: stretch; }
  tbody td[data-label="종목"]::before { display: none; }
  tbody td[data-label="수익률"] { grid-column: 1 / -1; justify-content: flex-end;
                                  border-top: 1px solid var(--border); padding-top: 8px; font-size: 15px; }
  tbody td[data-label="관리"] { grid-column: 1 / -1; justify-content: flex-end; }
  tbody td[data-label="관리"]::before { display: none; }
  .memo { max-width: none; }

  tfoot { display: block; }
  tfoot tr { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px;
             background: rgba(0,0,0,.25); border-radius: 12px; padding: 14px; }
  tfoot th, tfoot td { display: flex; justify-content: space-between; border: none; padding: 0; }
  tfoot th[colspan] { grid-column: 1 / -1; }
  .grid { grid-template-columns: 1fr 1fr; gap: 10px; }
  .card { padding: 14px; }
  .card-value { font-size: 17px; }
  .tab { padding: 9px 14px; font-size: 13px; }
  .logo { font-size: 20px; }
}

/* 숫자가 틀렸을 수 있다는 걸 화면에서 바로 보이게 한다 */
.alert { display: none; padding: 13px 18px; border-radius: 11px; margin-bottom: 20px; font-size: 13px; font-weight: 600; line-height: 1.6; }
.alert-error { background: rgba(239,68,68,.12); color: #fca5a5; border: 1px solid rgba(239,68,68,.3); }
.alert-warn  { background: rgba(234,179,8,.1);  color: #fde047; border: 1px solid rgba(234,179,8,.25); }
</style>
</head>
<body>

<div class="header">
    <div class="logo">👑 조대표 패밀리 오피스</div>
    <div class="header-right">
        <span class="cache-badge" id="cache-badge">⚡ 캐시 데이터</span>
        <a class="btn btn-report" href="/report" target="_blank" rel="noopener">📊 오늘 스크리너</a>
        <button class="btn btn-add" onclick="addStock()">➕ 자산 추가</button>
        <div class="update-badge" id="update-time">조회 중...</div>
    </div>
</div>

<div class="alert" id="alert-bar"></div>

<!-- 메모 — 매수·매도 시나리오를 적어두는 자리. 여러 줄을 써야 해서 따로 창을 띄운다 -->
<div class="modal" id="note-modal" onclick="if(event.target===this) closeNote()">
  <div class="modal-box">
    <h3 id="note-title"></h3>
    <div class="hint">매수·매도 시나리오를 적어두세요. 저장하면 표에 바로 보입니다.</div>
    <textarea id="note-text" placeholder="예)
매수: 12만원 아래로 눌리면 분할 1차
매도: 실적 발표 전 절반 익절
손절: 10만 5천원 종가 이탈
근거: 신규 수주 잔고가 계속 늘고 있음"></textarea>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeNote()">취소</button>
      <button class="btn btn-save" onclick="saveNote()">저장</button>
    </div>
  </div>
</div>

<div class="tabs">
    <div class="tab active" onclick="switchTab(this,'panel-total')">🏛️ 가족 통합 자산</div>
    <div class="tab"        onclick="switchTab(this,'panel-jo')">👨‍💼 조대표님 자산</div>
    <div class="tab"        onclick="switchTab(this,'panel-wife')">👸 공쥬님 자산</div>
</div>

<!-- 통합 탭 -->
<div id="panel-total" class="tab-panel active">
    <div class="grid">
        <div class="card">
            <div class="card-label">패밀리 총 평가금액</div>
            <div class="card-value" id="family-eval" style="color:#6366f1">-</div>
        </div>
        <div class="card">
            <div class="card-label">패밀리 통합 수익률</div>
            <div class="card-value" id="family-ret">-</div>
        </div>
        <div class="card">
            <div class="card-label">실시간 환율 (USD/KRW)</div>
            <div class="card-value" id="usd-text" style="color:#fde047">-</div>
        </div>
    </div>
    <div class="grid">
        <div class="card">
            <div class="card-label">KOSPI</div>
            <div class="card-value" id="kospi-val">-</div>
            <div class="card-sub"  id="kospi-chg"></div>
        </div>
        <div class="card">
            <div class="card-label">KOSDAQ</div>
            <div class="card-value" id="kosdaq-val">-</div>
            <div class="card-sub"  id="kosdaq-chg"></div>
        </div>
        <div class="card">
            <div class="card-label">👨‍💼 조대표님 평가금액</div>
            <div class="card-value" id="jo-total-eval">-</div>
            <div class="card-sub"  id="jo-total-ret"></div>
        </div>
        <div class="card">
            <div class="card-label">👸 공쥬님 평가금액</div>
            <div class="card-value" id="wife-total-eval">-</div>
            <div class="card-sub"  id="wife-total-ret"></div>
        </div>
    </div>
</div>

<!-- 조대표 탭 -->
<div id="panel-jo" class="tab-panel">
    <div class="section-title">🇰🇷 국내 주식</div>
    <div class="table-wrap"><table>
        <thead><tr><th>종목명</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>매입총액</th><th>평가금액</th><th>평가손익</th><th>오늘등락</th><th>수익률</th><th>관리</th></tr></thead>
        <tbody id="jo-kr-body"></tbody>
        <tfoot><tr><th colspan="5">국내 소계</th><td id="jo-kr-buy">-</td><td id="jo-kr-eval">-</td><td id="jo-kr-profit">-</td><td>-</td><td id="jo-kr-ret">-</td><td></td></tr></tfoot>
    </table></div>
    <div class="section-title">🇺🇸 해외 주식</div>
    <div class="table-wrap"><table>
        <thead><tr><th>종목명</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>매입총액(원화)</th><th>평가금액(원화)</th><th>평가손익(원화)</th><th>오늘등락</th><th>수익률</th><th>관리</th></tr></thead>
        <tbody id="jo-us-body"></tbody>
        <tfoot><tr><th colspan="5">해외 소계</th><td id="jo-us-buy">-</td><td id="jo-us-eval">-</td><td id="jo-us-profit">-</td><td>-</td><td id="jo-us-ret">-</td><td></td></tr></tfoot>
    </table></div>
</div>

<!-- 공쥬님 탭 -->
<div id="panel-wife" class="tab-panel">
    <div class="section-title">🇰🇷 국내 주식</div>
    <div class="table-wrap"><table>
        <thead><tr><th>종목명</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>매입총액</th><th>평가금액</th><th>평가손익</th><th>오늘등락</th><th>수익률</th><th>관리</th></tr></thead>
        <tbody id="wife-kr-body"></tbody>
        <tfoot><tr><th colspan="5">국내 소계</th><td id="wife-kr-buy">-</td><td id="wife-kr-eval">-</td><td id="wife-kr-profit">-</td><td>-</td><td id="wife-kr-ret">-</td><td></td></tr></tfoot>
    </table></div>
    <div class="section-title">🇺🇸 해외 주식</div>
    <div class="table-wrap"><table>
        <thead><tr><th>종목명</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>매입총액(원화)</th><th>평가금액(원화)</th><th>평가손익(원화)</th><th>오늘등락</th><th>수익률</th><th>관리</th></tr></thead>
        <tbody id="wife-us-body"></tbody>
        <tfoot><tr><th colspan="5">해외 소계</th><td id="wife-us-buy">-</td><td id="wife-us-eval">-</td><td id="wife-us-profit">-</td><td>-</td><td id="wife-us-ret">-</td><td></td></tr></tfoot>
    </table></div>
</div>

<script>
let G = [];

function switchTab(el, panelId) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    document.getElementById(panelId).classList.add('active');
}

const fmt  = n => Math.round(n).toLocaleString();
/* 수량은 소수점 매수가 가능하다. 37주는 '37주'로, 0.5주는 '0.5주'로 보여준다
   (정수까지 '37.0000주'로 쓰면 눈이 피곤하다) */
const fmtQty = n => Number.isInteger(n)
    ? n.toLocaleString()
    : n.toLocaleString(undefined, {maximumFractionDigits: 6});
const sign = n => n > 0 ? '+' : '';
const cls  = n => n > 0 ? 'up' : (n < 0 ? 'down' : '');

function updateDashboard() {
    fetch('/api/market').then(r => r.json()).then(data => {
        const bar = document.getElementById('alert-bar');

        // 실패했으면 낡은 화면을 그대로 두지 않는다. 예전에는 콘솔에만 찍혀서
        // 자료를 못 받아온 줄 모르고 옛날 숫자를 보고 있었다.
        if (data.error) {
            bar.className = 'alert alert-error';
            bar.style.display = 'block';
            bar.innerText = '⚠️ 자료를 못 불러왔습니다. 아래 숫자는 믿지 마세요.\\n' + data.error;
            return;
        }
        const warns = data.warnings || [];
        if (warns.length) {
            bar.className = 'alert alert-warn';
            bar.style.display = 'block';
            bar.innerText = '⚠️ 일부 시세를 못 받았습니다 — ' + warns.join(' / ');
        } else {
            bar.style.display = 'none';
        }

        const badge = document.getElementById('cache-badge');
        badge.style.display = data.cached ? 'inline-block' : 'none';

        document.getElementById('update-time').innerText =
            (data.cached ? '📦 ' : '🔄 ') + '갱신: ' + new Date().toLocaleTimeString();
        document.getElementById('usd-text').innerText = data.usd_krw + ' 원';

        if (data.kospi) {
            const c = parseFloat(data.kospi.change);
            document.getElementById('kospi-val').innerText = data.kospi.price;
            document.getElementById('kospi-chg').innerText = sign(c) + data.kospi.change + '%';
            document.getElementById('kospi-chg').className = 'card-sub ' + cls(c);
        }
        if (data.kosdaq) {
            const c = parseFloat(data.kosdaq.change);
            document.getElementById('kosdaq-val').innerText = data.kosdaq.price;
            document.getElementById('kosdaq-chg').innerText = sign(c) + data.kosdaq.change + '%';
            document.getElementById('kosdaq-chg').className = 'card-sub ' + cls(c);
        }

        G = data.portfolio;
        
        renderOwner('조대표', 'jo');
        renderOwner('공쥬님', 'wife');

        let fb = 0, fe = 0;
        G.forEach(s => { fb += s.buy_amount_raw; fe += s.eval_amount_raw; });
        const fr = fb > 0 ? (fe - fb) / fb * 100 : 0;
        const fProfit = fe - fb;
        
        document.getElementById('family-eval').innerText = fmt(fe) + '원';
        const frEl = document.getElementById('family-ret');
        frEl.innerText   = sign(fr) + fr.toFixed(2) + '% (' + sign(fProfit) + fmt(Math.abs(fProfit)) + '원)';
        frEl.className   = 'card-value ' + cls(fr);
    });
}

function renderOwner(owner, prefix) {
    const stocks = G.filter(s => s.owner === owner);
    let ob=0, oe=0, krb=0, kre=0, usb=0, use_=0;
    
    stocks.forEach(s => {
        ob += s.buy_amount_raw; oe += s.eval_amount_raw;
        if(s.type === 'KR'){ krb += s.buy_amount_raw; kre += s.eval_amount_raw; }
        else               { usb += s.buy_amount_raw; use_+= s.eval_amount_raw; }
    });

    /* 비중 큰 것부터 위로. 비중은 평가금액의 비율이라 평가금액 순이 곧 비중 순이다.
       예전에는 장부에 넣은 순서 그대로라 뒤죽박죽이었다. */
    const byWeight = (a, b) => b.eval_amount_raw - a.eval_amount_raw;
    document.getElementById(prefix + '-kr-body').innerHTML =
        stocks.filter(s=>s.type==='KR').sort(byWeight).map(s=>makeRow(s,kre)).join('');
    document.getElementById(prefix + '-us-body').innerHTML =
        stocks.filter(s=>s.type==='US').sort(byWeight).map(s=>makeRow(s,use_)).join('');

    const ownerRet = ob > 0 ? (oe - ob) / ob * 100 : 0;
    setText(prefix + '-total-eval', fmt(oe) + '원');
    setText(prefix + '-total-ret', '수익률 ' + sign(ownerRet) + ownerRet.toFixed(2) + '%', cls(ownerRet));

    const krRet = krb > 0 ? (kre - krb) / krb * 100 : 0;
    const krProfit = kre - krb;
    setText(prefix + '-kr-buy', fmt(krb) + '원');
    setText(prefix + '-kr-eval', fmt(kre) + '원');
    setText(prefix + '-kr-profit', sign(krProfit) + fmt(Math.abs(krProfit)) + '원', cls(krProfit));
    setText(prefix + '-kr-ret', sign(krRet) + krRet.toFixed(2) + '%', cls(krRet));

    const usRet = usb > 0 ? (use_ - usb) / usb * 100 : 0;
    const usProfit = use_ - usb;
    setText(prefix + '-us-buy', fmt(usb) + '원');
    setText(prefix + '-us-eval', fmt(use_) + '원');
    setText(prefix + '-us-profit', sign(usProfit) + fmt(Math.abs(usProfit)) + '원', cls(usProfit));
    setText(prefix + '-us-ret', sign(usRet) + usRet.toFixed(2) + '%', cls(usRet));
}

function makeRow(s, groupEval) {
    const w  = groupEval > 0 ? (s.eval_amount_raw / groupEval * 100).toFixed(1) : '0.0';
    const profit = s.eval_amount_raw - s.buy_amount_raw;
    const td = parseFloat(s.today_change);
    const mr = parseFloat(s.my_return);
    const b  = s.type === 'KR'
        ? '<span class="badge badge-kr">국내</span>'
        : '<span class="badge badge-us">해외</span>';
    const unit = s.type === 'KR' ? '원' : '$';

    /* 칸마다 연필을 붙여서 그 값 하나만 고치게 한다.
       예전에는 수정을 누르면 주인·종목·단가·수량을 순서대로 다 다시 넣어야 했다.

       값을 onclick 안에 직접 적지 않는다. 따옴표가 든 값이 오면 속성이 거기서 끊겨
       버튼이 아예 안 눌린다(실제로 그랬다). data-로 넘기고 클릭은 아래에서 한 번에 받는다. */
    const pencil = (field, label, value) =>
        `<button class="pen" title="${label} 수정" data-edit="${field}"
                 data-label="${esc(label)}" data-value="${esc(value)}">✏️</button>`;

    const noteText = (s.note || '').trim();

    return `<tr data-id="${s.id}">
        <td data-label="종목">
            <div class="name-line">${b}<strong>${s.name}</strong></div>
            <div class="sub">${s.code}</div>
            <div class="memo ${noteText ? '' : 'empty'}" onclick="editNote('${s.id}')"
                 title="눌러서 메모 쓰기">${noteText ? esc(noteText) : '＋ 메모'}</div>
        </td>
        <td data-label="보유수량" style="font-weight:600">${fmtQty(s.qty)}주 ${pencil('qty','보유수량',s.qty)}</td>
        <td data-label="비중" style="color:var(--muted)">${w}%</td>
        <td data-label="매입단가">${s.buy_price} ${pencil('buy_price','매입단가',String(s.buy_price).replace(/[원$,]/g,''))}</td>
        <td data-label="현재가"><strong>${s.current_price}</strong></td>
        <td data-label="매입총액" style="font-weight:600;color:var(--muted)">${fmt(s.buy_amount_raw)}원</td>
        <td data-label="평가금액" style="font-weight:700">${fmt(s.eval_amount_raw)}원</td>
        <td data-label="평가손익" class="${cls(profit)}" style="font-weight:700">${sign(profit)}${fmt(Math.abs(profit))}원</td>
        <td data-label="오늘등락" class="${cls(td)}">${sign(td)}${s.today_change}%</td>
        <td data-label="수익률" class="${cls(mr)}" style="font-size:14px"><strong>${sign(mr)}${s.my_return}%</strong></td>
        <td data-label="관리">
            <button class="btn btn-delete" onclick="deleteStock('${s.id}','${s.name}')">❌</button>
        </td>
    </tr>`;
}

/* 연필·삭제 클릭을 문서 한 곳에서 받는다.
   표는 갱신될 때마다 새로 그려지는데, 버튼마다 따로 달면 그때마다 다시 달아야 한다. */
document.addEventListener('click', (ev) => {
    const pen = ev.target.closest('.pen');
    if (!pen) return;
    const tr = pen.closest('tr');
    if (!tr) return;
    editField(tr.dataset.id, pen.dataset.edit, pen.dataset.label, pen.dataset.value);
});

function esc(t) {
    return String(t).replace(/[&<>"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
}

function setText(id, text, extraClass) {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerText = text;
    if (extraClass !== undefined) el.className = extraClass;
}

/* 보낸 항목만 바뀌고 나머지는 그대로 둔다 (서버가 합쳐준다) */
function save(patch) {
    return fetch('/api/update', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(patch)
    }).then(r => r.json()).then(r => {
        if (r.error) { alert(r.error); return; }
        updateDashboard();
    }).catch(e => alert('저장 실패: ' + e));
}

function editField(id, field, label, current) {
    const v = prompt(`${label} 입력:`, current);
    if (v === null) return;
    const num = parseFloat(String(v).replace(/[,원$\\s]/g, ''));
    if (isNaN(num)) return alert('숫자만 입력해주세요.');
    save({id, [field]: num});
}

/* 메모 — 매수·매도 시나리오를 적어두는 자리.
   여러 줄을 쓸 수 있어야 해서 prompt 대신 화면 위에 창을 띄운다. */
let noteTarget = null;

function editNote(id) {
    const s = G.find(x => x.id === id);
    if (!s) return;
    noteTarget = id;
    document.getElementById('note-title').innerText = `${s.name} (${s.code})`;
    const box = document.getElementById('note-text');
    box.value = s.note || '';
    document.getElementById('note-modal').style.display = 'flex';
    box.focus();
}

function closeNote() {
    document.getElementById('note-modal').style.display = 'none';
    noteTarget = null;
}

function saveNote() {
    const text = document.getElementById('note-text').value;
    const id = noteTarget;
    closeNote();
    save({id, note: text});
}

function addStock() {
    const o = prompt('누구의 자산인가요?\\n1: 조대표\\n2: 공쥬님','1'); if(!o) return;
    const owner = o.trim()==='2' ? '공쥬님' : '조대표';
    const c = prompt('종목코드 6자리 또는 미국 티커 (예: AAPL, TSLA):'); if(!c) return;
    const p = prompt('매입단가 (원화 또는 달러 숫자만):','100');          if(!p) return;
    const q = prompt('보유수량:','10');                                    if(!q) return;
    const buy = parseFloat(String(p).replace(/[,원$\\s]/g,''));
    const qty = parseFloat(String(q).replace(/[,주\\s]/g,''));
    if (isNaN(buy) || isNaN(qty)) return alert('단가와 수량은 숫자로 입력해주세요.');
    fetch('/api/add', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({owner, code:c.trim().toUpperCase(), buy_price:buy, qty})
    }).then(r=>r.json()).then(r=>{
        if (r.error) return alert(r.error);
        updateDashboard();
    });
}

function deleteStock(id, name) {
    if(!confirm(`[${name}]\\n정말 삭제하시겠습니까?`)) return;
    fetch('/api/delete', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id})
    }).then(()=>updateDashboard());
}

updateDashboard();
setInterval(updateDashboard, 300000);
document.addEventListener('visibilitychange', ()=>{
    if(document.visibilityState==='visible') updateDashboard();
});
</script>
</body>
</html>"""

# ─── 스크리너 리포트 ─────────────────────────────────────
# 스크리너가 매일 data 브랜치에 올려두는 리포트를 그대로 보여준다.
#
# 왜 여기서 중계하느냐 — 깃허브 raw 주소는 HTML을 '글자'로 내려줘서 브라우저가
# 화면으로 그리지 않고 소스 코드를 보여준다. 여기를 거치면 제대로 열린다.
# 덕분에 아이폰에서 리포트를 못 보던 문제도 풀린다(로컬 HTML 파일을 못 열어서
# 여태 카드 이미지로만 봤다).
REPORT_URL = ("https://raw.githubusercontent.com/woongjoe09626/"
              "woong-stock-dashboard/data/report-us.html")
_report_cache = {"html": None, "ts": 0}
REPORT_TTL = 300


@app.get("/report", response_class=HTMLResponse)
def get_report():
    with _cache_lock:
        if _report_cache["html"] and time.time() - _report_cache["ts"] < REPORT_TTL:
            return _report_cache["html"]
    try:
        res = _session.get(REPORT_URL, headers=HEADERS_YF, timeout=15)
        res.raise_for_status()
        res.encoding = "utf-8"
        html = res.text
    except Exception as e:
        # 조용히 빈 화면을 주지 않는다. 왜 안 나오는지 화면에 적는다.
        return HTMLResponse(
            "<body style='background:#0b0f19;color:#f3f4f6;font-family:sans-serif;padding:40px'>"
            f"<h2>리포트를 못 불러왔습니다</h2><p>{e}</p>"
            "<p>스크리너를 아직 안 돌렸거나 올리기가 실패했을 수 있습니다.</p></body>",
            status_code=503)
    with _cache_lock:
        _report_cache["html"] = html
        _report_cache["ts"] = time.time()
    return html


@app.get("/", response_class=HTMLResponse)
def get_dashboard_html():
    return HTML
