import json
import re
import sys
import requests
import pandas as pd
import yfinance as yf
#import streamlit as st
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
# 情緒指數百分位的比較基準：一年
SENTIMENT_LOOKBACK_DAYS = 400
# 百分位只取最近 250 個交易日（約一年），與凱基監控儀表板相同
RANK_WINDOW = 250

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
    """外資台指期（TX）淨未平倉 = 多方未平倉口數 - 空方未平倉口數。
    回傳 (日期, 淨口數, 較前一日, 一年的淨口數序列)；序列給情緒指數排百分位用。"""
    print("抓取外資台指期淨未平倉 -> [FinMind]")
    rows = [r for r in finmind("TaiwanFuturesInstitutionalInvestors", "TX", days=SENTIMENT_LOOKBACK_DAYS)
            if r["institutional_investors"] == "外資"]
    rows.sort(key=lambda r: r["date"])
    if len(rows) < 2:
        raise RuntimeError("外資台指期資料不足兩天")
    net = [r["long_open_interest_balance_volume"] - r["short_open_interest_balance_volume"] for r in rows]
    return rows[-1]["date"], net[-1], net[-1] - net[-2], net


# ==========================================
# 4. 小台散戶多空比與多空口數
# ==========================================
def fetch_retail(product="MTX", label="小台"):
    """全市場未平倉只算一般交易時段（trading_session = position），所有月份加總。
    散戶多單 = 全市場未平倉 - 三大法人多方未平倉
    散戶空單 = 全市場未平倉 - 三大法人空方未平倉
    散戶多空比 = (散戶多單 - 散戶空單) / 全市場未平倉 * 100 = -(三大法人淨未平倉 / 全市場未平倉) * 100
    回傳 [(日期, 多空比, 散戶多單, 散戶空單)]，依日期排序。"""
    print(f"抓取{label}散戶多空比 -> [FinMind {product}]")
    inst_long, inst_short = {}, {}
    for r in finmind("TaiwanFuturesInstitutionalInvestors", product, days=RETAIL_FETCH_DAYS):
        inst_long[r["date"]] = inst_long.get(r["date"], 0) + r["long_open_interest_balance_volume"]
        inst_short[r["date"]] = inst_short.get(r["date"], 0) + r["short_open_interest_balance_volume"]
    total_oi = {}
    for r in finmind("TaiwanFuturesDaily", product, days=RETAIL_FETCH_DAYS):
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
        raise RuntimeError(f"{label}法人與全市場未平倉沒有共同日期")
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
# 6. 外資台指選擇權淨未平倉
# ==========================================
def fetch_foreign_options():
    """外資台指選擇權（TXO）買權、賣權各自的淨未平倉 = 多方未平倉口數 - 空方未平倉口數。
    回傳 (日期, 買權淨口數, 賣權淨口數)。"""
    print("抓取外資選擇權 -> [FinMind TXO]")
    rows = [r for r in finmind("TaiwanOptionInstitutionalInvestors", "TXO", days=15)
            if r["institutional_investors"] == "外資"]
    if not rows:
        raise RuntimeError("沒有外資選擇權資料")
    last = max(r["date"] for r in rows)
    net = {r["call_put"]: r["long_open_interest_balance_volume"] - r["short_open_interest_balance_volume"]
           for r in rows if r["date"] == last}
    if "買權" not in net or "賣權" not in net:
        raise RuntimeError(f"{last} 外資選擇權缺買權或賣權")
    return last, net["買權"], net["賣權"]


# ==========================================
# 6.5 美股 VIX 指數（^VIX）與恐慌度換算
# ==========================================
def fetch_us_vix():
    """抓取最新美股 VIX (^VIX) 收盤價、漲跌與 0~100 情緒分數（VIX 越高越恐慌、分數越低）。"""
    print("抓取美股 VIX -> [yfinance ^VIX]")
    hist = yf.Ticker("^VIX").history(period="5d")
    if hist is None or hist.empty:
        raise RuntimeError("yfinance ^VIX 沒有資料")
    closes = hist["Close"].dropna()
    if closes.empty:
        raise RuntimeError("^VIX 收盤價為空")

    latest = round(float(closes.iloc[-1]), 2)
    prev = round(float(closes.iloc[-2]), 2) if len(closes) >= 2 else latest
    change = round(latest - prev, 2)
    change_pct = round((change / prev) * 100.0, 2) if prev != 0 else 0.0
    trade_date = closes.index[-1].strftime("%Y-%m-%d")

    # VIX 12 -> 100 分（低波動/偏樂觀），VIX 40 -> 0 分（極度恐慌）
    raw_score = (40.0 - latest) / (40.0 - 12.0) * 100.0
    sentiment_score = round(max(0.0, min(100.0, raw_score)), 1)

    if latest >= 30:
        light = "紅燈・高恐慌"
    elif latest >= 20:
        light = "黃燈・警戒升溫"
    elif latest >= 15:
        light = "藍燈・常態波動"
    else:
        light = "綠燈・低波動"

    return {
        "date": trade_date,
        "price": latest,
        "change": change,
        "changePercent": change_pct,
        "light": light,
        "sentimentScore": sentiment_score,
    }


# ==========================================
# 7. 台股情緒指數（0~100，子分數平均）
# ==========================================
def percentile_rank(values, today):
    return sum(1 for v in values if v <= today) / len(values) * 100


def rsi(closes, n=6):
    """Wilder 平滑的 RSI。"""
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    if len(gains) < n:
        raise RuntimeError("收盤價不足以計算 RSI")
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)


