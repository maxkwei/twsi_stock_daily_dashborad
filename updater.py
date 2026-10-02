import json
import sys
import time
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
}
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
# GitHub Actions 的機器是 UTC，時間一律換成台灣時間
TW = ZoneInfo("Asia/Taipei")


def now_tw():
    return datetime.now(TW)


def finmind(dataset, data_id, days=120):
    """FinMind 免費版（不帶 token）。回傳 data 列表，失敗丟例外。"""
    start_date = (now_tw() - timedelta(days=days)).strftime("%Y-%m-%d")
    res = requests.get(FINMIND_URL, params={"dataset": dataset, "data_id": data_id, "start_date": start_date},
                       headers=HEADERS, timeout=15).json()
    if res.get("msg") != "success" or not res.get("data"):
        raise RuntimeError(f"FinMind {dataset}/{data_id} 沒有資料：{res.get('msg') or str(res)[:120]}")
    return res["data"]


# ==========================================
# 1. 通用 K 線處理（計算 20MA / 60MA 與顏色）
# ==========================================
def build_kline_output(df, date_col='date', price_col='price', vol_col='volume'):
    df[price_col] = df[price_col].astype(float)
    df[vol_col] = df[vol_col].astype(float)

    df['ma20'] = df[price_col].rolling(window=20, min_periods=1).mean()
    df['ma60'] = df[price_col].rolling(window=60, min_periods=1).mean()

    df_recent = df.tail(60).copy()
    prices = df_recent[price_col].tolist()
    volumes = df_recent[vol_col].tolist()
    volume_colors = ['#de350b' if i == 0 or prices[i] >= prices[i-1] else '#00875a' for i in range(len(prices))]
    iso_dates = [str(d)[:10] for d in df_recent[date_col]]
    date_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d") for d in iso_dates]

    latest_price = round(prices[-1], 2)
    prev_price = round(prices[-2], 2)
    change = round(latest_price - prev_price, 2)
    change_pct = round((change / prev_price) * 100, 2)

    return {
        "price": latest_price,
        "change": change,
        "changePercent": change_pct,
        # 只給 main() 用（資料日期、散戶圖對齊），不寫進 data.json
        "isoDates": iso_dates,
        "chart": {
            "dates": date_labels,
            "prices": [round(x, 2) for x in df_recent[price_col].tolist()],
            "ma20": [round(x, 2) for x in df_recent['ma20'].tolist()],
            "ma60": [round(x, 2) for x in df_recent['ma60'].tolist()],
            "volumes": [round(x, 2) for x in volumes],
            "volumeColors": volume_colors
        }
    }


# ==========================================
# 2. 加權指數 (TAIEX) 三層備援抓取
# ==========================================
def fetch_taiex():
    # Level 1: 證交所 (TWSE)
    print("抓取加權指數 -> [Level 1: 證交所 TWSE]")
    try:
        all_data = []
        today = now_tw()
        for i in range(4):
            t_date = (today - timedelta(days=i*28)).strftime("%Y%m01")
            url = f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={t_date}"
            res = requests.get(url, headers=HEADERS, timeout=8).json()
            if res.get("stat") == "OK":
                for row in res["data"]:
                    p = row[0].split('/')
                    formatted_date = f"{int(p[0])+1911}-{p[1]}-{p[2]}"
                    price = float(row[4].replace(',', ''))
                    vol = float(row[2].replace(',', '')) / 100000000
                    all_data.append({"date": formatted_date, "price": price, "volume": vol})
            time.sleep(1)
        if len(all_data) >= 30:
            df = pd.DataFrame(all_data).drop_duplicates('date').sort_values('date').reset_index(drop=True)
            return build_kline_output(df), "證交所"
    except Exception as e:
        print(f"  Level 1 失敗: {e}")

    # Level 2: FinMind
    print("抓取加權指數 -> [Level 2: FinMind]")
    try:
        df = pd.DataFrame(finmind("TaiwanStockPrice", "TAIEX"))
        df['price'] = df['close']
        df['volume'] = df['Trading_money'] / 100000000
        return build_kline_output(df), "FinMind"
    except Exception as e:
        print(f"  Level 2 失敗: {e}")

    # Level 3: yfinance
    print("抓取加權指數 -> [Level 3: yfinance]")
    try:
        ticker = yf.Ticker("^TWII")
        df = ticker.history(period="4mo").reset_index()
        if not df.empty:
            df['date'] = pd.to_datetime(df['Date']).dt.strftime('%Y-%m-%d')
            df['price'] = df['Close']
            df['volume'] = df['Volume'] / 100000000
            return build_kline_output(df), "yfinance"
    except Exception as e:
        print(f"  Level 3 失敗: {e}")

    return None, None


# ==========================================
# 3. 櫃買指數 (OTC) 兩層備援抓取
# ==========================================
def fetch_otc():
    # Level 1: FinMind（櫃買指數在 FinMind 的代碼是 TPEx）
    print("抓取櫃買指數 -> [Level 1: FinMind TPEx]")
    try:
        df = pd.DataFrame(finmind("TaiwanStockPrice", "TPEx"))
        df['price'] = df['close']
        df['volume'] = df['Trading_money'] / 100000000
        return build_kline_output(df), "FinMind"
    except Exception as e:
        print(f"  Level 1 失敗: {e}")

    # Level 2: yfinance (^TWOII)
    print("抓取櫃買指數 -> [Level 2: yfinance]")
    try:
        ticker = yf.Ticker("^TWOII")
        df = ticker.history(period="4mo").reset_index()
        if not df.empty:
            df['date'] = pd.to_datetime(df['Date']).dt.strftime('%Y-%m-%d')
            df['price'] = df['Close']
            df['volume'] = df['Volume'] / 100000000
            return build_kline_output(df), "yfinance"
    except Exception as e:
        print(f"  Level 2 失敗: {e}")

    return None, None


