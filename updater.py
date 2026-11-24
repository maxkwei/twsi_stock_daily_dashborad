
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
TW = ZoneInfo("Asia/Taipei")
CHART_DAYS = 90
INDEX_FETCH_DAYS = 260
RETAIL_FETCH_DAYS = 150
SENTIMENT_LOOKBACK_DAYS = 400
RANK_WINDOW = 250

UP_COLOR = "#de350b"
DOWN_COLOR = "#1a1a1a"


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


def build_kline_output(df):
  """df 需有 date, open, high, low, close, volume 欄位（volume 單位：億元）。"""
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


def vix_to_gauge_score(vix_val, low_vix=12.0, high_vix=40.0):
  """把 VIX 轉成與半圓情緒儀表板一致的 0~100 分數（VIX 越低越平靜/分數越高，越高越恐慌/分數越低）。"""
  score = (high_vix - float(vix_val)) / (high_vix - low_vix) * 100.0
  return round(max(0.0, min(100.0, score)), 1)


def score_to_status(score):
  return (
      "極度恐懼"
      if score < 25
      else (
          "恐懼"
          if score < 45
          else "中性" if score < 55 else "貪婪" if score < 75 else "極度貪婪"
      )
  )


def fetch_us_vix_data():
  """抓取美股 VIX (^VIX) 最新值、半圓分數與近 CHART_DAYS 序列 {iso_date: close}。"""
  print("抓取美股 VIX -> [yfinance ^VIX]")
  hist = yf.Ticker("^VIX").history(period="6m")
  if hist is None or hist.empty:
    raise RuntimeError("yfinance ^VIX 沒有資料")

  df = hist.reset_index().dropna(subset=["Close"]).copy()
  df["date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
  df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
  df["Close"] = df["Close"].astype(float)

  latest = round(float(df["Close"].iloc[-1]), 2)
  prev = round(float(df["Close"].iloc[-2]), 2) if len(df) >= 2 else latest
  change = round(latest - prev, 2)
  change_pct = round((change / prev) * 100.0, 2) if prev else 0.0
  trade_date = str(df["date"].iloc[-1])
  gauge_score = vix_to_gauge_score(latest)

  recent = df.tail(CHART_DAYS)
  series_map = {
      str(d): round(float(c), 2)
      for d, c in zip(recent["date"], recent["Close"])
  }

  vix_info = {
      "date": trade_date,
      "price": latest,
      "change": change,
      "changePercent": change_pct,
      "score": gauge_score,
      "status": score_to_status(gauge_score),
  }
  return vix_info, series_map


def fetch_tw_vix_data():
  """從期交所 vixMinNew 抓取台股 VIX 最新值、半圓分數與歷史序列 {iso_date: close}。"""
  print("抓取台股 VIX -> [期交所 vixMinNew]")
  html = requests.get(
      "https://www.taifex.com.tw/cht/7/vixMinNew",
      headers=HEADERS,
      timeout=15,
  ).text
  records = []
  for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
    tds = [
        re.sub(r"<[^>]+>|\s+", "", x)
        for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
    ]
    if len(tds) >= 2 and re.match(r"^20\d\d/\d{1,2}/\d{1,2}$", tds[0]):
      m = re.search(r"(\d+(?:\.\d+)?)", tds[1].replace(",", ""))
      if m:
        d_str = datetime.strptime(tds[0], "%Y/%m/%d").strftime("%Y-%m-%d")
        records.append((d_str, round(float(m.group(1)), 2)))

  if not records:
    raise RuntimeError("期交所 vixMinNew 找不到資料列")

  records.sort(key=lambda x: x[0])
  recent = records[-CHART_DAYS:]
  latest_date, latest_val = records[-1]
  prev_val = records[-2][1] if len(records) >= 2 else latest_val
  change = round(latest_val - prev_val, 2)
  change_pct = round((change / prev_val) * 100.0, 2) if prev_val else 0.0
  gauge_score = vix_to_gauge_score(latest_val)

  tw_info = {
      "date": latest_date,
      "price": latest_val,
      "change": change,
      "changePercent": change_pct,
      "score": gauge_score,
      "status": score_to_status(gauge_score),
  }
  series_map = {d: v for d, v in recent}
  return tw_info, series_map


def build_vix_compare_chart(us_map, tw_map):
  """將美股 VIX 與台股 VIX 依日期合併成同一張對照圖表資料。"""
  all_dates = sorted(set(us_map.keys()) | set(tw_map.keys()))[-CHART_DAYS:]
  return {
      "dates": [
          datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d") for d in all_dates
      ],
      "usClose": [us_map.get(d) for d in all_dates],
      "twClose": [tw_map.get(d) for d in all_dates],
  }


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
  scores, notes, details = {}, [], {}
  us_vix_info, us_map = None, {}
  tw_vix_info, tw_map = None, {}

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
    us_vix_info, us_map = fetch_us_vix_data()
  except Exception as e:
    print(f"  美股 VIX 失敗: {e}")

  try:
    tw_vix_info, tw_map = fetch_tw_vix_data()
  except Exception as e:
    print(f"  台股 VIX 失敗: {e}")

  vix_compare_chart = build_vix_compare_chart(us_map, tw_map)

  if not scores:
    return (
        None,
        "未計算",
        {},
        "",
        [],
        us_vix_info,
        tw_vix_info,
        vix_compare_chart,
    )

  composite = round(sum(scores.values()) / len(scores), 1)
  label = score_to_status(composite)
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
      tw_vix_info,
      vix_compare_chart,
  )


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
      tw_vix_info,
      vix_compare_chart,
  ) = compute_sentiment(data_date, taiex_res["chart"]["close"], foreign_series)

  vix_sources = []
  if us_vix_info:
    vix_sources.append(f"美股 VIX yfinance（{us_vix_info['date']}）")
  if tw_vix_info:
    vix_sources.append(f"台股 VIX 期交所（{tw_vix_info['date']}）")
  vix_source_text = ("、" + "、".join(vix_sources)) if vix_sources else ""

  final_data = {
      "date": data_date,
      "updateTime": now_str,
      "chartDays": CHART_DAYS,
      "sentiment": sentiment,
      "sentimentStatus": sentiment_label,
      "sentimentScores": sentiment_items,
      "usVix": us_vix_info,
      "twVix": tw_vix_info,
      "vixCompareChart": vix_compare_chart,
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
