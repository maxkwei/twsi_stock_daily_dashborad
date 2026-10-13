import os
from finlab import data, finlab
import pandas as pd


def run_screening():
  print('=== 開始執行籌碼與財報篩選任務 ===')

  # 1. 取得 FinLab Token 並登入
  api_token = os.environ.get('FINLAB_TOKEN')
  if not api_token:
    raise ValueError('找不到 FINLAB_TOKEN 環境變數，請確認 GitHub Secrets 設定。')
  finlab.login(api_token)

  # 2. 籌碼面：外資與投信近 90 天買超
  print('正在計算外資與投信 90 天累計買超...')
  foreign_buy = data.get(
      'institutional_investors_trading_summary:外陸資買賣超股數(不含外資自營商)'
  )
  trust_buy = data.get('institutional_investors_trading_summary:投信買賣超股數')

  foreign_90d = foreign_buy.tail(90).sum()
  trust_90d = trust_buy.tail(90).sum()

  df_chips = pd.DataFrame(
      {'Foreign_Net_Buy': foreign_90d, 'Trust_Net_Buy': trust_90d}
  )
  chips_condition = (df_chips['Foreign_Net_Buy'] > 0) & (
      df_chips['Trust_Net_Buy'] > 0
  )
  selected_chips = df_chips[chips_condition].copy()
  selected_chips['Foreign_Net_Buy_Lots'] = (
      selected_chips['Foreign_Net_Buy'] / 1000
  ).round(2)
  selected_chips['Trust_Net_Buy_Lots'] = (
      selected_chips['Trust_Net_Buy'] / 1000
  ).round(2)
  chips_output = selected_chips[['Foreign_Net_Buy_Lots', 'Trust_Net_Buy_Lots']]

  # 3. 基本面：財報優質標的
  print('正在下載最新財報資料...')
  roe = data.get('fundamental_features:ROE稅後').iloc[-1]
  operating_margin = data.get('fundamental_features:營業利益率').iloc[-1]
  eps = data.get('financial_statement:每股盈餘').iloc[-1]

  df_fund = pd.DataFrame(
      {'ROE': roe, 'Operating_Margin': operating_margin, 'EPS': eps}
  )
  fund_condition = (
      (df_fund['ROE'] > 10)
      & (df_fund['Operating_Margin'] > 0)
      & (df_fund['EPS'] > 0)
  )
  selected_fund = df_fund[fund_condition].copy()

  # 4. 雙效合擊（交集）
  print('正在進行雙效合擊交叉比對...')
  super_stocks = pd.merge(
      chips_output, selected_fund, left_index=True, right_index=True, how='inner'
  )
  super_stocks = super_stocks.sort_values(by='ROE', ascending=False)

  # 5. 輸出成 CSV 供 Dashboard 讀取
  chips_output.to_csv('institutional_buy_90d.csv', encoding='utf-8-sig')
  selected_fund.to_csv('fundamental_stocks.csv', encoding='utf-8-sig')
  super_stocks.to_csv('dashboard_super_stocks.csv', encoding='utf-8-sig')

  print('=== 篩選結果已成功輸出為 CSV 檔案 ===')


if __name__ == '__main__':
  run_screening()
