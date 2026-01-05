import json
import pandas as pd
import numpy as np
from scipy.optimize import curve_fit
import requests
import os

# ---------------------------------------------------------
# 【設定エリア】
# ---------------------------------------------------------
# データ取得モード設定
# True  : ローカルの 'data.json' を使用 (ダウンロード済みデータ)
# False : APIから新しく取得 (以下のIDとトークンを使用)
USE_LOCAL_DATA = False

# API設定 (USE_LOCAL_DATA = False の場合のみ必要)
API_URL = "https://api.chunirec.net/2.0/records/showall.json"
API_USER_ID = "YOUR_USER_ID_HERE"  # 必要に応じて書き換えてください
API_TOKEN = "YOUR_API_TOKEN_HERE"  # 必要に応じて書き換えてください

# 表示設定
TITLE_MAX_LENGTH = 20

# ---------------------------------------------------------
# 1. OVER POWER 計算ロジック
# ---------------------------------------------------------
def calc_rating_from_score(const, score):
    """スコアと定数から単曲レート値を算出"""
    if score >= 1009000: return const + 2.15
    if score >= 1007500: return const + 2.0 + (score - 1007500) * 0.0001
    if score >= 1005000: return const + 1.5 + (score - 1005000) * 0.0002
    if score >= 1000000: return const + 1.0 + (score - 1000000) * 0.0001
    if score >= 990000:  return const + 0.6 + (score - 990000) * 0.00004
    if score >= 975000:  return const + 0.0 + (score - 975000) * 0.00002
    return 0

def get_lamp_bonus(score, is_fc, is_aj):
    """ランプボーナス算出"""
    if score >= 1010000: return 1.25 # 理論値
    if is_aj: return 1.0
    if is_fc: return 0.5
    return 0.0

def calculate_op_value(row, use_expected=False):
    """行データからOPを計算"""
    target_score = row['expected_score'] if use_expected else row['score']
    const = row['const']
    
    # 期待値計算の場合は、スコアに応じてランプを仮定する
    if use_expected:
        is_aj = target_score >= 1009000
        is_fc = target_score >= 1005000
    else:
        is_aj = row.get('is_alljustice', False)
        is_fc = row.get('is_fullcombo', False)
        
    bonus = get_lamp_bonus(target_score, is_fc, is_aj)
    
    # OP計算
    if target_score >= 1010000: # 理論値
        return (const + 3.0) * 5.0
    elif target_score >= 1007501: # SSS+
        base = (const + 2.0) * 5.0
        score_bonus = (target_score - 1007500) * 0.0015
        return base + bonus + score_bonus
    elif target_score >= 975000: # S ~ SSS
        r_val = calc_rating_from_score(const, target_score)
        return r_val * 5.0 + bonus
    else:
        return 0.0

# ---------------------------------------------------------
# 2. 統計的スコア予測 (Curve Fit)
# ---------------------------------------------------------
def model_func(x, a, b):
    """
    失点モデル: 難易度(x)が上がると、失点(1,010,000からのマイナス)が指数関数的に増える
    """
    dropout = a * np.exp(b * (x - 12.0))
    return np.maximum(0, 1010000 - dropout)

def estimate_expected_score_curve(df):
    """ユーザーデータから実力曲線を生成する"""
    
    # まともにプレイできている曲(S以上)を使って傾向を見る
    fit_df = df[df['score'] >= 975000]
    
    if len(fit_df) < 5:
        print("★ データ不足のため、固定モデルを使用します。")
        return lambda x: 1000000

    x_data = fit_df['const'].values
    y_data = fit_df['score'].values

    # 初期パラメータ推定 [a, b]
    p0 = [1000, 0.5] 
    
    try:
        popt, pcov = curve_fit(model_func, x_data, y_data, p0=p0, maxfev=5000)
        a_opt, b_opt = popt
        print(f"★ 分析完了: 実力トレンド係数 a={a_opt:.1f}, b={b_opt:.3f}")
        return lambda x: model_func(x, a_opt, b_opt)
    except Exception as e:
        print(f"★ カーブフィッティング失敗: {e}")
        return lambda x: 1005000

