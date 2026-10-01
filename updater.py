import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

def fetch_twse_90days():
    """從證交所 API 抓取過去 4 個月數據並計算 90 天 (約 60 交易日) 的 K 線與 MA"""
    today = datetime.now()
    all_data = []
    
    # 逐月抓取近 4 個月的資料以確保補足 60 個交易日
    for i in range(4):
        target_date = today - timedelta(days=i*30)
        date_str = target_date.strftime("%Y%m01")
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={date_str}"
        try:
            res = requests.get(url, timeout=10).json()
            if res.get("stat") == "OK":
                for row in res["data"]:
                    # row format: [日期, 成交股數, 成交金額, 成交筆數, 發行量加權股價指數, 漲跌點數]
                    date_parts = row[0].split('/')
                    year = int(date_parts[0]) + 1911 # 民國轉西元
                    formatted_date = f"{year}-{date_parts[1]}-{date_parts[2]}"
                    price = float(row[4].replace(',', ''))
                    volume = float(row[2].replace(',', '')) / 100000000 # 單位：億元
                    all_data.append({"date": formatted_date, "price": price, "volume": volume})
        except Exception as e:
            print(f"Fetch error for {date_str}: {e}")

    if not all_data:
        return {}

    # 轉 Pandas 進行排序與均線計算
    df = pd.DataFrame(all_data).drop_duplicates(subset=['date']).sort_values('date').reset_index(drop=True)
    
    # 計算 20MA 與 60MA
    df['ma20'] = df['price'].rolling(window=20).mean()
    df['ma60'] = df['price'].rolling(window=60).mean()
    
    # 取最近 60 個交易日 (約近 90 天曆日)
    df_recent = df.tail(60).dropna(subset=['ma20']).copy()
    
    # 計算漲跌顏色
    prices = df_recent['price'].tolist()
    volume_colors = []
    for i in range(len(prices)):
        if i == 0 or prices[i] >= prices[i-1]:
            volume_colors.append('#de350b') # 漲 (紅)
        else:
            volume_colors.append('#00875a') # 跌 (綠)

    date_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d") for d in df_recent['date']]

    return {
        "dates": date_labels,
        "prices": [round(x, 2) for x in df_recent['price'].tolist()],
        "ma20": [round(x, 2) for x in df_recent['ma20'].tolist()],
        "ma60": [round(x, 2) for x in df_recent['ma60'].tolist()],
        "volumes": [round(x, 2) for x in df_recent['volume'].tolist()],
        "volumeColors": volume_colors
    }

def update_json():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    chart_data = fetch_twse_90days()
    
    if chart_data and len(chart_data.get("prices", [])) > 1:
        latest_price = chart_data["prices"][-1]
        prev_price = chart_data["prices"][-2]
        change = round(latest_price - prev_price, 2)
        change_pct = round((change / prev_price) * 100, 2)
    else:
        latest_price, change, change_pct = 0, 0, 0

    latest_data = {
        "date": today_str,
        "updateTime": now_str,
        "sentiment": 58.6,
        "sentimentStatus": "偏多震盪期(樂觀)",
        "taiex": {
            "price": latest_price,
            "change": change,
            "changePercent": change_pct
        },
        "foreignFutures": "-32,150",
        "foreignChange": "+2,450",
        "foreignNote": "期貨空單持續回補",
        "retailSmall": -12.5,
        "retailMicro": -8.4,
        "pcRatio": 118.5,
        "optionCall": "12,450",
        "optionPut": "18,200",
        "analysis": "現貨與期貨籌碼逐步回穩，散戶小台與微台偏空（反指標偏多），P/C Ratio 保持在 100% 以上，整體維持偏多震盪格局。",
        # 動態更新的 90 天圖表數據
        "taiexChart": chart_data,
        "historyDates": chart_data.get("dates", []),
        "historyValues": chart_data.get("prices", [])
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(latest_data, f, ensure_ascii=False, indent=4)
        
    print(f"[{now_str}] data.json 已成功自動更新至最新的 90 天動態數據！")

if __name__ == "__main__":
    update_json()