def fetch_breadth(day):
    """上市＋上櫃股票的上漲／下跌家數。
    上市：證交所每日收盤行情「漲跌證券數合計」表的股票欄；上櫃：櫃買中心「上櫃股票當日彙總資訊」。
    回傳 (上漲, 下跌, 說明)。"""
    print("抓取漲跌家數 -> [證交所＋櫃買中心]")
    res = requests.get("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                       params={"date": day.replace("-", ""), "type": "MS", "response": "json"},
                       headers=HEADERS, timeout=15).json()
    for t in res.get("tables", []):
        if "漲跌證券數合計" in t.get("title", ""):
            col = t["fields"].index("股票")
            cnt = {row[0][:2]: int(row[col].split("(")[0].replace(",", "")) for row in t["data"]}
            break
    else:
        raise RuntimeError(f"證交所 {day} 沒有漲跌家數表")
    y, m, d = day.split("-")
    otc = requests.get("https://www.tpex.org.tw/web/stock/aftertrading/market_highlight/highlight_result.php",
                       params={"l": "zh-tw", "d": f"{int(y) - 1911}/{m}/{d}", "o": "json"},
                       headers=HEADERS, timeout=15).json()
    tbl = next((t for t in otc.get("tables", []) if t.get("data")), None)
    if tbl is None:
        raise RuntimeError(f"櫃買中心 {day} 沒有彙總資訊")
    row, f = tbl["data"][0], tbl["fields"]
    o_up = int(row[f.index("上漲家數")].replace(",", ""))
    o_dn = int(row[f.index("下跌家數")].replace(",", ""))
    return (cnt["上漲"] + o_up, cnt["下跌"] + o_dn,
            f"上市 {cnt['上漲']}／{cnt['下跌']}＋上櫃 {o_up}／{o_dn}")


def fetch_institutional_flow():
    """三大法人現貨買賣超金額（FinMind total 列，元）。回傳 [(日期, 淨額)]。"""
    print("抓取三大法人買賣超 -> [FinMind]")
    rows = [r for r in finmind("TaiwanStockTotalInstitutionalInvestors", "", days=SENTIMENT_LOOKBACK_DAYS)
            if r["name"] == "total"]
    return sorted((r["date"], r["buy"] - r["sell"]) for r in rows)


def compute_sentiment(data_date, taiex_closes, foreign_series):
    """子分數：漲跌家數比、大盤 RSI(6)、三大法人買賣超（一年百分位）、外資台指期淨未平倉（一年百分位）、美股 VIX 情緒分。
    任何一項抓不到就略過，用其餘子分數平均；回傳 (分數, 標籤, 子分數 dict, 說明, 子分數明細, 美股VIX資料)。"""
    scores, notes, details = {}, [], {}
    vix_info = None
    try:
        adv, dec, src = fetch_breadth(data_date)
        scores["漲跌家數"] = adv / (adv + dec) * 100
        notes.append(f"漲{adv}跌{dec}")
        details["漲跌家數"] = f"漲 {adv}／跌 {dec}（{src}）"
    except Exception as e:
        print(f"  漲跌家數失敗: {e}")
    try:
        scores["大盤RSI"] = rsi(taiex_closes)
        details["大盤RSI"] = "加權指數 RSI(6)"
    except Exception as e:
        print(f"  RSI 失敗: {e}")
    try:
        flow = fetch_institutional_flow()
        scores["三大法人"] = percentile_rank([v for _, v in flow][-RANK_WINDOW:], flow[-1][1])
        notes.append(f"法人{flow[-1][1] / 1e8:+.0f}億（{flow[-1][0]}）")
        details["三大法人"] = f"買賣超 {flow[-1][1] / 1e8:+,.0f} 億・一年排名"
    except Exception as e:
        print(f"  三大法人失敗: {e}")
    if foreign_series:
        scores["外資期貨"] = percentile_rank(foreign_series[-RANK_WINDOW:], foreign_series[-1])
        details["外資期貨"] = f"淨未平倉 {foreign_series[-1]:+,} 口・一年排名"
    try:
        vix_info = fetch_us_vix()
        scores["美股VIX"] = vix_info["sentimentScore"]
        notes.append(f"VIX {vix_info['price']}（{vix_info['date']}）")
        details["美股VIX"] = f"VIX {vix_info['price']}（{vix_info['light']}・{vix_info['date']}）"
    except Exception as e:
        print(f"  美股 VIX 失敗: {e}")
    if not scores:
        return None, "未計算", {}, "", [], vix_info
    composite = round(sum(scores.values()) / len(scores), 1)
    label = ("極度恐懼" if composite < 25 else "恐懼" if composite < 45 else "中性" if composite < 55
             else "貪婪" if composite < 75 else "極度貪婪")
    detail = "、".join(f"{k} {v:.0f}" for k, v in scores.items())
    items = [{"name": k, "score": round(v, 1), "detail": details.get(k, "")} for k, v in scores.items()]
    return composite, label, scores, f"{detail}（{'；'.join(notes)}）" if notes else detail, items, vix_info


# ==========================================
# 8. 主程序執行
# ==========================================
def main():
    now_str = now_tw().strftime("%Y-%m-%d %H:%M")

    taiex_res, taiex_src = fetch_index("加權指數", "TAIEX", "^TWII")
    otc_res, otc_src = fetch_index("櫃買指數", "TPEx", "^TWOII")
    errors = []
    if not taiex_res:
        errors.append("加權指數")
    if not otc_res:
        errors.append("櫃


