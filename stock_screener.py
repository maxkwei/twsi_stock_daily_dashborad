import json
import os
from typing import Any, Dict, Optional
from finlab import data, finlab
import pandas as pd
import yfinance as yf


def vix_to_sentiment_score(
    vix_value: float, low_vix: float = 12.0, high_vix: float = 40.0
) -> float:
  """將 VIX 轉化為 0~100 情緒分數：分數越低代表越恐慌，越高代表越平靜/樂觀。"""
  if high_vix <= low_vix:
    return 50.0
  raw = (high_vix - float(vix_value)) / (high_vix - low_vix) * 100.0
  return round(max(0.0, min(100.0, raw)), 2)


def classify_vix_level(vix_value: Optional[float]) -> Dict[str, Any]:
  """回傳 VIX 恐慌燈號與說明。"""
  if vix_value is None or pd.isna(vix_value):
    return {
        "level": "unknown",
        "fear_label": "無資料",
        "light": "灰燈・待更新",
        "color": "#9CA3AF",
        "sentiment_score": None,
    }

  val = float(vix_value)
  score = vix_to_sentiment_score(val)

  if val >= 40.0:
    return {
        "level": "extreme_fear",
        "fear_label": "極度恐慌",
        "light": "紅燈・極度恐慌",
        "color": "#DC2626",
        "sentiment_score": score,
    }
  if val >= 30.0:
    return {
        "level": "high_fear",
        "fear_label": "高恐慌",
        "light": "紅燈・高恐慌",
        "color": "#EF4444",
        "sentiment_score": score,
    }
  if val >= 20.0:
    return {
        "level": "elevated_caution",
        "fear_label": "警戒升溫",
        "light": "黃燈・警戒升溫",
        "color": "#F59E0B",
        "sentiment_score": score,
    }
  if val >= 15.0:
    return {
        "level": "normal",
        "fear_label": "常態波動",
        "light": "藍燈・常態波動",
        "color": "#3B82F6",
        "sentiment_score": score,
    }
  return {
      "level": "low_fear",
      "fear_label": "低波動／偏樂觀",
      "light": "綠燈・低波動",
      "color": "#10B981",
      "sentiment_score": score,
  }


def fetch_us_vix() -> Dict[str, Any]:
  """抓取美股 VIX (^VIX) 最新日線與前日變化。"""
  try:
    hist = yf.Ticker("^VIX").history(period="5d")
    if hist is None or hist.empty:
      return {"status": "error", "value": None, "message": "查無 ^VIX 歷史資料"}

    closes = hist["Close"].dropna()
    if closes.empty:
      return {"status": "error", "value": None, "message": "^VIX 收盤價為空"}

    latest = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) >= 2 else latest
    change = round(latest - prev, 2)
    change_pct = round((change / prev * 100.0), 2) if prev != 0 else 0.0
    trade_date = closes.index[-1].strftime("%Y-%m-%d")

    info = classify_vix_level(latest)
    return {
        "status": "ok",
        "symbol": "^VIX",
        "value": round(latest, 2),
        "prev_close": round(prev, 2),
        "change": change,
        "change_pct": change_pct,
        "as_of_date": trade_date,
        **info,
    }
  except Exception as exc:
    return {
        "status": "error",
        "value": None,
        "message": f"抓取美股 VIX 失敗: {exc}",
    }


def fetch_tw_vix(finlab_data_module: Any = None) -> Dict[str, Any]:
  """嘗試從 FinLab 取得台股波動率指數（找不到則安全回傳 unavailable）。"""
  candidate_keys = [
      "taifex_vix:vix",
      "option_daily_summary:臺指波動率指數",
      "market_index:臺指VIX",
  ]
  if finlab_data_module is not None:
    for key in candidate_keys:
      try:
        df = finlab_data_module.get(key)
        if df is not None and not df.empty:
          series = (
              df.iloc[:, -1].dropna()
              if isinstance(df, pd.DataFrame)
              else df.dropna()
          )
          if not series.empty:
            latest = float(series.iloc[-1])
            prev = float(series.iloc[-2]) if len(series) >= 2 else latest
            change = round(latest - prev, 2)
            date_str = str(series.index[-1])[:10]
            info = classify_vix_level(latest)
            return {
                "status": "ok",
                "source_key": key,
                "value": round(latest, 2),
                "prev_close": round(prev, 2),
                "change": change,
                "as_of_date": date_str,
                **info,
            }
      except Exception:
        continue

  return {
      "status": "unavailable",
      "value": None,
      "message": "FinLab 未命中台股 VIX 欄位，目前以美股 VIX 為主",
      **classify_vix_level(None),
  }


def compute_combined_vix_sentiment(
    us_vix: Dict[str, Any],
    tw_vix: Dict[str, Any],
    us_weight: float = 0.6,
    tw_weight: float = 0.4,
) -> Dict[str, Any]:
  """將美股 VIX 與台股 VIX 恐慌度合併為 0~100 情緒指標。"""
  us_score = us_vix.get("sentiment_score")
  tw_score = tw_vix.get("sentiment_score")

  if us_score is not None and tw_score is not None:
    total_w = us_weight + tw_weight
    combined_score = round(
        (us_score * us_weight + tw_score * tw_weight) / total_w, 2
    )
    mode = "us_and_tw_vix"
  elif us_score is not None:
    combined_score = round(float(us_score), 2)
    mode = "us_vix_only"
  elif tw_score is not None:
    combined_score = round(float(tw_score), 2)
    mode = "tw_vix_only"
  else:
    return {
        "status": "unavailable",
        "sentiment_score": None,
        "fear_index_0_100": None,
        "mood_label": "情緒分數待更新",
        "mode": "none",
    }

  if combined_score < 25:
    mood_label = "極度恐慌（偏空警戒）"
  elif combined_score < 45:
    mood_label = "恐慌升溫（防禦為主）"
  elif combined_score <= 65:
    mood_label = "中性震盪"
  elif combined_score <= 85:
    mood_label = "偏樂觀（順勢觀察）"
  else:
    mood_label = "低波動偏熱（留意追高風險）"

  return {
      "status": "ok",
      "sentiment_score": combined_score,
      "fear_index_0_100": round(100.0 - combined_score, 2),
      "mood_label": mood_label,
      "mode": mode,
  }


