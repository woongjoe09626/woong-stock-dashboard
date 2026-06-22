import requests
import json
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

GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbx1XXKA_GKnIsnaNJqLH0RCCY_iDxSIDv_xalVyuAB6-9gUVYN5r4cy1pNixs1XkSMM/exec"

HEADERS_NAV = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.naver.com/",
}

def load_portfolio():
    try:
        res = requests.get(f"{GOOGLE_SHEET_URL}?action=get", timeout=10)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print("구글 시트 불러오기 실패:", e)
    return {}

def save_portfolio(data):
    try:
        data_str = json.dumps(data)
        params = {"action": "set", "data": data_str}
        requests.get(GOOGLE_SHEET_URL, params=params, timeout=10)
    except Exception as e:
        print("구글 시트 저장하기 실패:", e)

class UpdateItem(BaseModel):
    code: str
    buy_price: float
    qty: int

class DeleteItem(BaseModel):
    code: str

@app.post("/api/update")
def update_portfolio(item: UpdateItem):
    my_port = load_portfolio()
    if item.code in my_port:
        my_port[item.code] = {"buy_price": item.buy_price, "qty": item.qty}
        save_portfolio(my_port)
        return {"status": "success"}
    return {"error": "종목을 찾을 수 없습니다."}

@app.post("/api/add")
def add_portfolio(item: UpdateItem):
    my_port = load_portfolio()
    my_port[item.code] = {"buy_price": item.buy_price, "qty": item.qty}
    save_portfolio(my_port)
    return {"status": "success"}

@app.post("/api/delete")
def delete_portfolio(item: DeleteItem):
    my_port = load_portfolio()
    if item.code in my_port:
        del my_port[item.code]
        save_portfolio(my_port)
        return {"status": "success"}
    return {"error": "삭제할 종목이 없습니다."}

