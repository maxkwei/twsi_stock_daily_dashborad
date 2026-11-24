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
      params={"date": day.replace("-", ""), "type": "MS", "response": "json"},
      headers=HEADERS,
      timeout=15,
  ).json()
  for t in res.get("tables", []):
    if "漲跌證券數合計" in t.get("title", ""):
      col = t["fields"].index("股票")
      cnt = {
          row[0][:2]: int(row[col].split("(")[0].replace(",", ""))
          for row in t["data"]
      }
      break
  else:
    raise RuntimeError(f"證交所 {day} 沒有漲跌家數表")
  y, m, d = day.split("-")
  otc = requests.get(
      "https://www.tpex.org.tw/web/stock/aftertrading/market_highlight/highlight_result.php",
      params={"l": "zh-tw", "d": f"{int(y) - 1911}/{m}/{d}", "o": "json"},
      headers=HEADERS,
      timeout=15,
  ).json()
  tbl = next((t for t in otc.get("tables", []) if t.get("data")), None)
  if tbl is None:
    raise RuntimeError(f"櫃買中心 {day} 沒有彙總資訊")
  row, f = tbl["data"][0], tbl["fields"]
  o_up = int(row[f.index("上漲家數")].replace(",", ""))
  o_dn = int(row[f.index("下跌家數")].replace(",", ""))
  return (
      cnt["上漲"] + o_up,
      cnt["下跌"] + o_dn,
      f"上市 {cnt['上漲']}／{cnt['下跌']}＋上櫃 {o_up}／{o_dn}",
  )


def fetch_institutional_flow():
  print("抓取三大法人買賣超 -> [FinMind]")
  rows = [
      r
      for r in finmind(
          "TaiwanStockTotalInstitutionalInvestors",
          "",
          days=SENTIMENT_LOOKBACK_DAYS,
      )
      if r["name"] == "total"
  ]
  return sorted((r["date"], r["buy"] - r["sell"]) for r in rows)


