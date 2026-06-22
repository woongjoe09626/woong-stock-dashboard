import requests
import json
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

# 🚨 조대표님의 구글 웹 앱 URL을 여기에 꼭 넣어주세요!
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
    buy_price: int
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
        tickers = ",".join(my_port.keys())
        query = "SERVICE_INDEX:KOSPI,KOSDAQ"
        if tickers:
            query += f"|SERVICE_ITEM:{tickers}"
        res = requests.get("https://polling.finance.naver.com/api/realtime", headers=HEADERS_NAV, params={"query": query}, timeout=5)
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
        if len(areas) > 1 and tickers:
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
                temp_portfolio.append({"code": code, "name": name, "buy_price": buy_price, "current_price": current_price, "qty": qty, "buy_amount": buy_amount, "eval_amount": eval_amount, "today_change": today_change_pct, "my_return": my_return_pct})
        portfolio_list = []
        for stock in temp_portfolio:
            weight = (stock["eval_amount"] / total_eval_amount * 100) if total_eval_amount > 0 else 0.0
            portfolio_list.append({"code": stock["code"], "name": stock["name"], "qty": stock["qty"], "buy_price": stock["buy_price"], "current_price": f"{stock['current_price']:,}", "buy_amount": f"{stock['buy_amount']:,}", "eval_amount": f"{stock['eval_amount']:,}", "weight": f"{weight:.1f}", "today_change": f"{stock['today_change']:.2f}", "my_return": f"{stock['my_return']:.2f}"})
        total_return_pct = ((total_eval_amount - total_buy_amount) / total_buy_amount) * 100 if total_buy_amount > 0 else 0.0
        return {"kospi": {"price": f"{kospi_val:,.2f}", "change": f"{kospi_pct:.2f}"}, "kosdaq": {"price": f"{kosdaq_val:,.2f}", "change": f"{kosdaq_pct:.2f}"}, "portfolio": portfolio_list, "summary": {"total_buy_amount": f"{total_buy_amount:,}", "total_eval_amount": f"{total_eval_amount:,}", "total_return_pct": f"{total_return_pct:.2f}"}}
    except Exception as e:
        return {"error": str(e)}

