import json
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

def fetch_twse_90days():
    """帶延遲防封鎖的證交所加權指數抓取邏輯"""
    today = datetime.now()
    all_data = []
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    # 抓取近 4 個月的月份數據
    for i in range(4):
        target_date = today - timedelta(days=i*28)
        date_str = target_date.strftime("%Y%m01")
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={date_str}"
        try:
            res = requests.get(url, headers=headers, timeout=10).json()
            if res.get("stat") == "OK":
                for row in res["data"]:
                    date_parts = row[0].split('/')
                    year = int(date_parts[0]) + 1911
                    formatted_date = f"{year}-{date_parts[1]}-{date_parts[2]}"
                    price = float(row[4].replace(',', ''))
                    volume = float(row[2].replace(',', '')) / 100000000
                    all_data.append({"date": formatted_date, "price": price, "volume": volume})
        except Exception as e:
            print(f"TWSE Fetch Warning [{date_str}]: {e}")
        
        # 關鍵：加上 1.5 秒延遲，避免 API 被證交所封鎖
        time.sleep(1.5)

    if not all_data:
        return {}

    # 排序與去重
    df = pd.DataFrame(all_data).drop_duplicates(subset=['date']).sort_values('date').reset_index(drop=True)
    
    # 計算 20MA 與 60MA (前幾天不夠長時補 None，絕不使用 dropna 刪除最新日期)
    df['ma20'] = df['price'].rolling(window=20, min_periods=1).mean()
    df['ma60'] = df['price'].rolling(window=60, min_periods=1).mean()
    
    # 取最近 60 個交易日（不進行 dropna）
    df_recent = df.tail(60).copy()
    
    prices = df_recent['price'].tolist()
    volume_colors = []
    for i in range(len(prices)):
        if i == 0 or prices[i] >= prices[i-1]:
            volume_colors.append('#de350b') # 漲紅
        else:
            volume_colors.append('#00875a') # 跌綠

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
    
    taiex_chart = fetch_twse_90days()
    
    if taiex_chart and len(taiex_chart.get("prices", [])) > 1:
        latest_price = taiex_chart["prices"][-1]
        prev_price = taiex_chart["prices"][-2]
        change = round(latest_price - prev_price, 2)
        change_pct = round((change / prev_price) * 100, 2)
    else:
        latest_price, change, change_pct = 0, 0, 0

    # 讀取現有 data.json 避免覆蓋其他正常欄位
    try:
        with open("data.json", "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    data["date"] = today_str
    data["updateTime"] = now_str
    if "taiex" not in data:
        data["taiex"] = {}
    data["taiex"]["price"] = latest_price
    data["taiex"]["change"] = change
    data["taiex"]["changePercent"] = change_pct
    data["taiexChart"] = taiex_chart

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    print(f"[{now_str}] 已成功修復並同步加權指數至最新日期！")

if __name__ == "__main__":
    update_json()