# ---------------------------------------------------------
# 3. メイン処理
# ---------------------------------------------------------
def analyze_records(json_data):
    # 表示設定
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.max_rows', None)
    pd.set_option('display.width', 1000)
    
    records = json_data.get("records", [])
    df = pd.DataFrame(records)
    
    if df.empty:
        print("データがありません")
        return

    # WORLD'S ENDなどを除外
    df = df[df['const'] > 0]

    print(f"全レコード数: {len(df)} 譜面")

    # 1. まず全譜面の実力トレンドを分析
    predict_func = estimate_expected_score_curve(df)
    
    # 2. 全譜面の期待スコアと、現在OP/期待OPを計算
    df['expected_score'] = df['const'].apply(predict_func)
    df['expected_score'] = df['expected_score'].clip(upper=1010000).astype(int)

    df['current_op'] = df.apply(lambda r: calculate_op_value(r, use_expected=False), axis=1)
    df['expected_op'] = df.apply(lambda r: calculate_op_value(r, use_expected=True), axis=1)

    # 3. 【重要】曲ごとに最もOPが高い譜面のみを残す (CHUNITHMのOP仕様)
    # 現在のOPが高い順にソートしておき、曲名で重複排除すれば、各曲の最高OP譜面が残る
    df_unique = df.sort_values("current_op", ascending=False).drop_duplicates("title")
    
    print(f"OP計算対象: {len(df_unique)} 曲 (曲ごとの最大OP譜面を抽出)")

    # 4. 優先度 (GAP) の計算
    df_unique['op_gap'] = df_unique['expected_op'] - df_unique['current_op']
    df_unique['priority_score'] = df_unique['op_gap'].apply(lambda x: max(0, x))

    # 5. 表示用整形
    result_df = df_unique.sort_values('priority_score', ascending=False).head(30).copy()
    
    result_df['Title'] = result_df['title'].apply(lambda x: str(x)[:TITLE_MAX_LENGTH] + '...' if len(str(x)) > TITLE_MAX_LENGTH else str(x))
    result_df['Diff'] = result_df['diff']
    result_df['Const'] = result_df['const']
    result_df['Score'] = result_df['score'].apply(lambda x: f"{int(x):,}")
    result_df['Expect'] = result_df['expected_score'].apply(lambda x: f"{int(x):,}")
    result_df['Cur.OP'] = result_df['current_op'].apply(lambda x: f"{x:.2f}")
    result_df['Exp.OP'] = result_df['expected_op'].apply(lambda x: f"{x:.2f}")
    result_df['GAP'] = result_df['priority_score'].apply(lambda x: f"+{x:.2f}")

    print("\n" + "="*90)
    print(f" 実力統計に基づく OverPower 攻略推奨リスト (TOP 30)")
    print(" ※ 1曲につき、現在最もOPが高い譜面のみを対象に診断しています")
    print("="*90)
    
    cols = ['Title', 'Diff', 'Const', 'Score', 'Expect', 'Cur.OP', 'Exp.OP', 'GAP']
    print(result_df[cols].to_string(index=False))
    print("="*90)

# ---------------------------------------------------------
# 実行ブロック
# ---------------------------------------------------------
if __name__ == "__main__":
    
    data = None
    
    if USE_LOCAL_DATA:
        # ローカルファイルモード
        if os.path.exists("data.json"):
            print("ローカルの data.json を読み込んでいます...")
            try:
                with open("data.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"ファイル読み込みエラー: {e}")
        else:
            print("エラー: data.json が見つかりません。")
            
    else:
        # API取得モード
        print("APIからデータを取得しています...")
        url = f"{API_URL}?region=jp2&token={API_TOKEN}&user_id={API_USER_ID}"
        try:
            response = requests.get(url)
            response.raise_for_status()
            data = json.loads(response.text)
            print("データ取得成功！")
            
            r = open("data.json", "w", encoding="utf-8")
            json.dump(data, r, ensure_ascii=False, indent=4)
            r.close()
            print("data.json にデータを保存しました。")
            
        except Exception as e:
            print(f"APIエラー: {e}")

    # 分析実行
    if data:
        analyze_records(data)