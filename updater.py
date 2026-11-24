from datetime import datetime, timedelta
import json
import re
import sys
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import yfinance as yf

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
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
# 百分位只取最近 250 個交易日（約一年）
RANK_WINDOW = 250

UP_COLOR = "#de350b"  # 紅K
DOWN_COLOR = "#1a1a1a"  # 黑K


def now_tw():
  return datetime.now(TW)


def finmind(dataset, data_id, days=120):
  """FinMind 免費版（不帶 token）。回傳 data 列表，失敗丟例外。"""
  start_date = (now_tw() - timedelta(days=days)).strftime("%Y-%m-%d")
  res = requests.get(
      FINMIND_URL,
      params={
          "dataset": dataset,
          "data_id": data_id,
          "start_date": start_date,
      },
      headers=HEADERS,
      timeout=15,
  ).json()
  if res.get("msg") != "success" or not res.get("data"):
    raise RuntimeError(
        f"FinMind {dataset}/{data_id} 沒有資料："
        f" {res.get('msg') or str(res)[:120]}"
    )
  return res["data"]


# ==========================================
# 1. 通用 K 線處理（開高低收、20MA / 60MA、成交量顏色）
# ==========================================
def build_kline_output(df):
  """df 需有 date, open, high, low, close, volume 欄位（volume 單位：億元）。
    均線用全部抓到的資料計算，再取最後 CHART_DAYS 天輸出。"""
  df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
  for col in ("open", "high", "low", "close", "volume"):
    df[col] = df[col].astype(float)

  df["ma20"] = df["close"].rolling(window=20, min_periods=1).mean()
  df["ma60"] = df["close"].rolling(window=60, min_periods=1).mean()

  recent = df.tail(CHART_DAYS)
  iso_dates = [str(d)[:10] for d in recent["date"]]
  r2 = lambda col: [round(x, 2) for x in recent[col].tolist()]

  closes = df["close"].tolist()
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
          "dates": [
              datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d")
              for d in iso_dates
          ],
          "open": r2("open"),
          "high": r2("high"),
          "low": r2("low"),
          "close": r2("close"),
          "prices": r2("close"),
          "ma20": r2("ma20"),
          "ma60": r2("ma60"),
          "volumes": r2("volume"),
          "volumeColors": [
              UP_COLOR if c >= o else DOWN_COLOR
              for o, c in zip(recent["open"], recent["close"])
          ],
      },
  }


