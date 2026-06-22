import requests
import json
import os
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

GOOGLE_SHEET_URL = os.environ.get("MY_GOOGLE_SHEET_URL", "여기에_URL_붙여넣기")

HEADERS_NAV = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.naver.com/",
}

DEFAULT_PORTFOLIO = {
    "조대표_005387": {"owner": "조대표", "code": "005387", "buy_price": 100000, "qty": 50},
    "조대표_TSLA":   {"owner": "조대표", "code": "TSLA",   "buy_price": 200.0,  "qty": 10},
    "사모님_005930": {"owner": "사모님", "code": "005930", "buy_price": 70000,  "qty": 100},
    "사모님_AAPL":   {"owner": "사모님", "code": "AAPL",   "buy_price": 150.0,  "qty": 20},
}

def load_portfolio():
    try:
        res = requests.get(f"{GOOGLE_SHEET_URL}?action=get", timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data:
                return data
    except Exception as e:
        print("구글 시트 불러오기 실패:", e)
    return DEFAULT_PORTFOLIO

def save_portfolio(data):
    try:
        params = {"action": "set", "data": json.dumps(data)}
        requests.get(GOOGLE_SHEET_URL, params=params, timeout=10)
    except Exception as e:
        print("구글 시트 저장 실패:", e)

class UpdateItem(BaseModel):
    id: str
    owner: str
    code: str
    buy_price: float
    qty: int

class DeleteItem(BaseModel):
    id: str

@app.post("/api/update")
def update_portfolio(item: UpdateItem):
    my_port = load_portfolio()
    if item.id in my_port:
        my_port[item.id] = {"owner": item.owner, "code": item.code, "buy_price": item.buy_price, "qty": item.qty}
        save_portfolio(my_port)
        return {"status": "success"}
    return {"error": "종목을 찾을 수 없습니다."}

@app.post("/api/add")
def add_portfolio(item: UpdateItem):
    my_port = load_portfolio()
    new_id = f"{item.owner}_{item.code}"
    my_port[new_id] = {"owner": item.owner, "code": item.code, "buy_price": item.buy_price, "qty": item.qty}
    save_portfolio(my_port)
    return {"status": "success"}

@app.post("/api/delete")
def delete_portfolio(item: DeleteItem):
    my_port = load_portfolio()
    if item.id in my_port:
        del my_port[item.id]
        save_portfolio(my_port)
        return {"status": "success"}
    return {"error": "삭제할 종목이 없습니다."}

@app.get("/api/market")
def get_market_data():
    try:
        my_port = load_portfolio()

        # 1. 환율
        usd_krw = 1380.0
        try:
            fx = yf.Ticker("USDKRW=X").history(period="1d")
            if not fx.empty:
                usd_krw = float(fx["Close"].iloc[-1])
        except:
            pass

        kr_tickers = list(set(v["code"] for v in my_port.values() if v["code"].isdigit()))
        us_tickers = list(set(v["code"] for v in my_port.values() if not v["code"].isdigit()))

        price_map = {}
        kospi_info = {"price": "0.00", "change": "0.00"}
        kosdaq_info = {"price": "0.00", "change": "0.00"}

        # 2. 네이버 API — 한국 주식 + 지수
        if kr_tickers:
            query = f"SERVICE_INDEX:KOSPI,KOSDAQ|SERVICE_ITEM:{','.join(kr_tickers)}"
            res = requests.get(
                "https://polling.finance.naver.com/api/realtime",
                headers=HEADERS_NAV,
                params={"query": query},
                timeout=5,
            )
            areas = res.json()["result"]["areas"]

            # 지수 파싱 (항상 areas[0])
            idx = areas[0]["datas"]
            kospi_val    = int(idx[0]["nv"]) / 100
            kospi_chg    = int(idx[0]["cv"]) / 100
            kospi_pct    = kospi_chg / (kospi_val - kospi_chg) * 100 if (kospi_val - kospi_chg) != 0 else 0
            kosdaq_val   = int(idx[1]["nv"]) / 100
            kosdaq_chg   = int(idx[1]["cv"]) / 100
            kosdaq_pct   = kosdaq_chg / (kosdaq_val - kosdaq_chg) * 100 if (kosdaq_val - kosdaq_chg) != 0 else 0

            kospi_info  = {"price": f"{kospi_val:,.2f}",  "change": f"{kospi_pct:.2f}"}
            kosdaq_info = {"price": f"{kosdaq_val:,.2f}", "change": f"{kosdaq_pct:.2f}"}

            # 한국 종목 파싱
            if len(areas) > 1:
                for item in areas[1]["datas"]:
                    price_map[item["cd"]] = {
                        "name":   item.get("nm", "이름없음"),
                        "price":  float(item["nv"]),
                        "change": float(item["cr"]),
                    }

        # 3. Yahoo Finance — 미국 주식
        for ticker in us_tickers:
            try:
                yf_obj = yf.Ticker(ticker)
                hist   = yf_obj.history(period="2d")
                info   = yf_obj.info
                name   = info.get("longName") or info.get("shortName") or ticker.upper()
                if len(hist) >= 1:
                    cp = float(hist["Close"].iloc[-1])
                    pc = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else cp
                    price_map[ticker] = {
                        "name":   name,
                        "price":  cp,
                        "change": ((cp - pc) / pc) * 100 if pc else 0,
                    }
            except Exception as e:
                print(f"미국 주식 {ticker} 조회 실패:", e)

        # 4. 포트폴리오 병합
        portfolio_list = []
        for pid, pdata in my_port.items():
            code    = pdata["code"]
            owner   = pdata["owner"]
            is_kr   = code.isdigit()
            p_info  = price_map.get(code, {"name": code, "price": 0.0, "change": 0.0})
            cp      = p_info["price"]
            rate    = 1.0 if is_kr else usd_krw

            buy_amount  = pdata["buy_price"] * pdata["qty"] * rate
            eval_amount = cp * pdata["qty"] * rate
            my_return   = ((cp - pdata["buy_price"]) / pdata["buy_price"]) * 100 if pdata["buy_price"] > 0 else 0.0

            b_price_str = f"{int(pdata['buy_price']):,}원" if is_kr else f"${pdata['buy_price']:.2f}"
            c_price_str = f"{int(cp):,}원"                 if is_kr else f"${cp:.2f}"

            portfolio_list.append({
                "id":              pid,
                "owner":           owner,
                "type":            "KR" if is_kr else "US",
                "code":            code,
                "name":            p_info["name"],
                "qty":             pdata["qty"],
                "buy_price":       b_price_str,
                "current_price":   c_price_str,
                "buy_amount_raw":  buy_amount,
                "eval_amount_raw": eval_amount,
                "today_change":    f"{p_info['change']:.2f}",
                "my_return":       f"{my_return:.2f}",
            })

        return {
            "usd_krw": f"{usd_krw:,.1f}",
            "kospi":   kospi_info,
            "kosdaq":  kosdaq_info,
            "portfolio": portfolio_list,
        }

    except Exception as e:
        return {"error": str(e)}


@app.get("/", response_class=HTMLResponse)
def get_dashboard_html():
    return """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>조대표 패밀리 오피스</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg-main: #0b0f19; --bg-card: #151f32; --accent: #6366f1;
            --up: #ef4444; --down: #3b82f6;
        }
        * { box-sizing: border-box; }
        body { background: var(--bg-main); color: #f3f4f6; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 20px; margin: 0; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
        h1 { margin: 0; font-size: 26px; font-weight: 800; background: linear-gradient(to right, #6366f1, #a855f7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .update-time { color: #9ca3af; font-size: 12px; background: #1e293b; padding: 6px 12px; border-radius: 20px; }

        /* 탭 */
        .tab-container { display: flex; gap: 0; margin-bottom: 25px; border-bottom: 2px solid rgba(255,255,255,0.07); overflow-x: auto; }
        .tab { padding: 12px 24px; cursor: pointer; color: #9ca3af; font-weight: 600; border-bottom: 3px solid transparent; transition: all 0.25s; white-space: nowrap; user-select: none; }
        .tab:hover { color: #fff; }
        .tab.active { color: #fff; border-bottom: 3px solid var(--accent); }
        .tab-content { display: none; }
        .tab-content.active { display: block; animation: fadeIn 0.3s ease; }
        @keyframes fadeIn { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:translateY(0); } }

        /* 카드 */
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .card { background: var(--bg-card); padding: 18px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.05); }
        .card h3 { margin: 0 0 8px; color: #9ca3af; font-size: 12px; font-weight: 600; letter-spacing: .4px; text-transform: uppercase; }
        .card p  { margin: 0; font-size: 22px; font-weight: 700; }
        .card small { font-size: 12px; color: #9ca3af; }

        /* 테이블 */
        .table-wrap { width: 100%; overflow-x: auto; background: var(--bg-card); border-radius: 12px; border: 1px solid rgba(255,255,255,0.05); }
        table { width: 100%; border-collapse: collapse; text-align: right; min-width: 820px; }
        th, td { padding: 13px 16px; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 13px; }
        th { background: rgba(255,255,255,0.02); color: #9ca3af; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }
        th:first-child, td:first-child { text-align: left; position: sticky; left: 0; background: var(--bg-card); }
        tr:last-child td { border-bottom: none; }
        tfoot th, tfoot td { background: rgba(0,0,0,0.25); color: #fff; font-weight: 700; font-size: 13px; }

        /* 배지 */
        .badge { padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 700; margin-right: 5px; }
        .badge-kr { background: rgba(59,130,246,0.15); color: #60a5fa; }
        .badge-us { background: rgba(234,179,8,0.15);  color: #fde047; }

        /* 색상 */
        .up   { color: var(--up); }
        .down { color: var(--down); }

        /* 버튼 */
        .btn { border: none; padding: 5px 11px; border-radius: 6px; cursor: pointer; font-weight: 700; font-size: 12px; transition: all .2s; }
        .btn-edit   { background: rgba(99,102,241,0.15); color: #818cf8; }
        .btn-edit:hover { background: var(--accent); color: #fff; }
        .btn-delete { background: rgba(239,68,68,0.1); color: #f87171; margin-left: 4px; }
        .btn-delete:hover { background: var(--up); color: #fff; }
        .btn-add { background: linear-gradient(to right, #6366f1, #7c3aed); color: #fff; padding: 8px 16px; border-radius: 8px; font-size: 13px; }
    </style>
</head>
<body>

<div class="header">
    <h1>조대표 패밀리 오피스</h1>
    <div style="display:flex;align-items:center;gap:12px;">
        <button class="btn btn-add" onclick="addStock()">➕ 자산 추가</button>
        <div class="update-time" id="last-update">조회 중...</div>
    </div>
</div>

<!-- 탭 메뉴 -->
<div class="tab-container">
    <div class="tab active"  onclick="switchTab(this,'tab-total')">🏛️ 가족 통합 자산</div>
    <div class="tab"         onclick="switchTab(this,'tab-jo')">👨‍💼 조대표님 자산</div>
    <div class="tab"         onclick="switchTab(this,'tab-wife')">👩‍⚕️ 사모님 자산</div>
</div>

<!-- 통합 탭 -->
<div id="tab-total" class="tab-content active">
    <div class="grid">
        <div class="card"><h3>패밀리 총 평가금액</h3><p id="family-eval" style="color:#6366f1">-</p></div>
        <div class="card"><h3>패밀리 통합 수익률</h3><p id="family-ret">-</p></div>
        <div class="card"><h3>실시간 환율 (USD/KRW)</h3><p id="usd-text" style="color:#fde047">-</p></div>
    </div>
    <div class="grid">
        <div class="card"><h3>KOSPI</h3><p id="kospi-text">-</p></div>
        <div class="card"><h3>KOSDAQ</h3><p id="kosdaq-text">-</p></div>
        <div class="card"><h3>👨‍💼 조대표님 평가금액</h3><p id="jo-eval-card">-</p><small id="jo-ret-card"></small></div>
        <div class="card"><h3>👩‍⚕️ 사모님 평가금액</h3><p id="wife-eval-card">-</p><small id="wife-ret-card"></small></div>
    </div>
</div>

<!-- 조대표 탭 -->
<div id="tab-jo" class="tab-content">
    <div class="table-wrap">
        <table>
            <thead><tr>
                <th>종목명/티커</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>평가금액(원화)</th><th>오늘등락</th><th>수익률</th><th>관리</th>
            </tr></thead>
            <tbody id="tbody-jo"></tbody>
            <tfoot><tr>
                <th colspan="5">조대표님 계좌 총계</th>
                <td id="jo-total-eval">-</td><td>-</td><td id="jo-total-ret">-</td><td></td>
            </tr></tfoot>
        </table>
    </div>
</div>

<!-- 사모님 탭 -->
<div id="tab-wife" class="tab-content">
    <div class="table-wrap">
        <table>
            <thead><tr>
                <th>종목명/티커</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>평가금액(원화)</th><th>오늘등락</th><th>수익률</th><th>관리</th>
            </tr></thead>
            <tbody id="tbody-wife"></tbody>
            <tfoot><tr>
                <th colspan="5">사모님 계좌 총계</th>
                <td id="wife-total-eval">-</td><td>-</td><td id="wife-total-ret">-</td><td></td>
            </tr></tfoot>
        </table>
    </div>
</div>

<script>
let globalData = [];

// ✅ 버그 수정: this(탭 엘리먼트)를 직접 받아서 처리
function switchTab(el, tabId) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
    document.getElementById(tabId).classList.add('active');
}

function fmt(n)    { return Math.round(n).toLocaleString(); }
function sign(n)   { return n > 0 ? '+' : ''; }
function cls(n)    { return n > 0 ? 'up' : (n < 0 ? 'down' : ''); }

function updateDashboard() {
    fetch("/api/market").then(r => r.json()).then(data => {
        if (data.error) { console.error(data.error); return; }

        document.getElementById("last-update").innerText = "🔄 갱신: " + new Date().toLocaleTimeString();
        document.getElementById("usd-text").innerText    = data.usd_krw + " 원";

        // KOSPI / KOSDAQ
        if (data.kospi) {
            const kp = parseFloat(data.kospi.change);
            document.getElementById("kospi-text").innerText  = data.kospi.price + " (" + sign(kp) + data.kospi.change + "%)";
            document.getElementById("kospi-text").className  = cls(kp);
        }
        if (data.kosdaq) {
            const kd = parseFloat(data.kosdaq.change);
            document.getElementById("kosdaq-text").innerText = data.kosdaq.price + " (" + sign(kd) + data.kosdaq.change + "%)";
            document.getElementById("kosdaq-text").className = cls(kd);
        }

        globalData = data.portfolio;

        // 각 탭 렌더
        renderTab("조대표", "tbody-jo",   "jo-eval-card",   "jo-ret-card",   "jo-total-eval",   "jo-total-ret");
        renderTab("사모님", "tbody-wife", "wife-eval-card", "wife-ret-card", "wife-total-eval", "wife-total-ret");

        // 패밀리 합산
        let fb = 0, fe = 0;
        globalData.forEach(s => { fb += s.buy_amount_raw; fe += s.eval_amount_raw; });
        const fr = fb > 0 ? ((fe - fb) / fb * 100) : 0;
        document.getElementById("family-eval").innerText = fmt(fe) + "원";
        document.getElementById("family-ret").innerText  = sign(fr) + fr.toFixed(2) + "%";
        document.getElementById("family-ret").className  = cls(fr);
    });
}

function renderTab(ownerName, tbodyId, cardEvalId, cardRetId, tdEvalId, tdRetId) {
    const tbody    = document.getElementById(tbodyId);
    tbody.innerHTML = "";

    const filtered = globalData.filter(s => s.owner === ownerName);
    let tb = 0, te = 0;
    filtered.forEach(s => { tb += s.buy_amount_raw; te += s.eval_amount_raw; });
    const tr = tb > 0 ? ((te - tb) / tb * 100) : 0;

    filtered.forEach(stock => {
        const weight     = te > 0 ? (stock.eval_amount_raw / te * 100).toFixed(1) : "0.0";
        const td         = parseFloat(stock.today_change);
        const mr         = parseFloat(stock.my_return);
        const badge      = stock.type === 'KR'
            ? '<span class="badge badge-kr">국내</span>'
            : '<span class="badge badge-us">해외</span>';

        tbody.innerHTML += `
            <tr>
                <td>${badge}<strong>${stock.name}</strong><br>
                    <small style="color:#9ca3af;margin-left:35px">${stock.code}</small></td>
                <td style="font-weight:600">${stock.qty.toLocaleString()}주</td>
                <td style="color:#aaa">${weight}%</td>
                <td>${stock.buy_price}</td>
                <td><strong>${stock.current_price}</strong></td>
                <td style="color:#fff;font-weight:700">${fmt(stock.eval_amount_raw)}원</td>
                <td class="${cls(td)}">${sign(td)}${stock.today_change}%</td>
                <td class="${cls(mr)}" style="font-size:14px"><strong>${sign(mr)}${stock.my_return}%</strong></td>
                <td>
                    <button class="btn btn-edit"
                        onclick="editStock('${stock.id}','${stock.owner}','${stock.code}','${stock.name}','${stock.buy_price}',${stock.qty})">수정</button>
                    <button class="btn btn-delete"
                        onclick="deleteStock('${stock.id}','${stock.name}')">❌</button>
                </td>
            </tr>`;
    });

    const evalStr = fmt(te) + "원";
    const retStr  = sign(tr) + tr.toFixed(2) + "%";

    document.getElementById(cardEvalId).innerText = evalStr;
    document.getElementById(cardRetId).innerText  = "수익률 " + retStr;
    document.getElementById(cardRetId).className  = cls(tr);
    document.getElementById(tdEvalId).innerText   = evalStr;
    document.getElementById(tdRetId).innerText    = retStr;
    document.getElementById(tdRetId).className    = cls(tr);
}

function editStock(id, owner, code, name, price, qty) {
    const cleanPrice = price.replace(/[원$,]/g, '');
    const p = prompt(`[${owner} - ${name}]\\n새로운 매입단가 (숫자만):`, cleanPrice); if (!p) return;
    const q = prompt(`[${owner} - ${name}]\\n새로운 보유수량:`, qty);                 if (!q) return;
    fetch("/api/update", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({id, owner, code, buy_price: parseFloat(p), qty: parseInt(q)})
    }).then(() => updateDashboard());
}

function addStock() {
    const o = prompt("누구의 자산인가요?\\n1: 조대표\\n2: 사모님", "1"); if (!o) return;
    const owner = o.trim() === "2" ? "사모님" : "조대표";
    const c = prompt("종목코드 6자리 또는 미국 티커 (예: AAPL, TSLA):"); if (!c) return;
    const p = prompt("매입단가 (원화 또는 달러 숫자만):", "100");         if (!p) return;
    const q = prompt("보유수량:", "10");                                   if (!q) return;
    fetch("/api/add", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({id:"", owner, code: c.trim().toUpperCase(), buy_price: parseFloat(p), qty: parseInt(q)})
    }).then(() => updateDashboard());
}

function deleteStock(id, name) {
    if (!confirm(`[${name}] 삭제하시겠습니까?`)) return;
    fetch("/api/delete", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({id})
    }).then(() => updateDashboard());
}

updateDashboard();
setInterval(updateDashboard, 300000);
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") updateDashboard();
});
</script>
</body>
</html>"""
