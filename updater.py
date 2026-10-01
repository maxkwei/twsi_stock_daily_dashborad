import json
import time
import requests
import pandas as pd
from datetime import datetime, timedelta

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def fetch_twse_index_and_chart():
    """動態抓取證交所近 90 天加權指數、成交量並計算 20MA/60MA"""
    today = datetime.now()
    all_data = []

    # 抓取近 4 個月的日成交資訊以確保涵蓋 90 天曆日 (約 60 個交易日)
    for i in range(4):
        target_date = today - timedelta(days=i*28)
        date_str = target_date.strftime("%Y%m01")
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={date_str}"
        try:
            res = requests.get(url, headers=HEADERS, timeout=10).json()
            if res.get("stat") == "OK":
                for row in res["data"]:
                    date_parts = row[0].split('/')
                    year = int(date_parts[0]) + 1911 # 民國轉西元
                    formatted_date = f"{year}-{date_parts[1]}-{date_parts[2]}"
                    price = float(row[4].replace(',', ''))
                    volume = float(row[2].replace(',', '')) / 100000000 # 億元
                    all_data.append({"date": formatted_date, "price": price, "volume": volume})
        except Exception as e:
            print(f"TWSE API Warning [{date_str}]: {e}")
        time.sleep(1.5) # 避開證交所 Rate Limit

    if not all_data:
        return 0, 0, 0, {}

    df = pd.DataFrame(all_data).drop_duplicates(subset=['date']).sort_values('date').reset_index(drop=True)
    
    # 計算 20MA (月線) 與 60MA (季線)
    df['ma20'] = df['price'].rolling(window=20, min_periods=1).mean()
    df['ma60'] = df['price'].rolling(window=60, min_periods=1).mean()
    
    df_recent = df.tail(60).copy()
    
    prices = df_recent['price'].tolist()
    volume_colors = ['#de350b' if i == 0 or prices[i] >= prices[i-1] else '#00875a' for i in range(len(prices))]
    date_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d") for d in df_recent['date']]

    latest_price = round(prices[-1], 2)
    prev_price = round(prices[-2], 2)
    change = round(latest_price - prev_price, 2)
    change_pct = round((change / prev_price) * 100, 2)

    chart_data = {
        "dates": date_labels,
        "prices": [round(x, 2) for x in df_recent['price'].tolist()],
        "ma20": [round(x, 2) for x in df_recent['ma20'].tolist()],
        "ma60": [round(x, 2) for x in df_recent['ma60'].tolist()],
        "volumes": [round(x, 2) for x in df_recent['volume'].tolist()],
        "volumeColors": volume_colors
    }

    return latest_price, change, change_pct, chart_data

def fetch_taifex_futures():
    """從期交所 API 抓取最新盤後外資台指期未平倉口數"""
    try:
        # 期交所三大法人每日未平倉公開 API
        url = "https://openapi.taifex.com.tw/v1/Daily_304"
        res = requests.get(url, headers=HEADERS, timeout=10).json()
        
        # 篩選「外資及陸資」且商品為「臺股期貨」
        foreign_data = [item for item in res if item.get("AccountType") == "外資及陸資" and "臺股期貨" in item.get("CommodityID", "")]
        
        if foreign_data:
            latest = foreign_data[-1]
            net_oi = int(latest.get("OpenInterestLong", 0)) - int(latest.get("OpenInterestShort", 0))
            return net_oi
    except Exception as e:
        print(f"TAIFEX Futures API Warning: {e}")
    return None

def update_all_data():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 1. 抓取證交所動態真實指數與 90 天走勢
    taiex_price, taiex_change, taiex_pct, taiex_chart = fetch_twse_index_and_chart()

    # 2. 抓取期交所真實外資期貨淨未平倉
    foreign_futures_oi = fetch_taifex_futures()
    foreign_futures_str = f"{foreign_futures_oi:,}" if foreign_futures_oi is not None else "--"

    # 3. 讀取或更新 data.json
    try:
        with open("data.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    data["date"] = today_str
    data["updateTime"] = now_str
    
    # 大盤現貨動態更新
    data["taiex"] = {
        "price": taiex_price,
        "change": taiex_change,
        "changePercent": taiex_pct
    }
    data["taiexChart"] = taiex_chart

    # 期貨真實籌碼更新
    if foreign_futures_oi is not None:
        data["foreignFutures"] = foreign_futures_str
        data["foreignNote"] = "偏多操作" if foreign_futures_oi > 0 else "避險偏空"

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    print(f"[{now_str}] 已完成全真實數據自動抓取並更新 data.json！")

if __name__ == "__main__":
    update_all_data()