# ==========================================
# 4. 外資台指期淨未平倉
# ==========================================
def fetch_foreign_futures():
    """外資台指期（TX）淨未平倉 = 多方未平倉口數 - 空方未平倉口數。回傳 (日期, 淨口數, 較前一日)。"""
    print("抓取外資台指期淨未平倉 -> [FinMind]")
    rows = [r for r in finmind("TaiwanFuturesInstitutionalInvestors", "TX", days=30)
            if r["institutional_investors"] == "外資"]
    rows.sort(key=lambda r: r["date"])
    if len(rows) < 2:
        raise RuntimeError("外資台指期資料不足兩天")
    net = [r["long_open_interest_balance_volume"] - r["short_open_interest_balance_volume"] for r in rows]
    return rows[-1]["date"], net[-1], net[-1] - net[-2]


# ==========================================
# 5. 小台散戶多空比
# ==========================================
def fetch_retail():
    """散戶多空比 = -(三大法人小台淨未平倉 / 小台全市場未平倉) * 100。
    全市場未平倉只算一般交易時段（trading_session = position），所有月份加總。
    回傳 [(日期, 多空比)]，依日期排序。"""
    print("抓取小台散戶多空比 -> [FinMind]")
    inst_net = {}
    for r in finmind("TaiwanFuturesInstitutionalInvestors", "MTX"):
        inst_net[r["date"]] = inst_net.get(r["date"], 0) + \
            r["long_open_interest_balance_volume"] - r["short_open_interest_balance_volume"]
    total_oi = {}
    for r in finmind("TaiwanFuturesDaily", "MTX"):
        if r.get("trading_session") == "position":
            total_oi[r["date"]] = total_oi.get(r["date"], 0) + r["open_interest"]
    out = [(d, round(-100.0 * inst_net[d] / total_oi[d], 2))
           for d in sorted(inst_net) if total_oi.get(d)]
    if not out:
        raise RuntimeError("小台法人與全市場未平倉沒有共同日期")
    return out


# ==========================================
# 6. 主程序執行
# ==========================================
def main():
    now_str = now_tw().strftime("%Y-%m-%d %H:%M")

    taiex_res, taiex_src = fetch_taiex()
    otc_res, otc_src = fetch_otc()
    errors = []
    if not taiex_res:
        errors.append("加權指數")
    if not otc_res:
        errors.append("櫃買指數")
    try:
        foreign_date, foreign_net, foreign_change = fetch_foreign_futures()
    except Exception as e:
        print(f"  外資台指期抓取失敗: {e}")
        errors.append("外資台指期")
    try:
        retail = fetch_retail()
    except Exception as e:
        print(f"  散戶多空比計算失敗: {e}")
        errors.append("散戶多空比")

    # 任何一項失敗：不覆蓋 data.json，並以錯誤結束，讓 Actions 顯示失敗
    if errors:
        print(f"❌ 抓取失敗：{'、'.join(errors)}。暫不更新 data.json。")
        sys.exit(1)

    # 資料日期以加權指數最後一個交易日為準（假日執行時不會把日期改成當天）
    data_date = taiex_res["isoDates"][-1]

    retail = retail[-60:]
    taiex_by_date = dict(zip(taiex_res["isoDates"], taiex_res["chart"]["prices"]))

    final_data = {
        "date": data_date,
        "updateTime": now_str,
        # 以下尚未接資料，前端顯示「—」
        "sentiment": None,
        "sentimentStatus": "未計算",
        "foreignFutures": int(foreign_net),
        "foreignChange": int(foreign_change),
        "foreignNote": f"外資台指期淨未平倉（{foreign_date}）",
        "retailSmall": retail[-1][1],
        "retailMicro": None,
        "pcRatio": None,
        "optionCall": None,
        "optionPut": None,
        "retailLong": None,
        "retailShort": None,
        "analysis": (f"資料日期 {data_date}，於 {now_str}（台灣時間）更新。來源：加權指數 {taiex_src}、"
                     f"櫃買指數 {otc_src}、外資台指期與小台散戶多空比 FinMind。"
                     "情緒指數、微台散戶、P/C ratio、外資選擇權、散戶多空口數尚未接資料。"),
        "taiex": {
            "price": taiex_res["price"],
            "change": taiex_res["change"],
            "changePercent": taiex_res["changePercent"]
        },
        "otc": {
            "price": otc_res["price"],
            "change": otc_res["change"],
            "changePercent": otc_res["changePercent"]
        },
        "taiexChart": taiex_res["chart"],
        "otcChart": otc_res["chart"],
        "retailChart": {
            "dates": [datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d") for d, _ in retail],
            "retailRatios": [r for _, r in retail],
            # 依日期對齊加權指數；加權沒有那天的資料就留空
            "indexValues": [taiex_by_date.get(d) for d, _ in retail]
        }
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=4)

    print(f"\n✅ [{now_str}] data.json 已更新（資料日期 {data_date}）")


if __name__ == "__main__":
    main()
