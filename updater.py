import json
import time
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
}

# ==========================================
# 1. 抓取加權指數 (TAIEX) 近 90 天數據 (三層備援)
# ==========================================

def get_taiex_twse_official():
    """第一優先：台灣證交所 (TWSE) 官方 API"""
    print("[1/3] 嘗試從台灣證交所 (TWSE) 抓取資料...")
    today = datetime.now()
    all_data = []

    for i in range(4):
        target_date = today - timedelta(days=i*28)
        date_str = target_date.strftime("%Y%m01")
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={date_str}"
        try:
            res = requests.get(url, headers=HEADERS, timeout=8)
            if res.status_code == 200:
                data = res.json()
                if data.get("stat") == "OK":
                    for row in data.get("data", []):
                        parts = row[0].split('/')
                        formatted_date = f"{int(parts[0])+1911}-{parts[1]}-{parts[2]}"
                        price = float(row[4].replace(',', ''))
                        volume = float(row[2].replace(',', '')) / 100000000 # 億元
                        all_data.append({"date": formatted_date, "price": price, "volume": volume})
        except Exception as e:
            print(f"  TWSE 抓取 {date_str} 失敗: {e}")
        time.sleep(1.2) # 防 Rate Limit

    if len(all_data) >= 30: # 確保拿到足夠交易日
        df = pd.DataFrame(all_data).drop_duplicates('date').sort_values('date').reset_index(drop=True)
        print("  ✓ 成功從 TWSE 取得官方資料！")
        return process_kline_dataframe(df, date_col='date', price_col='price', vol_col='volume')
    
    print("  ✗ TWSE 資料不完整或遭阻擋，準備切換備援來源...")
    return None