def compute_sentiment(data_date, taiex_closes, foreign_series):
  """子分數：漲跌家數、大盤RSI、三大法人、外資期貨、美股VIX、台股VIX（若有）。
    回傳 (分數, 標籤, 子分數 dict, 說明, 子分數明細, 美股VIX資料, 美股VIX圖表, 台股VIX資料)。"""
  scores, notes, details = {}, [], {}
  us_vix_info, us_vix_chart = None, None
  tw_vix_info = None

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
    scores["三大法人"] = percentile_rank(
        [v for _, v in flow][-RANK_WINDOW:], flow[-1][1]
    )
    notes.append(f"法人{flow[-1][1] / 1e8:+.0f}億（{flow[-1][0]}）")
    details["三大法人"] = f"買賣超 {flow[-1][1] / 1e8:+,.0f} 億・一年排名"
  except Exception as e:
    print(f"  三大法人失敗: {e}")

  if foreign_series:
    scores["外資期貨"] = percentile_rank(
        foreign_series[-RANK_WINDOW:], foreign_series[-1]
    )
    details["外資期貨"] = f"淨未平倉 {foreign_series[-1]:+,} 口・一年排名"

  try:
    us_vix_info, us_vix_chart = fetch_us_vix_and_chart()
    scores["美股VIX"] = us_vix_info["sentimentScore"]
    notes.append(f"美VIX {us_vix_info['price']}（{us_vix_info['date']}）")
    details["美股VIX"] = (
        f"VIX {us_vix_info['price']}（{us_vix_info['light']}・{us_vix_info['date']}）"
    )
  except Exception as e:
    print(f"  美股 VIX 失敗: {e}")

  tw_vix_info = fetch_tw_vix()
  if tw_vix_info and tw_vix_info.get("sentimentScore") is not None:
    scores["台股VIX"] = tw_vix_info["sentimentScore"]
    notes.append(f"台VIX {tw_vix_info['price']}（{tw_vix_info['date']}）")
    details["台股VIX"] = (
        f"VIX {tw_vix_info['price']}（{tw_vix_info['light']}・{tw_vix_info['date']}）"
    )

  if not scores:
    return None, "未計算", {}, "", [], us_vix_info, us_vix_chart, tw_vix_info

  composite = round(sum(scores.values()) / len(scores), 1)
  label = (
      "極度恐懼"
      if composite < 25
      else (
          "恐懼"
          if composite < 45
          else "中性" if composite < 55 else "貪婪" if composite < 75 else "極度貪婪"
      )
  )
  detail = "、".join(f"{k} {v:.0f}" for k, v in scores.items())
  items = [
      {"name": k, "score": round(v, 1), "detail": details.get(k, "")}
      for k, v in scores.items()
  ]
  return (
      composite,
      label,
      scores,
      f"{detail}（{'；'.join(notes)}）" if notes else detail,
      items,
      us_vix_info,
      us_vix_chart,
      tw_vix_info,
  )


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
    errors.append("櫃買指數")
  try:
    foreign_date, foreign_net, foreign_change, foreign_series = (
        fetch_foreign_futures()
    )
  except Exception as e:
    print(f"  外資台指期抓取失敗: {e}")
    errors.append("外資台指期")
  try:
    retail = fetch_retail("MTX", "小台")
  except Exception as e:
    print(f"  小台散戶多空比計算失敗: {e}")
    errors.append("小台散戶多空比")
  try:
    micro = fetch_retail("TMF", "微台")
  except Exception as e:
    print(f"  微台散戶多空比計算失敗: {e}")
    errors.append("微台散戶多空比")
  try:
    opt_date, opt_call, opt_put = fetch_foreign_options()
  except Exception as e:
    print(f"  外資選擇權抓取失敗: {e}")
    errors.append("外資選擇權")
  try:
    pc_date, pc_ratio = fetch_pc_ratio()
  except Exception as e:
    print(f"  Put/Call 未平倉比抓取失敗: {e}")
    errors.append("Put/Call 未平倉比")

  if errors:
    print(f"❌ 抓取失敗：{'、'.join(errors)}。暫不更新 data.json。")
    sys.exit(1)

  data_date = taiex_res["isoDates"][-1]
  retail = retail[-CHART_DAYS:]
  taiex_by_date = dict(zip(taiex_res["isoDates"], taiex_res["chart"]["close"]))
  retail_date, retail_ratio, retail_long, retail_short = retail[-1]
  micro_date, micro_ratio = micro[-1][0], micro[-1][1]

  (
      sentiment,
      sentiment_label,
      _,
      sentiment_detail,
      sentiment_items,
      us_vix_info,
      us_vix_chart,
      tw_vix_info,
  ) = compute_sentiment(data_date, taiex_res["chart"]["close"], foreign_series)

  vix_source_parts = []
  if us_vix_info:
    vix_source_parts.append(f"美股 VIX yfinance（{us_vix_info['date']}）")
  if tw_vix_info:
    vix_source_parts.append(f"台股 VIX FinMind（{tw_vix_info['date']}）")
  vix_source_text = ("、" + "、".join(vix_source_parts)) if vix_source_parts else ""

  final_data = {
      "date": data_date,
      "updateTime": now_str,
      "chartDays": CHART_DAYS,
      "sentiment": sentiment,
      "sentimentStatus": sentiment_label,
      "sentimentScores": sentiment_items,
      "usVix": us_vix_info,
      "twVix": tw_vix_info,
      "foreignFutures": int(foreign_net),
      "foreignChange": int(foreign_change),
      "foreignNote": f"外資台指期淨未平倉（{foreign_date}）",
      "retailSmall": retail_ratio,
      "retailMicro": micro_ratio,
      "pcRatio": pc_ratio,
      "optionCall": int(opt_call),
      "optionPut": int(opt_put),
      "retailLong": int(retail_long),
      "retailShort": int(retail_short),
      "analysis": (
          f"資料日期 {data_date}，於 {now_str}（台灣時間）更新。來源：加權指數"
          f" {taiex_src}、櫃買指數 {otc_src}、外資台指期與小台／微台散戶多空比"
          f" FinMind（{retail_date}／{micro_date}）、外資選擇權"
          f" FinMind（{opt_date}）、P/C 未平倉比"
          f" 期交所（{pc_date}）{vix_source_text}。情緒指數子分數：{sentiment_detail}。"
      ),
      "taiex": {
          "price": taiex_res["price"],
          "change": taiex_res["change"],
          "changePercent": taiex_res["changePercent"],
      },
      "otc": {
          "price": otc_res["price"],
          "change": otc_res["change"],
          "changePercent": otc_res["changePercent"],
      },
      "taiexChart": taiex_res["chart"],
      "otcChart": otc_res["chart"],
      "usVixChart": us_vix_chart,
      "retailChart": {
          "dates": [
              datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d")
              for d, *_ in retail
          ],
          "retailRatios": [r for _, r, *_ in retail],
          "indexValues": [taiex_by_date.get(d) for d, *_ in retail],
      },
  }

  with open("data.json", "w", encoding="utf-8") as f:
    json.dump(final_data, f, ensure_ascii=False, indent=4)

  print(f"\n✅ [{now_str}] data.json 已更新（資料日期 {data_date}）")


if __name__ == "__main__":
  main()