# ==========================================
# 2. 加權指數／櫃買指數（開高低收），兩層備援
# ==========================================
def fetch_index(name, finmind_id, yf_symbol):
  print(f"抓取{name} -> [Level 1: FinMind {finmind_id}]")
  try:
    df = pd.DataFrame(
        finmind("TaiwanStockPrice", finmind_id, days=INDEX_FETCH_DAYS)
    )
    df = df.rename(columns={"max": "high", "min": "low"})
    df["volume"] = df["Trading_money"] / 100000000
    return build_kline_output(df), "FinMind"
  except Exception as e:
    print(f"  Level 1 失敗: {e}")

  print(f"抓取{name} -> [Level 2: yfinance {yf_symbol}]")
  try:
    df = yf.Ticker(yf_symbol).history(period="1y").reset_index()
    if not df.empty:
      df["date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
      df = df.rename(
          columns={
              "Open": "open",
              "High": "high",
              "Low": "low",
              "Close": "close",
          }
      )
      df["volume"] = df["Volume"] / 100000000
      return build_kline_output(df), "yfinance"
  except Exception as e:
    print(f"  Level 2 失敗: {e}")

  return None, None


# ==========================================
# 3. 外資台指期淨未平倉
# ==========================================
def fetch_foreign_futures():
  print("抓取外資台指期淨未平倉 -> [FinMind]")
  rows = [
      r
      for r in finmind(
          "TaiwanFuturesInstitutionalInvestors",
          "TX",
          days=SENTIMENT_LOOKBACK_DAYS,
      )
      if r["institutional_investors"] == "外資"
  ]
  rows.sort(key=lambda r: r["date"])
  if len(rows) < 2:
    raise RuntimeError("外資台指期資料不足兩天")
  net = [
      r["long_open_interest_balance_volume"]
      - r["short_open_interest_balance_volume"]
      for r in rows
  ]
  return rows[-1]["date"], net[-1], net[-1] - net[-2], net


# ==========================================
# 4. 小台／微台散戶多空比與多空口數
# ==========================================
def fetch_retail(product="MTX", label="小台"):
  print(f"抓取{label}散戶多空比 -> [FinMind {product}]")
  inst_long, inst_short = {}, {}
  for r in finmind(
      "TaiwanFuturesInstitutionalInvestors", product, days=RETAIL_FETCH_DAYS
  ):
    inst_long[r["date"]] = (
        inst_long.get(r["date"], 0) + r["long_open_interest_balance_volume"]
    )
    inst_short[r["date"]] = (
        inst_short.get(r["date"], 0) + r["short_open_interest_balance_volume"]
    )
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
    out.append((
        d,
        round(100.0 * (retail_long - retail_short) / oi, 2),
        retail_long,
        retail_short,
    ))
  if not out:
    raise RuntimeError(f"{label}法人與全市場未平倉沒有共同日期")
  return out


# ==========================================
# 5. 選擇權 Put/Call 未平倉比（期交所官網）
# ==========================================
def fetch_pc_ratio():
  print("抓取 Put/Call 未平倉比 -> [期交所]")
  html = requests.get(
      "https://www.taifex.com.tw/cht/3/pcRatio", headers=HEADERS, timeout=15
  ).text
  for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
    tds = [
        re.sub(r"<[^>]+>|\s+", "", x)
        for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
    ]
    if len(tds) >= 7 and re.match(r"20\d\d/\d+/\d+$", tds[0]):
      date = datetime.strptime(tds[0], "%Y/%m/%d").strftime("%Y-%m-%d")
      return date, float(tds[6].replace(",", ""))
  raise RuntimeError("期交所 P/C 頁面找不到資料列")


# ==========================================
# 6. 外資台指選擇權淨未平倉
# ==========================================
def fetch_foreign_options():
  print("抓取外資選擇權 -> [FinMind TXO]")
  rows = [
      r
      for r in finmind("TaiwanOptionInstitutionalInvestors", "TXO", days=15)
      if r["institutional_investors"] == "外資"
  ]
  if not rows:
    raise RuntimeError("沒有外資選擇權資料")
  last = max(r["date"] for r in rows)
  net = {
      r["call_put"]: r["long_open_interest_balance_volume"]
      - r["short_open_interest_balance_volume"]
      for r in rows
      if r["date"] == last
  }
  if "買權" not in net or "賣權" not in net:
    raise RuntimeError(f"{last} 外資選擇權缺買權或賣權")
  return last, net["買權"], net["賣權"]


# ==========================================
# 6.5 美股 VIX（轉為與台股相同圖表結構）與台股 VIX 情緒分
# ==========================================
def vix_to_sentiment(vix_val, low_vix=12.0, high_vix=40.0):
  """VIX 轉 0~100 情緒分：VIX 越低分越高（偏平靜），VIX 越高分越低（越恐慌）。"""
  score = (high_vix - float(vix_val)) / (high_vix - low_vix) * 100.0
  return round(max(0.0, min(100.0, score)), 1)


def classify_vix_light(vix_val):
  val = float(vix_val)
  if val >= 30:
    return "紅燈・高恐慌"
  if val >= 20:
    return "黃燈・警戒升溫"
  if val >= 15:
    return "藍燈・常態波動"
  return "綠燈・低波動"


def fetch_us_vix_and_chart():
  """抓取美股 VIX (^VIX) 最新值、燈號、情緒分，以及與台股同格式的近 CHART_DAYS 圖表資料。"""
  print("抓取美股 VIX 與走勢圖 -> [yfinance ^VIX]")
  hist = yf.Ticker("^VIX").history(period="1y")
  if hist is None or hist.empty:
    raise RuntimeError("yfinance ^VIX 沒有資料")

  df = hist.reset_index().dropna(subset=["Close"]).copy()
  df["date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
  df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
  for col in ("Open", "High", "Low", "Close"):
    df[col] = df[col].astype(float)

  df["ma20"] = df["Close"].rolling(window=20, min_periods=1).mean()
  df["ma60"] = df["Close"].rolling(window=60, min_periods=1).mean()

  recent = df.tail(CHART_DAYS)
  iso_dates = recent["date"].tolist()
  r2 = lambda s: [round(float(x), 2) for x in s.tolist()]

  latest = round(float(df["Close"].iloc[-1]), 2)
  prev = round(float(df["Close"].iloc[-2]), 2) if len(df) >= 2 else latest
  change = round(latest - prev, 2)
  change_pct = round((change / prev) * 100.0, 2) if prev else 0.0
  trade_date = iso_dates[-1]
  sentiment_score = vix_to_sentiment(latest)
  light = classify_vix_light(latest)

  info = {
      "date": trade_date,
      "price": latest,
      "change": change,
      "changePercent": change_pct,
      "light": light,
      "sentimentScore": sentiment_score,
  }
  # 與台股圖表結構對齊（dates, open, high, low, close, prices, ma20, ma60）
  chart = {
      "dates": [
          datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d")
          for d in iso_dates
      ],
      "open": r2(recent["Open"]),
      "high": r2(recent["High"]),
      "low": r2(recent["Low"]),
      "close": r2(recent["Close"]),
      "prices": r2(recent["Close"]),
      "ma20": r2(recent["ma20"]),
      "ma60": r2(recent["ma60"]),
  }
  return info, chart


def fetch_tw_vix():
  """嘗試抓取台股 VIX（臺指波動率指數）；若來源暫無資料則安全回傳 None。"""
  print("嘗試抓取台股 VIX...")
  candidate_datasets = [
      ("TaiwanOptionVIX", ""),
      ("TaiwanStockVIX", ""),
  ]
  for dataset, data_id in candidate_datasets:
    try:
      rows = finmind(dataset, data_id, days=30)
      if rows:
        df = pd.DataFrame(rows)
        val_col = next(
            (c for c in ("Close", "close", "vix", "VIX", "value") if c in df.columns),
            None,
        )
        date_col = next(
            (c for c in ("date", "Date") if c in df.columns),
            None,
        )
        if val_col and date_col:
          df = df.dropna(subset=[val_col]).sort_values(date_col)
          if not df.empty:
            latest = round(float(df[val_col].iloc[-1]), 2)
            prev = (
                round(float(df[val_col].iloc[-2]), 2)
                if len(df) >= 2
                else latest
            )
            change = round(latest - prev, 2)
            trade_date = str(df[date_col].iloc[-1])[:10]
            return {
                "date": trade_date,
                "price": latest,
                "change": change,
                "light": classify_vix_light(latest),
                "sentimentScore": vix_to_sentiment(latest),
            }
    except Exception:
      continue

  print("  台股 VIX 公開/FinMind 欄位未命中，暫略過台股VIX子分數")
  return None


# ==========================================
# 7. 台股情緒指數（0~100，子分數平均）
# ==========================================
def percentile_rank(values, today):
  return sum(1 for v in values if v <= today) / len(values) * 100


def rsi(closes, n=6):
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
  print("抓取漲跌家數 -> [證交所＋櫃買中心]")
  res = requests.get(
      "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
