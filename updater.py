import json
import re
import sys
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
}
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
# GitHub Actions 的機器是 UTC，時間一律換成台灣時間
TW = ZoneInfo("Asia/Taipei")
# 圖表顯示的交易日數；每天新增一根，最舊的一根往左移出
CHART_DAYS = 90
# 抓的日曆天數：要夠 CHART_DAYS + 60MA 暖身（約 150 個交易日）
INDEX_FETCH_DAYS = 260
RETAIL_FETCH_DAYS = 150

UP_COLOR = '#de350b'     # 紅K
DOWN_COLOR = '#1a1a1a'   # 黑K


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
# 1. 通用 K 線處理（開高低收、20MA / 60MA、成交量顏色）
# ==========================================
def build_kline_output(df):
    """df 需有 date, open, high, low, close, volume 欄位（volume 單位：億元）。
    均線用全部抓到的資料計算，再取最後 CHART_DAYS 天輸出。"""
    df = df.sort_values('date').drop_duplicates('date').reset_index(drop=True)
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = df[col].astype(float)

    df['ma20'] = df['close'].rolling(window=20, min_periods=1).mean()
    df['ma60'] = df['close'].rolling(window=60, min_periods=1).mean()

    recent = df.tail(CHART_DAYS)
    iso_dates = [str(d)[:10] for d in recent['date']]
    r2 = lambda col: [round(x, 2) for x in recent[col].tolist()]

    closes = df['close'].tolist()
    latest_price = round(closes[-1], 2)
    prev_price = round(closes[-2], 2)
    change = round(latest_price - prev_price, 2)
    change_pct = round((change / prev_price) * 100, 2)

    return {
        "price": latest_price,
        "change": change,
        "changePercent": change_pct,
        # 只給 main() 用（資料日期、散戶圖對齊），不寫進 data.json
        "isoDates": iso_dates,
        "chart": {
            "dates": [datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d") for d in iso_dates],
            "open": r2('open'),
            "high": r2('high'),
            "low": r2('low'),
            "close": r2('close'),
            # prices 與 close 相同，保留給舊欄位名稱
            "prices": r2('close'),
            "ma20": r2('ma20'),
            "ma60": r2('ma60'),
            "volumes": r2('volume'),
            # 成交量顏色跟著當天 K 棒：收 >= 開為紅，否則黑
            "volumeColors": [UP_COLOR if c >= o else DOWN_COLOR
                             for o, c in zip(recent['open'], recent['close'])]
        }
    }


# ==========================================
# 2. 加權指數／櫃買指數（開高低收），兩層備援
# ==========================================
def fetch_index(name, finmind_id, yf_symbol):
    # Level 1: FinMind（加權 TAIEX、櫃買 TPEx，有開高低收與成交金額）
    print(f"抓取{name} -> [Level 1: FinMind {finmind_id}]")
    try:
        df = pd.DataFrame(finmind("TaiwanStockPrice", finmind_id, days=INDEX_FETCH_DAYS))
        df = df.rename(columns={'max': 'high', 'min': 'low'})
        df['volume'] = df['Trading_money'] / 100000000
        return build_kline_output(df), "FinMind"
    except Exception as e:
        print(f"  Level 1 失敗: {e}")

    # Level 2: yfinance（成交量單位與 FinMind 不同，只當備援）
    print(f"抓取{name} -> [Level 2: yfinance {yf_symbol}]")
    try:
        df = yf.Ticker(yf_symbol).history(period="1y").reset_index()
        if not df.empty:
            df['date'] = pd.to_datetime(df['Date']).dt.strftime('%Y-%m-%d')
            df = df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close'})
            df['volume'] = df['Volume'] / 100000000
            return build_kline_output(df), "yfinance"
    except Exception as e:
        print(f"  Level 2 失敗: {e}")

    return None, None


# ==========================================
# 3. 外資台指期淨未平倉
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
# 4. 小台散戶多空比與多空口數
# ==========================================
def fetch_retail():
    """全市場未平倉只算一般交易時段（trading_session = position），所有月份加總。
    散戶多單 = 全市場未平倉 - 三大法人多方未平倉
    散戶空單 = 全市場未平倉 - 三大法人空方未平倉
    散戶多空比 = (散戶多單 - 散戶空單) / 全市場未平倉 * 100 = -(三大法人淨未平倉 / 全市場未平倉) * 100
    回傳 [(日期, 多空比, 散戶多單, 散戶空單)]，依日期排序。"""
    print("抓取小台散戶多空比 -> [FinMind]")
    inst_long, inst_short = {}, {}
    for r in finmind("TaiwanFuturesInstitutionalInvestors", "MTX", days=RETAIL_FETCH_DAYS):
        inst_long[r["date"]] = inst_long.get(r["date"], 0) + r["long_open_interest_balance_volume"]
        inst_short[r["date"]] = inst_short.get(r["date"], 0) + r["short_open_interest_balance_volume"]
    total_oi = {}
    for r in finmind("TaiwanFuturesDaily", "MTX", days=RETAIL_FETCH_DAYS):
        if r.get("trading_session") == "position":
            total_oi[r["date"]] = total_oi.get(r["date"], 0) + r["open_interest"]
    out = []
    for d in sorted(inst_long):
        oi = total_oi.get(d)
        if not oi:
            continue
        retail_long, retail_short = oi - inst_long[d], oi - inst_short[d]
        out.append((d, round(100.0 * (retail_long - retail_short) / oi, 2), retail_long, retail_short))
    if not out:
        raise RuntimeError("小台法人與全市場未平倉沒有共同日期")
    return out


# ==========================================
# 5. 選擇權 Put/Call 未平倉比（期交所官網）
# ==========================================
def fetch_pc_ratio():
    """期交所「臺指選擇權 Put/Call Ratio」頁，表格第一列是最新一天。
    欄位：日期、賣權成交量、買權成交量、成交量比率%、賣權未平倉量、買權未平倉量、未平倉量比率%。
    回傳 (日期 YYYY-MM-DD, 未平倉量比率%)。"""
    print("抓取 Put/Call 未平倉比 -> [期交所]")
    html = requests.get("https://www.taifex.com.tw/cht/3/pcRatio", headers=HEADERS, timeout=15).text
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S):
        tds = [re.sub(r'<[^>]+>|\s+', '', x) for x in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(tds) >= 7 and re.match(r'20\d\d/\d+/\d+$', tds[0]):
            date = datetime.strptime(tds[0], "%Y/%m/%d").strftime("%Y-%m-%d")
            return date, float(tds[6].replace(',', ''))
    raise RuntimeError("期交所 P/C 頁面找不到資料列")


# ==========================================
# 6. 主程序執行
# ==========================================
def main():
    now_str = now_tw().strftime("%Y-%m-%d %H:%M")

    taiex_res, taiex_src = fetch_index("加權指數", "TAIEX", "^TWII")
    otc_res, otc_src = fetch_index("櫃買指數", "TPEx", "^TWOII")
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
    try:
        pc_date, pc_ratio = fetch_pc_ratio()
    except Exception as e:
        print(f"  Put/Call 未平倉比抓取失敗: {e}")
        errors.append("Put/Call 未平倉比")

    # 任何一項失敗：不覆蓋 data.json，並以錯誤結束，讓 Actions 顯示失敗
    if errors:
        print(f"❌ 抓取失敗：{'、'.join(errors)}。暫不更新 data.json。")
        sys.exit(1)

    # 資料日期以加權指數最後一個交易日為準（假日執行時不會把日期改成當天）
    data_date = taiex_res["isoDates"][-1]

    retail = retail[-CHART_DAYS:]
    taiex_by_date = dict(zip(taiex_res["isoDates"], taiex_res["chart"]["close"]))
    retail_date, retail_ratio, retail_long, retail_short = retail[-1]

    final_data = {
        "date": data_date,
        "updateTime": now_str,
        "chartDays": CHART_DAYS,
        # 以下尚未接資料，前端顯示「—」
        "sentiment": None,
        "sentimentStatus": "未計算",
        "foreignFutures": int(foreign_net),
        "foreignChange": int(foreign_change),
        "foreignNote": f"外資台指期淨未平倉（{foreign_date}）",
        "retailSmall": retail_ratio,
        "retailMicro": None,
        "pcRatio": pc_ratio,
        "optionCall": None,
        "optionPut": None,
        "retailLong": int(retail_long),
        "retailShort": int(retail_short),
        "analysis": (f"資料日期 {data_date}，於 {now_str}（台灣時間）更新。來源：加權指數 {taiex_src}、"
                     f"櫃買指數 {otc_src}、外資台指期與小台散戶多空比 FinMind（{retail_date}）、"
                     f"P/C 未平倉比 期交所（{pc_date}）。情緒指數、微台散戶、外資選擇權尚未接資料。"),
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
            "dates": [datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d") for d, *_ in retail],
            "retailRatios": [r for _, r, *_ in retail],
            # 依日期對齊加權指數；加權沒有那天的資料就留空
            "indexValues": [taiex_by_date.get(d) for d, *_ in retail]
        }
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=4)

    print(f"\n✅ [{now_str}] data.json 已更新（資料日期 {data_date}）")


if __name__ == "__main__":
    main()
