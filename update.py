import json
import datetime
import requests

def fetch_taifex_data():
    """
    此處寫入抓取台灣期貨交易所 (TAIFEX) 與證交所 (TWSE) 盤後 Open Data API 的邏輯
    """
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # 模擬自動計算與爬取到的最新數據
    latest_data = {
        "date": today_str,
        "updateTime": now_str,
        "sentiment": 42.4,
        "sentimentStatus": "震盪整理期(中性)",
        "foreignFutures": "-76,595",
        "foreignChange": "+1,895",
        "foreignNote": "期貨端偏空避險",
        "retailSmall": 22.2,
        "retailMicro": 25.9,
        "pcRatio": 101.9,
        "optionCall": "417",
        "optionPut": "2,735",
        "analysis": "現貨偏多、但外資期貨偏空＋散戶過度偏多＝「現貨熱、期貨示警」的背離格局。",
        "historyDates": ["7/15", "7/16", "7/17", "7/18", "7/19", "7/22"],
        "historyValues": [15.8, 18.2, 20.1, 19.5, 21.0, 22.2]
    }

    # 將數據寫入 HTML 讀取的 json 檔案
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(latest_data, f, ensure_ascii=False, indent=4)
        
    print(f"[{now_str}] 數據更新成功！")

if __name__ == "__main__":
    fetch_taifex_data()
