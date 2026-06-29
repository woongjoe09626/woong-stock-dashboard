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
import yfinance as yf

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

HEADERS_NAV = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.naver.com/",
}

DEFAULT_PORTFOLIO = {}

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
def load_portfolio():
    try:
        res = requests.get(f"{GOOGLE_SHEET_URL}?action=get", timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data: return data
    except Exception as e:
        print("구글 시트 불러오기 실패:", e)
    return DEFAULT_PORTFOLIO

def save_portfolio(data):
    try:
        params = {"action": "set", "data": json.dumps(data)}
        requests.get(GOOGLE_SHEET_URL, params=params, timeout=10)
    except Exception as e:
        print("구글 시트 저장 실패:", e)

# ─── API 모델 ────────────────────────────────────────────
class UpdateItem(BaseModel):
    id: str
    owner: str
    code: str
    buy_price: float
    qty: int

class DeleteItem(BaseModel):
    id: str

# ─── 포트폴리오 CRUD ─────────────────────────────────────
@app.post("/api/update")
def update_portfolio(item: UpdateItem):
    my_port = load_portfolio()
    if item.id in my_port:
        my_port[item.id] = {"owner": item.owner, "code": item.code,
                            "buy_price": item.buy_price, "qty": item.qty}
        save_portfolio(my_port)
        set_cache(None)
        return {"status": "success"}
    return {"error": "종목을 찾을 수 없습니다."}

@app.post("/api/add")
def add_portfolio(item: UpdateItem):
    my_port = load_portfolio()
    my_port[f"{item.owner}_{item.code}"] = {
        "owner": item.owner, "code": item.code,
        "buy_price": item.buy_price, "qty": item.qty
    }
    save_portfolio(my_port)
    set_cache(None)
    return {"status": "success"}

@app.post("/api/delete")
def delete_portfolio(item: DeleteItem):
    my_port = load_portfolio()
    if item.id in my_port:
        del my_port[item.id]
        save_portfolio(my_port)
        set_cache(None)
        return {"status": "success"}
    return {"error": "삭제할 종목이 없습니다."}

# ─── 실제 시세 가져오기 (병렬) ──────────────────────────
def fetch_exchange_rate():
    try:
        fx = yf.Ticker("USDKRW=X").history(period="1d")
        if not fx.empty: return float(fx["Close"].iloc[-1])
    except: pass
    return 1380.0

def fetch_kr_stocks(kr_tickers):
    price_map = {}
    kospi_info  = {"price": "0.00", "change": "0.00"}
    kosdaq_info = {"price": "0.00", "change": "0.00"}
    try:
        query = f"SERVICE_INDEX:KOSPI,KOSDAQ|SERVICE_ITEM:{','.join(kr_tickers)}"
        res = requests.get("https://polling.finance.naver.com/api/realtime",
                           headers=HEADERS_NAV, params={"query": query}, timeout=5)
        areas = res.json()["result"]["areas"]
        idx = areas[0]["datas"]

        kp_val = int(idx[0]["nv"]) / 100
        kp_chg = int(idx[0]["cv"]) / 100
        kp_pct = kp_chg / (kp_val - kp_chg) * 100 if (kp_val - kp_chg) else 0
        kd_val = int(idx[1]["nv"]) / 100
        kd_chg = int(idx[1]["cv"]) / 100
        kd_pct = kd_chg / (kd_val - kd_chg) * 100 if (kd_val - kd_chg) else 0

        kospi_info  = {"price": f"{kp_val:,.2f}", "change": f"{kp_pct:.2f}"}
        kosdaq_info = {"price": f"{kd_val:,.2f}", "change": f"{kd_pct:.2f}"}

        if len(areas) > 1:
            for item in areas[1]["datas"]:
                price_map[item["cd"]] = {
                    "name":   item.get("nm", "이름없음"),
                    "price":  float(item["nv"]),
                    "change": float(item["cr"]),
                }
    except Exception as e:
        print("네이버 조회 실패:", e)
    return price_map, kospi_info, kosdaq_info

def fetch_one_us_stock(ticker):
    try:
        hist = yf.Ticker(ticker).history(period="2d")
        if len(hist) >= 1:
            cp = float(hist["Close"].iloc[-1])
            pc = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else cp
            return ticker, {
                "name":   ticker.upper(),
                "price":  cp,
                "change": (cp - pc) / pc * 100 if pc else 0,
            }
    except Exception as e:
        print(f"미국 주식 {ticker} 실패:", e)
    return ticker, None

# ─── 시세 조합 ───────────────────────────────────────────
def build_market_data():
    my_port = load_portfolio()
    kr_tickers = list(set(v["code"] for v in my_port.values() if v["code"].isdigit()))
    us_tickers = list(set(v["code"] for v in my_port.values() if not v["code"].isdigit()))

    price_map = {}
    kospi_info = {"price": "0.00", "change": "0.00"}
    kosdaq_info = {"price": "0.00", "change": "0.00"}
    usd_krw = 1380.0

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {}
        futures["fx"] = ex.submit(fetch_exchange_rate)
        if kr_tickers: futures["kr"] = ex.submit(fetch_kr_stocks, kr_tickers)
        for t in us_tickers: futures[f"us_{t}"] = ex.submit(fetch_one_us_stock, t)

        usd_krw = futures["fx"].result()
        if "kr" in futures:
            pm, ki, kdi = futures["kr"].result()
            price_map.update(pm)
            kospi_info  = ki
            kosdaq_info = kdi

        for t in us_tickers:
            ticker, info = futures[f"us_{t}"].result()
            if info: price_map[ticker] = info

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
        "usd_krw":   f"{usd_krw:,.1f}",
        "kospi":     kospi_info,
        "kosdaq":    kosdaq_info,
        "portfolio": portfolio_list,
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
        _cache["ts"] = time.time()  # Cache Stampede 방지
    try:
        data = build_market_data()
        set_cache(data)
    except:
        pass

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
</style>
</head>
<body>

<div class="header">
    <div class="logo">👑 조대표 패밀리 오피스</div>
    <div class="header-right">
        <span class="cache-badge" id="cache-badge">⚡ 캐시 데이터</span>
        <button class="btn btn-add" onclick="addStock()">➕ 자산 추가</button>
        <div class="update-badge" id="update-time">조회 중...</div>
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
        <thead><tr><th>종목명</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>평가금액</th><th>평가손익</th><th>오늘등락</th><th>수익률</th><th>관리</th></tr></thead>
        <tbody id="jo-kr-body"></tbody>
        <tfoot><tr><th colspan="5">국내 소계</th><td id="jo-kr-eval">-</td><td id="jo-kr-profit">-</td><td>-</td><td id="jo-kr-ret">-</td><td></td></tr></tfoot>
    </table></div>
    <div class="section-title">🇺🇸 해외 주식</div>
    <div class="table-wrap"><table>
        <thead><tr><th>종목명</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>평가금액(원화)</th><th>평가손익(원화)</th><th>오늘등락</th><th>수익률</th><th>관리</th></tr></thead>
        <tbody id="jo-us-body"></tbody>
        <tfoot><tr><th colspan="5">해외 소계</th><td id="jo-us-eval">-</td><td id="jo-us-profit">-</td><td>-</td><td id="jo-us-ret">-</td><td></td></tr></tfoot>
    </table></div>
</div>

<!-- 공쥬님 탭 -->
<div id="panel-wife" class="tab-panel">
    <div class="section-title">🇰🇷 국내 주식</div>
    <div class="table-wrap"><table>
        <thead><tr><th>종목명</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>평가금액</th><th>평가손익</th><th>오늘등락</th><th>수익률</th><th>관리</th></tr></thead>
        <tbody id="wife-kr-body"></tbody>
        <tfoot><tr><th colspan="5">국내 소계</th><td id="wife-kr-eval">-</td><td id="wife-kr-profit">-</td><td>-</td><td id="wife-kr-ret">-</td><td></td></tr></tfoot>
    </table></div>
    <div class="section-title">🇺🇸 해외 주식</div>
    <div class="table-wrap"><table>
        <thead><tr><th>종목명</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>평가금액(원화)</th><th>평가손익(원화)</th><th>오늘등락</th><th>수익률</th><th>관리</th></tr></thead>
        <tbody id="wife-us-body"></tbody>
        <tfoot><tr><th colspan="5">해외 소계</th><td id="wife-us-eval">-</td><td id="wife-us-profit">-</td><td>-</td><td id="wife-us-ret">-</td><td></td></tr></tfoot>
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
const sign = n => n > 0 ? '+' : '';
const cls  = n => n > 0 ? 'up' : (n < 0 ? 'down' : '');

function updateDashboard() {
    fetch('/api/market').then(r => r.json()).then(data => {
        if (data.error) { console.error(data.error); return; }

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

    document.getElementById(prefix + '-kr-body').innerHTML = stocks.filter(s=>s.type==='KR').map(s=>makeRow(s,kre)).join('');
    document.getElementById(prefix + '-us-body').innerHTML = stocks.filter(s=>s.type==='US').map(s=>makeRow(s,use_)).join('');

    const ownerRet = ob > 0 ? (oe - ob) / ob * 100 : 0;
    setText(prefix + '-total-eval', fmt(oe) + '원');
    setText(prefix + '-total-ret', '수익률 ' + sign(ownerRet) + ownerRet.toFixed(2) + '%', cls(ownerRet));

    const krRet = krb > 0 ? (kre - krb) / krb * 100 : 0;
    const krProfit = kre - krb;
    setText(prefix + '-kr-eval', fmt(kre) + '원');
    setText(prefix + '-kr-profit', sign(krProfit) + fmt(Math.abs(krProfit)) + '원', cls(krProfit));
    setText(prefix + '-kr-ret', sign(krRet) + krRet.toFixed(2) + '%', cls(krRet));

    const usRet = usb > 0 ? (use_ - usb) / usb * 100 : 0;
    const usProfit = use_ - usb;
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
        
    return `<tr>
        <td>${b}<strong>${s.name}</strong><br>
            <small style="color:var(--muted);margin-left:34px">${s.code}</small></td>
        <td style="font-weight:600">${s.qty.toLocaleString()}주</td>
        <td style="color:var(--muted)">${w}%</td>
        <td>${s.buy_price}</td>
        <td><strong>${s.current_price}</strong></td>
        <td style="font-weight:700">${fmt(s.eval_amount_raw)}원</td>
        <td class="${cls(profit)}" style="font-weight:700">${sign(profit)}${fmt(Math.abs(profit))}원</td>
        <td class="${cls(td)}">${sign(td)}${s.today_change}%</td>
        <td class="${cls(mr)}" style="font-size:14px"><strong>${sign(mr)}${s.my_return}%</strong></td>
        <td>
            <button class="btn btn-edit"
                onclick="editStock('${s.id}','${s.owner}','${s.code}','${s.name}','${s.buy_price}',${s.qty})">수정</button>
            <button class="btn btn-delete"
                onclick="deleteStock('${s.id}','${s.name}')">❌</button>
        </td>
    </tr>`;
}

function setText(id, text, extraClass) {
    const el = document.getElementById(id);
    el.innerText = text;
    if (extraClass !== undefined) el.className = extraClass;
}

function editStock(id, owner, code, name, price, qty) {
    const clean = price.replace(/[원$,]/g, '');
    const p = prompt(`[${owner} — ${name}]\\n새로운 매입단가 (숫자만):`, clean); if(!p) return;
    const q = prompt(`[${owner} — ${name}]\\n새로운 보유수량:`, qty);            if(!q) return;
    fetch('/api/update', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id, owner, code, buy_price:parseFloat(p), qty:parseInt(q)})
    }).then(()=>updateDashboard());
}

function addStock() {
    const o = prompt('누구의 자산인가요?\\n1: 조대표\\n2: 공쥬님','1'); if(!o) return;
    const owner = o.trim()==='2' ? '공쥬님' : '조대표';
    const c = prompt('종목코드 6자리 또는 미국 티커 (예: AAPL, TSLA):'); if(!c) return;
    const p = prompt('매입단가 (원화 또는 달러 숫자만):','100');          if(!p) return;
    const q = prompt('보유수량:','10');                                    if(!q) return;
    fetch('/api/add', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id:'', owner, code:c.trim().toUpperCase(), buy_price:parseFloat(p), qty:parseInt(q)})
    }).then(()=>updateDashboard());
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

@app.get("/", response_class=HTMLResponse)
def get_dashboard_html():
    return HTML