# 🌟 핸드폰으로 접속했을 때 화면을 바로 띄워주는 마법의 라우트!
@app.get("/", response_class=HTMLResponse)
def get_dashboard_html():
    return """
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>조대표님의 실시간 포트폴리오</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { background-color: #121212; color: #ffffff; font-family: Arial, sans-serif; padding: 15px; margin: 0; }
            .header-container { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
            h1 { margin: 0; font-size: 22px; }
            .header-right { display: flex; align-items: center; gap: 10px; }
            .update-time { color: #aaa; font-size: 12px; font-weight: bold; }
            .dashboard-top { display: flex; justify-content: space-between; gap: 15px; margin-bottom: 20px; flex-wrap: wrap; }
            .market-summary { display: flex; gap: 10px; flex: 1; min-width: 280px; }
            .market-card { background-color: #1e1e1e; padding: 15px; border-radius: 8px; flex: 1; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
            .market-card h3 { margin: 0 0 5px 0; color: #aaa; font-size: 14px; }
            .market-card p { margin: 0; font-size: 18px; font-weight: bold; }
            .chart-container { background-color: #1e1e1e; padding: 15px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); width: 100%; max-width: 350px; height: 180px; display: flex; flex-direction: column; align-items: center; justify-content: center; margin: 0 auto; }
            .chart-container h3 { margin: 0 0 5px 0; color: #aaa; font-size: 14px; text-align: center; width:100%; }
            .canvas-wrapper { position: relative; height: 130px; width: 100%; }
            .table-wrapper { width: 100%; overflow-x: auto; background-color: #1e1e1e; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
            table { width: 100%; border-collapse: collapse; text-align: right; min-width: 600px; }
            th, td { padding: 10px 12px; border-bottom: 1px solid #333; font-size: 13px; }
            th { background-color: #2a2a2a; color: #aaa; }
            th:first-child, td:first-child { text-align: left; position: sticky; left: 0; background-color: #1e1e1e; }
            th:first-child { background-color: #2a2a2a; }
            tfoot th { background-color: #222; color: #fff; font-size: 14px; text-align: center; border-top: 2px solid #555; }
            tfoot td { background-color: #222; color: #fff; font-size: 14px; font-weight: bold; border-top: 2px solid #555; }
            .up { color: #ff4b4b; }
            .down { color: #4b89ff; }
            .highlight { background-color: #2a2a2a; font-weight: bold; }
            .btn { border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-weight: bold; color: white; font-size: 12px; }
            .btn-edit { background-color: #4b89ff; }
            .btn-delete { background-color: #ff4b4b; margin-left: 3px; }
            .btn-add { background-color: #28a745; padding: 6px 12px; font-size: 13px; }
        </style>
    </head>
    <body>
        <div class="header-container">
            <h1>조대표님 포트폴리오</h1>
            <div class="header-right">
                <button class="btn btn-add" onclick="addStock()">➕ 종목 추가</button>
                <div class="update-time" id="last-update">갱신 중...</div>
            </div>
        </div>
        <div class="dashboard-top">
            <div class="market-summary">
                <div class="market-card">
                    <h3>KOSPI</h3>
                    <p id="kospi-text">...</p>
                </div>
                <div class="market-card">
                    <h3>KOSDAQ</h3>
                    <p id="kosdaq-text">...</p>
                </div>
            </div>
            <div class="chart-container">
                <h3>포트폴리오 비중 현황</h3>
                <div class="canvas-wrapper"><canvas id="portfolioChart"></canvas></div>
            </div>
        </div>
        <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>종목명</th><th>매입개수</th><th>비중</th><th>매입단가</th><th>현재가</th><th>매입금액</th><th>평가금액</th><th>오늘등락</th><th>총수익률</th><th>관리</th>
                    </tr>
                </thead>
                <tbody></tbody>
                <tfoot>
                    <tr>
                        <th colspan="5">계좌 총계</th>
                        <td id="total-buy" class="highlight">0원</td>
                        <td id="total-eval" class="highlight">0원</td>
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
                // 클라우드 환경이므로 같은 서버의 /api/market 주소를 상대 경로로 찌릅니다!
                fetch("/api/market")
                    .then(res => res.json())
                    .then(data => {
                        if(data.error) return;
                        document.getElementById("last-update").innerText = new Date().toLocaleTimeString();
                        document.getElementById("kospi-text").innerText = data.kospi.price + " (" + data.kospi.change + "%)";
                        document.getElementById("kospi-text").className = parseFloat(data.kospi.change) > 0 ? "up" : "down";
                        document.getElementById("kosdaq-text").innerText = data.kosdaq.price + " (" + data.kosdaq.change + "%)";
                        document.getElementById("kosdaq-text").className = parseFloat(data.kosdaq.change) > 0 ? "up" : "down";
                        
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
                            
                            tbody.innerHTML += `
                                <tr>
                                    <td><strong>${stock.name}</strong><br><small style="color:#aaa;">${stock.code}</small></td>
                                    <td>${stock.qty.toLocaleString()}주</td>
                                    <td>${stock.weight}%</td>
                                    <td>${stock.buy_price.toLocaleString()}원</td>
                                    <td>${stock.current_price}원</td>
                                    <td class="highlight">${stock.buy_amount}원</td>
                                    <td class="highlight">${stock.eval_amount}원</td>
                                    <td class="${todayColor}">${todayChange > 0 ? '+':''}${stock.today_change}%</td>
                                    <td class="${myColor}"><strong>${myReturn > 0 ? '+':''}${stock.my_return}%</strong></td>
                                    <td>
                                        <button class="btn btn-edit" onclick="editStock('${stock.code}', '${stock.name}', ${stock.buy_price}, ${stock.qty})">수정</button>
                                        <button class="btn btn-delete" onclick="deleteStock('${stock.code}', '${stock.name}')">❌</button>
                                    </td>
                                </tr>
                            `;
                        });
                        document.getElementById("total-buy").innerText = data.summary.total_buy_amount + "원";
                        document.getElementById("total-eval").innerText = data.summary.total_eval_amount + "원";
                        const totalRet = parseFloat(data.summary.total_return_pct);
                        document.getElementById("total-return").innerText = (totalRet > 0 ? '+': '') + totalRet + "%";
                        document.getElementById("total-return").className = totalRet > 0 ? "up" : (totalRet < 0 ? "down" : "");
                        
                        const ctx = document.getElementById('portfolioChart').getContext('2d');
                        if (myChart) {
                            myChart.data.labels = labels; myChart.data.datasets[0].data = chartData; myChart.update();
                        } else {
                            myChart = new Chart(ctx, {
                                type: 'doughnut',
                                data: { labels: labels, datasets: [{ data: chartData, backgroundColor: ['#ff6384', '#36a2eb', '#ffce56', '#4bc0c0', '#9966ff', '#ff9f40'], borderWidth: 0 }] },
                                options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'right', labels: { color: '#ffffff', font: { size: 10 } } } } }
                            });
                        }
                    });
            }
            function editStock(code, name, price, qty) {
                const p = prompt(`[${name}] 새로운 매입단가:`, price); if(!p) return;
                const q = prompt(`[${name}] 새로운 보유수량:`, qty); if(!q) return;
                fetch("/api/update", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: code, buy_price: parseInt(p), qty: parseInt(q) }) }).then(() => updateDashboard());
            }
            function addStock() {
                const c = prompt("종목코드 6자리:"); if(!c) return;
                const p = prompt("매입단가:", "10000"); if(!p) return;
                const q = prompt("보유수량:", "10"); if(!q) return;
                fetch("/api/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: c.trim(), buy_price: parseInt(p), qty: parseInt(q) }) }).then(() => updateDashboard());
            }
            function deleteStock(code, name) {
                if(confirm(`[${name}] 삭제하시겠습니까?`)) {
                    fetch("/api/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: code }) }).then(() => updateDashboard());
                }
            }
            updateDashboard(); setInterval(updateDashboard, 300000);
        </script>
    </body>
    </html>
    """