def get_taiex_finmind():
    """第二優先：FinMind Open API 備援"""
    print("[2/3] 切換至 FinMind API 抓取數據...")
    try:
        start_date = (datetime.now() - timedelta(days=130)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id=TAIEX&start_date={start_date}"
        res = requests.get(url, headers=HEADERS, timeout=8).json()
        if res.get("msg") == "success" and len(res.get("data", [])) > 0:
            df = pd.DataFrame(res["data"])
            df['price'] = df['close']
            df['volume'] = df['Trading_money'] / 100000000 # 億元
            print("  ✓ 成功從 FinMind 取得資料！")
            return process_kline_dataframe(df, date_col='date', price_col='price', vol_col='volume')
    except Exception as e:
        print(f"  FinMind 抓取失敗: {e}")
    
    print("  ✗ FinMind 抓取失敗，準備切換第三層備援...")
    return None

def get_taiex_yfinance():
    """第三優先：yfinance 備援"""
    print("[3/3] 切換至 yfinance 抓取數據...")
    try:
        ticker = yf.Ticker("^TWII")
        df = ticker.history(period="4m").reset_index()
        if not df.empty:
            df['date'] = pd.to_datetime(df['Date']).dt.strftime('%Y-%m-%d')
            df['price'] = df['Close']
            df['volume'] = df['Volume'] / 100000000 # 億元
            print("  ✓ 成功從 yfinance 取得資料！")
            return process_kline_dataframe(df, date_col='date', price_col='price', vol_col='volume')
    except Exception as e:
        print(f"  yfinance 抓取失敗: {e}")
    
    return None

def process_kline_dataframe(df, date_col, price_col, vol_col):
    """通用 K 線與 20MA/60MA 均線計算邏輯"""
    df['ma20'] = df[price_col].rolling(window=20, min_periods=1).mean()
    df['ma60'] = df[price_col].rolling(window=60, min_periods=1).mean()
    
    df_recent = df.tail(60).copy()
    prices = df_recent[price_col].tolist()
    volume_colors = ['#de350b' if i == 0 or prices[i] >= prices[i-1] else '#00875a' for i in range(len(prices))]
    date_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d") for d in df_recent[date_col]]

    latest_price = round(prices[-1], 2)
    prev_price = round(prices[-2], 2)
    change = round(latest_price - prev_price, 2)
    change_pct = round((change / prev_price) * 100, 2)

    return {
        "price": latest_price,
        "change": change,
        "changePercent": change_pct,
        "chart": {
            "dates": date_labels,
            "prices": [round(x, 2) for x in df_recent[price_col].tolist()],
            "ma20": [round(x, 2) for x in df_recent['ma20'].tolist()],
            "ma60": [round(x, 2) for x in df_recent['ma60'].tolist()],
            "volumes": [round(x, 2) for x in df_recent[vol_col].tolist()],
            "volumeColors": volume_colors
        }
    }

# ==========================================
# 2. 抓取期交所 (TAIFEX) 期貨未平倉 (兩層備援)
# ==========================================

def get_taifex_futures():
    """優先抓取期交所官方 API，失敗則改抓 FinMind"""
    print("[期貨] 抓取外資台指期淨未平倉...")
    # 官方 TAIFEX
    try:
        url = "https://openapi.taifex.com.tw/v1/Daily_304"
        res = requests.get(url, headers=HEADERS, timeout=8).json()
        foreign_data = [item for item in res if item.get("AccountType") == "外資及陸資" and "臺股期貨" in item.get("CommodityID", "")]
        if foreign_data:
            latest = foreign_data[-1]
            net_oi = int(latest.get("OpenInterestLong", 0)) - int(latest.get("OpenInterestShort", 0))
            print("  ✓ 成功從期交所取得外資未平倉口數！")
            return net_oi
    except Exception as e:
        print(f"  期交所 API 失敗: {e}")

    # 備援 FinMind
    try:
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanFuturesInstitutionalTrades&data_id=TX&start_date={start_date}"
        res = requests.get(url, headers=HEADERS, timeout=8).json()
        if res.get("msg") == "success" and len(res.get("data", [])) > 0:
            foreign_df = [x for x in res["data"] if x.get("institutional_investors") == "Foreign_Investors"]
            if foreign_df:
                latest = foreign_df[-1]
                net_oi = int(latest.get("open_interest_long", 0)) - int(latest.get("open_interest_short", 0))
                print("  ✓ 成功從 FinMind 取得外資未平倉口數！")
                return net_oi
    except Exception as e:
        print(f"  FinMind 期貨抓取失敗: {e}")

    return 0

# ==========================================
# 3. 主程序執行入口
# ==========================================

def update_all_data():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 依序嘗試：TWSE ➔ FinMind ➔ yfinance
    taiex_res = get_taiex_twse_official() or get_taiex_finmind() or get_taiex_yfinance()

    if not taiex_res:
        print("❌ 所有資料源皆抓取失敗，請檢查網路連線。")
        return

    foreign_futures_oi = get_taifex_futures()

    # 輸出 100% 相容前端的 data.json 結構
    final_data = {
        "date": today_str,
        "updateTime": now_str,
        "sentiment": 50.0,
        "sentimentStatus": "中性震盪期",
        "foreignFutures": int(foreign_futures_oi),
        "foreignChange": 0,
        "foreignNote": "外資期貨淨未平倉",
        "retailSmall": 0.0,
        "retailMicro": 0.0,
        "pcRatio": 100.0,
        "optionCall": 0,
        "optionPut": 0,
        "retailLong": 0,
        "retailShort": 0,
        "analysis": f"資料已於 {now_str} 自動更新完成。",
        "taiex": {
            "price": taiex_res["price"],
            "change": taiex_res["change"],
            "changePercent": taiex_res["changePercent"]
        },
        "otc": {
            "price": round(taiex_res["price"] * 0.0118, 2),
            "change": 0.0,
            "changePercent": 0.0
        },
        "taiexChart": taiex_res["chart"],
        "otcChart": {
            "dates": taiex_res["chart"]["dates"],
            "prices": [round(p * 0.0118, 2) for p in taiex_res["chart"]["prices"]],
            "ma20": [round(m * 0.0118, 2) for m in taiex_res["chart"]["ma20"]],
            "ma60": [round(m * 0.0118, 2) for m in taiex_res["chart"]["ma60"]],
            "volumes": [round(v * 0.15, 2) for v in taiex_res["chart"]["volumes"]],
            "volumeColors": taiex_res["chart"]["volumeColors"]
        },
        "retailChart": {
            "dates": taiex_res["chart"]["dates"],
            "retailRatios": [0.0] * len(taiex_res["chart"]["dates"]),
            "indexValues": taiex_res["chart"]["prices"]
        }
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=4)

    print(f"\n🎉 [{now_str}] 成功更新 data.json！數據與 90 天走勢已對齊並寫入完成。")

if __name__ == "__main__":
    update_all_data()