def build_vix_sentiment_report(
    finlab_data_module: Any = None,
    output_json_path: str = "vix_sentiment.json",
    output_csv_path: str = "vix_sentiment.csv",
) -> Dict[str, Any]:
  """產出 VIX 情緒報告並輸出 JSON 與 CSV。"""
  us_vix = fetch_us_vix()
  tw_vix = fetch_tw_vix(finlab_data_module=finlab_data_module)
  sentiment = compute_combined_vix_sentiment(us_vix, tw_vix)

  report = {
      "us_vix": us_vix,
      "tw_vix": tw_vix,
      "vix_sentiment": sentiment,
  }

  with open(output_json_path, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

  flat_row = {
      "us_vix_date": us_vix.get("as_of_date"),
      "us_vix": us_vix.get("value"),
      "us_vix_change": us_vix.get("change"),
      "us_vix_light": us_vix.get("light"),
      "us_vix_sentiment_score": us_vix.get("sentiment_score"),
      "tw_vix_date": tw_vix.get("as_of_date"),
      "tw_vix": tw_vix.get("value"),
      "tw_vix_change": tw_vix.get("change"),
      "tw_vix_light": tw_vix.get("light"),
      "tw_vix_sentiment_score": tw_vix.get("sentiment_score"),
      "combined_sentiment_score": sentiment.get("sentiment_score"),
      "fear_index_0_100": sentiment.get("fear_index_0_100"),
      "mood_label": sentiment.get("mood_label"),
  }
  pd.DataFrame([flat_row]).to_csv(
      output_csv_path, index=False, encoding="utf-8-sig"
  )
  return report


def run_screening():
  print("=== 開始執行籌碼、財報篩選與 VIX 情緒指標任務 ===")

  # 1. 取得 FinLab Token 並登入
  api_token = os.environ.get("FINLAB_TOKEN")
  if not api_token:
    raise ValueError("找不到 FINLAB_TOKEN 環境變數，請確認 GitHub Secrets 設定。")
  finlab.login(api_token)

  # 2. 籌碼面：外資與投信近 90 天買超
  print("正在計算外資與投信 90 天累計買超...")
  foreign_buy = data.get(
      "institutional_investors_trading_summary:外陸資買賣超股數(不含外資自營商)"
  )
  trust_buy = data.get("institutional_investors_trading_summary:投信買賣超股數")

  foreign_90d = foreign_buy.tail(90).sum()
  trust_90d = trust_buy.tail(90).sum()

  df_chips = pd.DataFrame(
      {"Foreign_Net_Buy": foreign_90d, "Trust_Net_Buy": trust_90d}
  )
  chips_condition = (df_chips["Foreign_Net_Buy"] > 0) & (
      df_chips["Trust_Net_Buy"] > 0
  )
  selected_chips = df_chips[chips_condition].copy()
  selected_chips["Foreign_Net_Buy_Lots"] = (
      selected_chips["Foreign_Net_Buy"] / 1000
  ).round(2)
  selected_chips["Trust_Net_Buy_Lots"] = (
      selected_chips["Trust_Net_Buy"] / 1000
  ).round(2)
  chips_output = selected_chips[["Foreign_Net_Buy_Lots", "Trust_Net_Buy_Lots"]]

  # 3. 基本面：財報優質標的
  print("正在下載最新財報資料...")
  roe = data.get("fundamental_features:ROE稅後").iloc[-1]
  operating_margin = data.get("fundamental_features:營業利益率").iloc[-1]
  eps = data.get("financial_statement:每股盈餘").iloc[-1]

  df_fund = pd.DataFrame(
      {"ROE": roe, "Operating_Margin": operating_margin, "EPS": eps}
  )
  fund_condition = (
      (df_fund["ROE"] > 10)
      & (df_fund["Operating_Margin"] > 0)
      & (df_fund["EPS"] > 0)
  )
  selected_fund = df_fund[fund_condition].copy()

  # 4. 雙效合擊（交集）
  print("正在進行雙效合擊交叉比對...")
  super_stocks = pd.merge(
      chips_output, selected_fund, left_index=True, right_index=True, how="inner"
  )
  super_stocks = super_stocks.sort_values(by="ROE", ascending=False)

  # 5. 輸出篩選結果 CSV
  chips_output.to_csv("institutional_buy_90d.csv", encoding="utf-8-sig")
  selected_fund.to_csv("fundamental_stocks.csv", encoding="utf-8-sig")
  super_stocks.to_csv("dashboard_super_stocks.csv", encoding="utf-8-sig")

  # 6. 整合 VIX 與恐慌度情緒指標
  print("正在更新美股/台股 VIX 與恐慌度情緒指標...")
  vix_report = build_vix_sentiment_report(finlab_data_module=data)
  vix_sent = vix_report.get("vix_sentiment", {})
  print(
      f"VIX 情緒分數：{vix_sent.get('sentiment_score')} | "
      f"恐慌度：{vix_sent.get('fear_index_0_100')} | "
      f"狀態：{vix_sent.get('mood_label')}"
  )

  print("=== 篩選與 VIX 情緒指標已成功輸出 ===")


if __name__ == "__main__":
  run_screening()