@app.get("/api/market")
def get_market_data():
    try:
        my_port = load_portfolio()

        # 1. 실시간 환율 가져오기
        usd_krw = 1380.0
        try:
            fx = yf.Ticker("USDKRW=X").history(period="1d")
            if not fx.empty:
                usd_krw = float(fx['Close'].iloc[-1])
        except Exception as e:
            print("환율 가져오기 실패:", e)

        # 한국/미국 주식 구분
        kr_tickers = [k for k in my_port.keys() if k.isdigit()]
        us_tickers = [k for k in my_port.keys() if not k.isdigit()]

        # 2. 네이버 API로 국내 지수 + 한국 주식
        query = "SERVICE_INDEX:KOSPI,KOSDAQ"
        if kr_tickers:
            query += f"|SERVICE_ITEM:{','.join(kr_tickers)}"

        res = requests.get(
            "https://polling.finance.naver.com/api/realtime",
            headers=HEADERS_NAV,
            params={"query": query},
            timeout=5
        )
        areas = res.json()["result"]["areas"]

        index_items = areas[0]["datas"]
        kospi_val = int(index_items[0]["nv"]) / 100
        kospi_change = int(index_items[0]["cv"]) / 100
        kospi_pct = kospi_change / (kospi_val - kospi_change) * 100

        kosdaq_val = int(index_items[1]["nv"]) / 100
        kosdaq_change = int(index_items[1]["cv"]) / 100
        kosdaq_pct = kosdaq_change / (kosdaq_val - kosdaq_change) * 100

        temp_portfolio = []
        total_eval_amount = 0
        total_buy_amount = 0

        # 한국 주식 파싱
        if len(areas) > 1 and kr_tickers:
            for item in areas[1]["datas"]:
                code = item["cd"]
                name = item.get("nm", "이름없음")
                current_price = int(item["nv"])
                today_change_pct = float(item["cr"])

                port_info = my_port.get(code, {"buy_price": 0, "qty": 0})
                buy_price = port_info["buy_price"]
                qty = port_info["qty"]

                buy_amount = buy_price * qty
                eval_amount = current_price * qty

                total_buy_amount += buy_amount
                total_eval_amount += eval_amount
                my_return_pct = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0.0

                temp_portfolio.append({
                    "type": "KR", "code": code, "name": name, "buy_price": buy_price,
                    "current_price": current_price, "qty": qty, "buy_amount": buy_amount,
                    "eval_amount": eval_amount, "today_change": today_change_pct, "my_return": my_return_pct
                })

        # 3. Yahoo Finance로 미국 주식
        if us_tickers:
            for ticker in us_tickers:
                try:
                    yf_ticker = yf.Ticker(ticker)
                    hist = yf_ticker.history(period="2d")

                    # 정식 회사명 가져오기
                    info = yf_ticker.info
                    full_name = info.get('longName') or info.get('shortName') or ticker.upper()

                    if len(hist) >= 1:
                        current_price = float(hist['Close'].iloc[-1])
                        prev_close = float(hist['Close'].iloc[-2]) if len(hist) >= 2 else current_price
                        today_change_pct = ((current_price - prev_close) / prev_close) * 100

                        port_info = my_port.get(ticker, {"buy_price": 0, "qty": 0})
                        buy_price = port_info["buy_price"]
                        qty = port_info["qty"]

                        buy_amount = buy_price * qty * usd_krw
                        eval_amount = current_price * qty * usd_krw

                        total_buy_amount += buy_amount
                        total_eval_amount += eval_amount
                        my_return_pct = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0.0

                        temp_portfolio.append({
                            "type": "US",
                            "code": ticker.upper(),
                            "name": full_name,
                            "buy_price": buy_price,
                            "current_price": current_price,
                            "qty": qty,
                            "buy_amount": buy_amount,
                            "eval_amount": eval_amount,
                            "today_change": today_change_pct,
                            "my_return": my_return_pct
                        })
                except Exception as e:
                    print(f"미국 주식 {ticker} 조회 실패:", e)

        # 4. 비중 계산 및 최종 포맷팅
        portfolio_list = []
        for stock in temp_portfolio:
            weight = (stock["eval_amount"] / total_eval_amount * 100) if total_eval_amount > 0 else 0.0

            if stock["type"] == "KR":
                b_price_str = f"{int(stock['buy_price']):,}원"
                c_price_str = f"{int(stock['current_price']):,}원"
            else:
                b_price_str = f"${stock['buy_price']:.2f}"
                c_price_str = f"${stock['current_price']:.2f}"

            portfolio_list.append({
                "type": stock["type"],
                "code": stock["code"],
                "name": stock["name"],
                "qty": stock["qty"],
                "buy_price": b_price_str,
                "current_price": c_price_str,
                "buy_amount": f"{int(stock['buy_amount']):,}",
                "eval_amount": f"{int(stock['eval_amount']):,}",
                "weight": f"{weight:.1f}",
                "today_change": f"{stock['today_change']:.2f}",
                "my_return": f"{stock['my_return']:.2f}"
            })

        total_return_pct = ((total_eval_amount - total_buy_amount) / total_buy_amount) * 100 if total_buy_amount > 0 else 0.0

        return {
            "kospi": {"price": f"{kospi_val:,.2f}", "change": f"{kospi_pct:.2f}"},
            "kosdaq": {"price": f"{kosdaq_val:,.2f}", "change": f"{kosdaq_pct:.2f}"},
            "usd_krw": f"{usd_krw:,.1f}",
            "portfolio": portfolio_list,
            "summary": {
                "total_buy_amount": f"{int(total_buy_amount):,}",
                "total_eval_amount": f"{int(total_eval_amount):,}",
                "total_return_pct": f"{total_return_pct:.2f}"
            }
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/", response_class=HTMLResponse)
def get_dashboard_html():
    return """
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>조대표 스마트 자산 전광판</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root { --bg-main: #0b0f19; --bg-card: #151f32; --accent: #6366f1; --up-color: #ef4444; --down-color: #3b82f6; }
            body { background-color: var(--bg-main); color: #f3f4f6; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 20px; margin: 0; }
            .header-container { display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px; }
            h1 { margin: 0; font-size: 24px; font-weight: 800; background: linear-gradient(to right, #6366f1, #a855f7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .update-time { color: #9ca3af; font-size: 12px; background: #1e293b; padding: 6px 12px; border-radius: 20px; }
            .dashboard-top { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 15px; margin-bottom: 25px; }
            .market-card { background-color: var(--bg-card); padding: 18px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.05); box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3); }
            .market-card h3 { margin: 0 0 8px 0; color: #9ca3af; font-size: 13px; font-weight: 600; letter-spacing: 0.5px; }
            .market-card p { margin: 0; font-size: 20px; font-weight: 700; }
            .chart-container { background-color: var(--bg-card); padding: 18px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.05); display: flex; flex-direction: column; align-items: center; justify-content: center; grid-column: span 2; min-height: 160px; }
            @media (max-width: 768px) { .chart-container { grid-column: span 1; } }
            .canvas-wrapper { position: relative; height: 140px; width: 100%; }
            .table-wrapper { width: 100%; overflow-x: auto; background-color: var(--bg-card); border-radius: 12px; border: 1px solid rgba(255,255,255,0.05); box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3); }
            table { width: 100%; border-collapse: collapse; text-align: right; min-width: 800px; }
            th, td { padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 13px; }
            th { background-color: rgba(255,255,255,0.02); color: #9ca3af; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
            th:first-child, td:first-child { text-align: left; position: sticky; left: 0; background-color: var(--bg-card); }
            .badge { padding: 3px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; margin-right: 5px; }
            .badge-kr { background: rgba(59,130,246,0.15); color: #60a5fa; }
            .badge-us { background: rgba(234,179,8,0.15); color: #fde047; }
            tfoot th { background-color: rgba(0,0,0,0.2); color: #fff; font-size: 14px; text-align: center; }
            tfoot td { background-color: rgba(0,0,0,0.2); color: #fff; font-size: 14px; font-weight: bold; }
            .up { color: var(--up-color); } .down { color: var(--down-color); }
            .btn { border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; font-weight: bold; font-size: 12px; transition: all 0.2s; }
            .btn-edit { background-color: rgba(99,102,241,0.2); color: #818cf8; }
            .btn-edit:hover { background-color: var(--accent); color: white; }
            .btn-delete { background-color: rgba(239,68,68,0.1); color: #f87171; margin-left: 4px; }
            .btn-delete:hover { background-color: var(--up-color); color: white; }
            .btn-add { background: linear-gradient(to right, #6366f1, #7c3aed); color: white; padding: 8px 16px; border-radius: 8px; font-size: 13px; box-shadow: 0 4px 6px rgba(99,102,241,0.2); }
        </style>
    </head>
    <body>
        <div class="header-container">
            <h1>조대표 GLOBAL 포트폴리오</h1>
            <div style="display:flex; align-items:center; gap:12px;">
                <button class="btn btn-add" onclick="addStock()">➕ 자산 추가</button>
                <div class="update-time" id="last-update">조회 중...</div>
            </div>
        </div>
        <div class="dashboard-top">
            <div class="market-card"><h3>KOSPI</h3><p id="kospi-text">...</p></div>
            <div class="market-card"><h3>KOSDAQ</h3><p id="kosdaq-text">...</p></div>
            <div class="market-card"><h3>실시간 환율 (USD/KRW)</h3><p id="usd-text" style="color:#fde047">...</p></div>
            <div class="chart-container">
                <div class="canvas-wrapper"><canvas id="portfolioChart"></canvas></div>
            </div>
        </div>
        <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>종목명/티커</th><th>보유수량</th><th>비중</th><th>매입단가</th><th>현재가</th><th>총 매입금액(원화)</th><th>총 평가금액(원화)</th><th>오늘등락</th><th>수익률</th><th>관리</th>
                    </tr>
                </thead>
                <tbody></tbody>
                <tfoot>
                    <tr>
                        <th colspan="5">총 자산 계좌 총계</th>
                        <td id="total-buy" style="color:#aaa;">0원</td>
                        <td id="total-eval" style="color:#6366f1; font-size:16px;">0원</td>
                        <td>-</td>
                        <td id="total-return">0%</td>
                        <td></td>
                    </tr>
                </tfoot>
            </table>
        </div>
        <script>
            let myChart = null;
            function updateDashboard() {
                fetch("/api/market")
                    .then(res => res.json())
                    .then(data => {
                        if(data.error) { console.error(data.error); return; }
                        document.getElementById("last-update").innerText = "🔄 갱신: " + new Date().toLocaleTimeString();
                        document.getElementById("kospi-text").innerText = data.kospi.price + " (" + data.kospi.change + "%)";
                        document.getElementById("kospi-text").className = parseFloat(data.kospi.change) > 0 ? "up" : "down";
                        document.getElementById("kosdaq-text").innerText = data.kosdaq.price + " (" + data.kosdaq.change + "%)";
                        document.getElementById("kosdaq-text").className = parseFloat(data.kosdaq.change) > 0 ? "up" : "down";
                        document.getElementById("usd-text").innerText = data.usd_krw + " 원";

                        const tbody = document.querySelector("table tbody");
                        tbody.innerHTML = "";
                        const labels = []; const chartData = [];

                        data.portfolio.forEach(stock => {
                            labels.push(stock.name);
                            chartData.push(parseInt(stock.eval_amount.replace(/,/g, '')));
                            const todayChange = parseFloat(stock.today_change);
                            const todayColor = todayChange > 0 ? 'up' : (todayChange < 0 ? 'down' : '');
                            const myReturn = parseFloat(stock.my_return);
                            const myColor = myReturn > 0 ? 'up' : (myReturn < 0 ? 'down' : '');
                            const badge = stock.type === 'KR' ? '<span class="badge badge-kr">국내</span>' : '<span class="badge badge-us">해외</span>';

                            tbody.innerHTML += `
                                <tr>
                                    <td>${badge}<strong>${stock.name}</strong><br><small style="color:#9ca3af; margin-left:35px;">${stock.code}</small></td>
                                    <td style="font-weight:600; text-align:right;">${stock.qty.toLocaleString()}주</td>
                                    <td><span style="color:#aaa">${stock.weight}%</span></td>
                                    <td>${stock.buy_price}</td>
                                    <td><strong>${stock.current_price}</strong></td>
                                    <td>${stock.buy_amount}원</td>
                                    <td style="color:#fff; font-weight:700;">${stock.eval_amount}원</td>
                                    <td class="${todayColor}">${todayChange > 0 ? '+' : ''}${stock.today_change}%</td>
                                    <td class="${myColor}" style="font-size:14px;"><strong>${myReturn > 0 ? '+' : ''}${stock.my_return}%</strong></td>
                                    <td>
                                        <button class="btn btn-edit" onclick="editStock('${stock.code}', '${stock.name}', '${stock.buy_price}', ${stock.qty})">수정</button>
                                        <button class="btn btn-delete" onclick="deleteStock('${stock.code}', '${stock.name}')">❌</button>
                                    </td>
                                </tr>
                            `;
                        });

                        document.getElementById("total-buy").innerText = data.summary.total_buy_amount + "원";
                        document.getElementById("total-eval").innerText = data.summary.total_eval_amount + "원";
                        const totalRet = parseFloat(data.summary.total_return_pct);
                        document.getElementById("total-return").innerText = (totalRet > 0 ? '+' : '') + totalRet + "%";
                        document.getElementById("total-return").className = totalRet > 0 ? "up" : (totalRet < 0 ? "down" : "");

                        const ctx = document.getElementById('portfolioChart').getContext('2d');
                        if (myChart) {
                            myChart.data.labels = labels;
                            myChart.data.datasets[0].data = chartData;
                            myChart.update();
                        } else {
                            myChart = new Chart(ctx, {
                                type: 'doughnut',
                                data: {
                                    labels: labels,
                                    datasets: [{ data: chartData, backgroundColor: ['#6366f1', '#a855f7', '#ec4899', '#10b981', '#f59e0b', '#3b82f6'], borderWidth: 0 }]
                                },
                                options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'right', labels: { color: '#f3f4f6', font: { size: 11, weight: 'bold' } } } } }
                            });
                        }
                    });
            }
            function editStock(code, name, price, qty) {
                let cleanPrice = price.replace(/[원$,]/g, '');
                const p = prompt(`[${name}] 새로운 매입단가 (원화 혹은 달러 숫자만):`, cleanPrice); if (!p) return;
                const q = prompt(`[${name}] 새로운 보유수량:`, qty); if (!q) return;
                fetch("/api/update", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: code, buy_price: parseFloat(p), qty: parseInt(q) }) }).then(() => updateDashboard());
            }
            function addStock() {
                const c = prompt("종목코드 6자리 또는 미국 티커(예: AAPL, TSLA):"); if (!c) return;
                const p = prompt("매입단가 (원화 혹은 달러 숫자만):", "100"); if (!p) return;
                const q = prompt("보유수량:", "10"); if (!q) return;
                fetch("/api/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: c.trim().toUpperCase(), buy_price: parseFloat(p), qty: parseInt(q) }) }).then(() => updateDashboard());
            }
            function deleteStock(code, name) {
                if (confirm(`[${name}] 삭제하시겠습니까?`)) {
                    fetch("/api/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: code }) }).then(() => updateDashboard());
                }
            }
            updateDashboard();
            setInterval(updateDashboard, 300000);
            document.addEventListener("visibilitychange", () => {
                if (document.visibilityState === "visible") updateDashboard();
            });
        </script>
    </body>
    </html>
    """
