# coding: utf-8
import os
import json
import uuid
import tempfile
import time as pytime
import pandas as pd
import streamlit as st
from datetime import datetime, time, timedelta, timezone
from filelock import FileLock, Timeout
from streamlit_autorefresh import st_autorefresh

# タイムゾーンの設定（常に日本時間にする）
JST = timezone(timedelta(hours=+9), 'JST')

DATA_DIR = "data"
INVENTORY_FILE = os.path.join(DATA_DIR, "inventory.csv")
HISTORY_FILE = os.path.join(DATA_DIR, "history.csv")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
START_INVENTORY_FILE = os.path.join(DATA_DIR, "start_inventory.csv")
TERMINAL_INVENTORY_FILE = os.path.join(DATA_DIR, "terminal_inventory_wide.csv")

# 各ファイルの排他制御用ロックファイル
LOCK_FILE = os.path.join(DATA_DIR, "app.lock")

try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception as e:
    st.error(f"⚠️ データディレクトリの作成に失敗しました: {e}")

DEFAULT_TERMINALS = ["本部", "レジ1", "レジ2"]

default_inventory = pd.DataFrame([
    {'商品名': 'チュロス（チョコ）', '価格': 200, '在庫数': 368},
    {'商品名': 'チュロス（シナモン）', '価格': 200, '在庫数': 180},
    {'商品名': 'シュー（いちご）', '価格': 100, '在庫数': 168},
    {'商品名': 'シュー（バニラ）', '価格': 100, '在庫数': 168},
    {'商品名': 'シュー（抹茶）', '価格': 100, '在庫数': 82},
    {'商品名': 'シュー（チョコ）', '価格': 100, '在庫数': 82}
])

default_history = pd.DataFrame(columns=['履歴ID', '日時', '端末', '商品名', '数量', '合計金額', '整理券番号', '受け渡し済'])

def safe_int(val, default=0):
    try:
        num = pd.to_numeric(val, errors='coerce')
        return int(num) if pd.notnull(num) else default
    except Exception:
        return default

def ticket_to_int(val):
    try:
        if pd.isna(val) or str(val).strip() == "なし":
            return None
        return int(float(val))
    except (ValueError, TypeError):
        return None

def initialize_default_files():
    """初回起動時などに必要な全データファイルを安全に生成する"""
    try:
        # 1. inventory.csv
        if not os.path.exists(INVENTORY_FILE):
            default_inventory.to_csv(INVENTORY_FILE, index=False, encoding="utf-8-sig")

        # 2. history.csv
        if not os.path.exists(HISTORY_FILE):
            default_history.to_csv(HISTORY_FILE, index=False, encoding="utf-8-sig")

        # 3. start_inventory.csv
        if not os.path.exists(START_INVENTORY_FILE):
            default_inventory.to_csv(START_INVENTORY_FILE, index=False, encoding="utf-8-sig")

        # 4. terminal_inventory_wide.csv
        if not os.path.exists(TERMINAL_INVENTORY_FILE):
            default_term_data = []
            for _, row in default_inventory.iterrows():
                default_term_data.append({
                    '商品名': row['商品名'],
                    '本部': row['在庫数'],
                    'レジ1': 0,
                    'レジ2': 0
                })
            pd.DataFrame(default_term_data).to_csv(TERMINAL_INVENTORY_FILE, index=False, encoding="utf-8-sig")

        # 5. settings.json
        if not os.path.exists(SETTINGS_FILE):
            settings = {
                "ticket_counter": 1,
                "master_prices": {row['商品名']: int(row['価格']) for _, row in default_inventory.iterrows()},
                "start_inventory_set": False
            }
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        st.error(f"❌ 初期データの生成に失敗しました: {e}")
        return False

def auto_repair_csv(file_path, required_cols, default_df, dtype=None, strict_mode=True):
    bak_path = file_path + ".bak"
    df = None

    if os.path.exists(file_path):
        try:
            df = pd.read_csv(file_path, encoding="utf-8-sig", dtype=dtype)
        except Exception:
            df = None

    if df is None and os.path.exists(bak_path):
        try:
            df = pd.read_csv(bak_path, encoding="utf-8-sig", dtype=dtype)
            if not strict_mode:
                st.warning(f"⚠️ {os.path.basename(file_path)} が破損していたため、バックアップから自動復元しました。")
        except Exception:
            df = None

    if df is None:
        if strict_mode:
            st.error(f"🚨 データ読み込みエラー\n\n安全のためシステム（会計・編集）を停止しました。\nデータを上書きから保護するため、本部担当者に確認して再試行してください。\n（ファイル読込失敗: {os.path.basename(file_path)}）")
            raise RuntimeError(f"Strict mode: Failed to load {file_path}")
        else:
            st.warning(f"⚠️ {os.path.basename(file_path)} を読み込めなかったため、標準データで再生成しました。")
            df = default_df.copy()

    for col, default_val in required_cols.items():
        if col not in df.columns:
            if strict_mode:
                st.error(f"🚨 データ整合性エラー\n\n安全のためシステムを停止しました。\n必須カラムが欠損しています: {col} in {os.path.basename(file_path)}")
                raise RuntimeError(f"Strict mode: Missing column {col}")
            else:
                df[col] = default_val
        else:
            if isinstance(default_val, int):
                df[col] = df[col].apply(lambda x: safe_int(x, default_val))

    return df

def validate_startup_integrity(inv, hist, term_inv):
    """起動時および読み込み時の厳格なデータ整合性チェック"""
    if not inv.empty:
        if (inv['在庫数'] < 0).any():
            st.error("🚨 システム停止: 在庫数がマイナスの商品があります。本部担当者に確認してください。")
            raise RuntimeError("Integrity Error: Negative stock detected")
        if (inv['価格'] < 0).any():
            st.error("🚨 システム停止: 価格がマイナスの商品があります。本部担当者に確認してください。")
            raise RuntimeError("Integrity Error: Negative price detected")
        if inv['商品名'].duplicated().any():
            st.error("🚨 システム停止: 商品名が重複しています。本部担当者に確認してください。")
            raise RuntimeError("Integrity Error: Duplicate product names")

    if not hist.empty:
        if ('数量' in hist.columns) and (hist['数量'] <= 0).any():
            st.error("🚨 システム停止: 販売履歴に数量が0以下のデータがあります。本部担当者に確認してください。")
            raise RuntimeError("Integrity Error: Invalid history quantity")
        if ('履歴ID' in hist.columns) and hist['履歴ID'].duplicated().any():
            st.error("🚨 システム停止: 履歴IDが重複しています。本部担当者に確認してください。")
            raise RuntimeError("Integrity Error: Duplicate history IDs")

    if not inv.empty and not term_inv.empty:
        for _, row in inv.iterrows():
            p_name = row['商品名']
            total_stk = safe_int(row['在庫数'])
            t_match = term_inv[term_inv['商品名'] == p_name]
            if not t_match.empty:
                r1 = safe_int(t_match.iloc[0].get('レジ1', 0))
                r2 = safe_int(t_match.iloc[0].get('レジ2', 0))
                honbu = safe_int(t_match.iloc[0].get('本部', 0))
                if total_stk != (honbu + r1 + r2):
                    st.error(f"🚨 システム停止: 「{p_name}」の全体在庫と端末割り当ての合計が一致しません。本部担当者に確認してください。")
                    raise RuntimeError("Integrity Error: Stock sum mismatch")

def _load_data_core(strict_mode=True):
    inv = auto_repair_csv(
        INVENTORY_FILE, 
        {'商品名': '不明な商品', '価格': 0, '在庫数': 0}, 
        default_inventory,
        strict_mode=strict_mode
    )

    hist = auto_repair_csv(
        HISTORY_FILE, 
        {
            '履歴ID': '', 
            '日時': datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S'), 
            '端末': '本部', 
            '商品名': '不明', 
            '数量': 0, 
            '合計金額': 0, 
            '整理券番号': 'なし', 
            '受け渡し済': False
        }, 
        default_history,
        dtype={'整理券番号': str},
        strict_mode=strict_mode
    )
    
    # --- 履歴IDの厳格な検証と読み込み（修正版） ---
    if '履歴ID' not in hist.columns:
        if strict_mode:
            st.error("🚨 データ整合性エラー\n\n安全のためシステムを停止しました。\n履歴IDカラムがありません。")
            raise RuntimeError("Strict mode: Missing history ID column")
        else:
            hist['履歴ID'] = [str(uuid.uuid4()) for _ in range(len(hist))]

    ids = hist['履歴ID'].fillna("").astype(str).str.strip()

    if (ids == "").any():
        if strict_mode:
            st.error("🚨 データ整合性エラー\n\n安全のためシステムを停止しました。\n履歴IDが欠損（空欄）しているデータがあります。")
            raise RuntimeError("Strict mode: Missing history ID")
        else:
            ids = ids.apply(lambda x: str(uuid.uuid4()) if x == "" else x)

    if ids.duplicated().any():
        if strict_mode:
            st.error("🚨 データ整合性エラー\n\n安全のためシステムを停止しました。\n履歴IDが重複しています。")
            raise RuntimeError("Strict mode: Duplicate history IDs")
        else:
            used_ids = set()
            new_ids = []
            for val in ids:
                current_id = val
                if current_id in used_ids:
                    current_id = str(uuid.uuid4())
                    while current_id in used_ids:
                        current_id = str(uuid.uuid4())
                used_ids.add(current_id)
                new_ids.append(current_id)
            ids = pd.Series(new_ids)

    hist['履歴ID'] = ids

    if '受け渡し済' in hist.columns:
        def parse_bool(val):
            if isinstance(val, bool): return val
            if pd.isna(val): return False
            return str(val).strip().lower() in ['true', '1', 'yes', 't', 'y']
        hist['受け渡し済'] = hist['受け渡し済'].apply(parse_bool)

    settings_bak = SETTINGS_FILE + ".bak"
    settings = None

    for target_s_path in [SETTINGS_FILE, settings_bak]:
        if os.path.exists(target_s_path):
            try:
                with open(target_s_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                break
            except Exception:
                settings = None

    if settings is None and strict_mode:
        st.error(f"🚨 データ整合性エラー\n\n安全のためシステムを停止しました。\nsettings.json の読み込みに失敗しました。")
        raise RuntimeError("Strict mode: Failed to load settings.json")

    master_prices = {}
    ticket_counter = 1
    start_inventory_set = False

    if settings is not None:
        try:
            master_prices = {str(k): int(v) for k, v in settings.get("master_prices", {}).items()}
            ticket_counter = int(settings.get("ticket_counter", 1))
            start_inventory_set = settings.get("start_inventory_set", False)
        except Exception:
            if strict_mode:
                st.error(f"🚨 データ整合性エラー\n\n安全のためシステムを停止しました。\nsettings.json のフォーマットが不正です。")
                raise RuntimeError("Strict mode: Invalid settings format")
            settings = None

    if settings is None:
        max_ticket = 0
        if not hist.empty and '整理券番号' in hist.columns:
            for t_val in hist['整理券番号']:
                t_int = ticket_to_int(t_val)
                if t_int is not None and t_int > max_ticket:
                    max_ticket = t_int
        
        ticket_counter = max_ticket + 1 if max_ticket > 0 else 1
        master_prices = {}
        start_inventory_set = False

    if not master_prices:
        master_prices = {row['商品名']: safe_int(row['価格']) for _, row in inv.iterrows() if pd.notnull(row.get('商品名'))}

    start_inv = auto_repair_csv(
        START_INVENTORY_FILE, 
        {'商品名': '不明な商品', '価格': 0, '在庫数': 0}, 
        inv,
        strict_mode=strict_mode
    )

    default_term_data = []
    for _, row in inv.iterrows():
        default_term_data.append({
            '商品名': row['商品名'],
            '本部': row['在庫数'],
            'レジ1': 0,
            'レジ2': 0
        })
    default_term_df = pd.DataFrame(default_term_data)

    term_req_cols = {'商品名': '不明な商品'}
    for t in DEFAULT_TERMINALS:
        term_req_cols[t] = 0

    term_inv = auto_repair_csv(
        TERMINAL_INVENTORY_FILE, 
        term_req_cols, 
        default_term_df,
        strict_mode=strict_mode
    )

    max_hist_ticket = 0
    if not hist.empty and '整理券番号' in hist.columns:
        for t_val in hist['整理券番号']:
            t_int = ticket_to_int(t_val)
            if t_int is not None and t_int > max_hist_ticket:
                max_hist_ticket = t_int
    if max_hist_ticket >= ticket_counter:
        ticket_counter = max_hist_ticket + 1

    for _, row in inv.iterrows():
        p_name = row['商品名']
        total_stk = safe_int(row['在庫数'])
        t_match = term_inv[term_inv['商品名'] == p_name]
        if not t_match.empty:
            r1 = safe_int(t_match.iloc[0].get('レジ1', 0))
            r2 = safe_int(t_match.iloc[0].get('レジ2', 0))
            honbu = safe_int(t_match.iloc[0].get('本部', 0))
            if r1 + r2 + honbu != total_stk:
                new_honbu = max(0, total_stk - r1 - r2)
                term_inv.loc[term_inv['商品名'] == p_name, '本部'] = new_honbu

    # 厳格な整合性検証の実行
    validate_startup_integrity(inv, hist, term_inv)

    return inv, hist, master_prices, start_inv, term_inv, ticket_counter, start_inventory_set

def load_data(use_lock=True, strict_mode=True):
    if use_lock:
        try:
            with FileLock(LOCK_FILE, timeout=5):
                return _load_data_core(strict_mode=strict_mode)
        except Timeout:
            st.error("❌ データベースが混雑しています（ロック取得タイムアウト）。時間を置いて再度お試しください。")
            raise RuntimeError("Lock acquisition timeout during load")
        except Exception as e:
            if strict_mode:
                raise e
            st.error(f"❌ ファイルシステムまたはロック処理で予期せぬエラーが発生しました: {e}")
            return default_inventory.copy(), default_history.copy(), {}, default_inventory.copy(), pd.DataFrame(), 1, False
    else:
        try:
            return _load_data_core(strict_mode=strict_mode)
        except Exception as e:
            if strict_mode:
                raise e
            st.error(f"❌ データ読み込み中にエラーが発生しました: {e}")
            return default_inventory.copy(), default_history.copy(), {}, default_inventory.copy(), pd.DataFrame(), 1, False

def save_data(inv, hist, master_prices, ticket_counter, start_inventory_set, start_inv, term_inv, use_lock=True):
    def _save_core():
        temp_dir = DATA_DIR
        temp_files = {}
        try:
            hist_columns = ['履歴ID', '日時', '端末', '商品名', '数量', '合計金額', '整理券番号', '受け渡し済']
            hist_to_save = hist.copy()
            for col in hist_columns:
                if col not in hist_to_save.columns:
                    hist_to_save[col] = ""
            hist_to_save = hist_to_save[hist_columns]

            settings = {
                "ticket_counter": ticket_counter,
                "master_prices": master_prices,
                "start_inventory_set": start_inventory_set
            }

            targets = {
                INVENTORY_FILE: inv,
                HISTORY_FILE: hist_to_save,
                START_INVENTORY_FILE: start_inv,
                TERMINAL_INVENTORY_FILE: term_inv
            }

            for target_path, df in targets.items():
                with tempfile.NamedTemporaryFile('w', dir=temp_dir, delete=False, encoding="utf-8-sig") as tf:
                    df.to_csv(tf.name, index=False, encoding="utf-8-sig")
                    temp_files[target_path] = tf.name

            with tempfile.NamedTemporaryFile('w', dir=temp_dir, delete=False, encoding="utf-8") as tf:
                json.dump(settings, tf, ensure_ascii=False, indent=2)
                temp_settings_path = tf.name
            temp_files[SETTINGS_FILE] = temp_settings_path

            for target_path, temp_path in targets.items():
                if os.path.exists(target_path):
                    try:
                        os.replace(target_path, target_path + ".bak")
                    except Exception:
                        pass
                os.replace(temp_path, target_path)

            if os.path.exists(SETTINGS_FILE):
                try:
                    os.replace(SETTINGS_FILE, SETTINGS_FILE + ".bak")
                except Exception:
                    pass
            os.replace(temp_settings_path, SETTINGS_FILE)

        except Exception as e:
            for t_path in temp_files.values():
                if os.path.exists(t_path):
                    try:
                        os.remove(t_path)
                    except Exception:
                        pass
            raise e

    if use_lock:
        try:
            with FileLock(LOCK_FILE, timeout=5):
                _save_core()
        except Timeout:
            st.error("❌ 保存処理のロック取得に失敗しました。データ競合を防ぐため保存をキャンセルしました。")
            raise RuntimeError("Lock acquisition timeout during save")
        except Exception as e:
            st.error(f"❌ トランザクション保存中にエラーが発生しました: {e}")
            raise e
    else:
        _save_core()

def process_checkout(current_terminal, issue_ticket=False):
    now_str = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
    
    try:
        with FileLock(LOCK_FILE, timeout=5):
            inv_latest, hist_latest, mp_latest, start_inv_latest, term_inv_latest, tc_latest, sis_latest = _load_data_core(strict_mode=True)
            
            for name, qty in st.session_state.temp_cart.items():
                if qty > 0:
                    t_match = term_inv_latest[term_inv_latest['商品名'] == name]
                    curr_term_stock = safe_int(t_match.iloc[0][current_terminal]) if not t_match.empty and current_terminal in t_match.columns else 0
                    if qty > curr_term_stock:
                        st.error(f"⚠️ 「{name}」の{current_terminal}の在庫が不足しました（最新在庫: {curr_term_stock}個 / 注文: {qty}個）。")
                        return False

            ticket_num = str(tc_latest) if issue_ticket else "なし"
            if issue_ticket:
                tc_latest += 1

            for name, qty in st.session_state.temp_cart.items():
                if qty > 0:
                    match = inv_latest['商品名'] == name
                    idx = inv_latest.index[match][0]
                    price = safe_int(inv_latest.at[idx, '価格'])
                    total_stk = safe_int(inv_latest.at[idx, '在庫数'])
                    inv_latest.at[idx, '在庫数'] = max(0, total_stk - qty)

                    t_idx = term_inv_latest[term_inv_latest['商品名'] == name].index[0]
                    term_stk = safe_int(term_inv_latest.at[t_idx, current_terminal])
                    term_inv_latest.at[t_idx, current_terminal] = max(0, term_stk - qty)

                    new_hist = pd.DataFrame([{
                        '履歴ID': str(uuid.uuid4()),
                        '日時': now_str, 
                        '端末': current_terminal, 
                        '商品名': name, 
                        '数量': qty, 
                        '合計金額': price * qty, 
                        '整理券番号': ticket_num, 
                        '受け渡し済': not issue_ticket
                    }])
                    hist_latest = pd.concat([hist_latest, new_hist], ignore_index=True)
            
            save_data(inv_latest, hist_latest, mp_latest, tc_latest, sis_latest, start_inv_latest, term_inv_latest, use_lock=False)
            
            st.session_state.inventory = inv_latest
            st.session_state.history = hist_latest
            st.session_state.terminal_inventory = term_inv_latest
            if issue_ticket:
                st.session_state.ticket_counter = tc_latest

            return ticket_num if issue_ticket else True

    except Timeout:
        st.error("❌ 他端末が処理中のため会計できませんでした。もう一度お試しください。")
        return False
    except RuntimeError:
        return False
    except Exception as e:
        st.error(f"❌ 会計処理中に予期せぬエラーが発生しました: {e}")
        return False

# --- 初回起動チェック＆初期セットアップUI ---
required_files_exist = all(os.path.exists(f) for f in [INVENTORY_FILE, HISTORY_FILE, SETTINGS_FILE, START_INVENTORY_FILE, TERMINAL_INVENTORY_FILE])

if not required_files_exist:
    st.title("簡易レジ＆在庫管理アプリ - 初期セットアップ")
    st.warning("⚠️ 初期データが存在しません。初回セットアップを実行してください。")
    if st.button("📦 初期データを作成"):
        if initialize_default_files():
            st.success("初期データの作成に成功しました！アプリを再読み込みしています...")
            st.rerun()
        else:
            st.error("初期データの作成に失敗しました。")
    st.stop()

# --- 初期化（原則 strict_mode=True でファイル破損時の上書きを防ぐ） ---
try:
    inv_data, hist_data, mp_data, start_inv_data, term_inv_data, tc_data, sis_data = load_data(strict_mode=True)
    st.session_state.inventory = inv_data
    st.session_state.history = hist_data
    st.session_state.master_prices = mp_data
    st.session_state.start_inventory = start_inv_data
    st.session_state.terminal_inventory = term_inv_data
    st.session_state.ticket_counter = tc_data
    st.session_state.start_inventory_set = sis_data
except Exception as e:
    st.error("🚨 データの読み込みに失敗したか、整合性エラーが検知されました。\nデータ保護のためシステムを停止しています。管理者に連絡してください。")
    st.stop()

if 'temp_cart' not in st.session_state:
    st.session_state.temp_cart = {}

st.title("簡易レジ＆在庫管理アプリ")

# --- サイドバー ---
st.sidebar.header("⚙️ システム・更新設定")
enable_auto_refresh = st.sidebar.checkbox("5秒自動更新を有効にする", value=True)
if enable_auto_refresh:
    st_autorefresh(interval=5000, limit=None, key="realtime_sync_refresh")

st.sidebar.header("🖥️ 操作端末の選択")
current_terminal = st.sidebar.selectbox("現在の端末", DEFAULT_TERMINALS, key="current_terminal")

st.sidebar.header("🔐 モード切替")
passcode = st.sidebar.text_input("管理者パスワード（編集用）", type="password")
ADMIN_PASSWORD = "1234"

is_admin = (passcode == ADMIN_PASSWORD)
if is_admin:
    st.sidebar.success("🟢 編集モード（PC操作中）")
else:
    st.sidebar.warning("🔒 閲覧専用モード")

st.sidebar.header("🕒 営業日時の設定")
today = datetime.now(JST).date()
start_date_input = st.sidebar.date_input("開始日", value=today, key="s_date")
start_time_input = st.sidebar.time_input("開始時間", value=time(9, 30), key="s_time")
s_dt = datetime.combine(start_date_input, start_time_input)

end_date_input = st.sidebar.date_input("終了日", value=today, key="e_date")
end_time_input = st.sidebar.time_input("終了時間", value=time(14, 0), key="e_time")
e_dt = datetime.combine(end_date_input, end_time_input)

if e_dt <= s_dt:
    e_dt += timedelta(days=1)

def is_peak_time(dt_slot):
    current_minutes = dt_slot.hour * 60 + dt_slot.minute
    return (11 * 60) <= current_minutes < (13 * 60)

now = datetime.now(JST).replace(tzinfo=None)

elapsed_sales = {}
if 'history' in st.session_state and not st.session_state.history.empty:
    hist_df = st.session_state.history.copy()
    hist_df['dt'] = pd.to_datetime(hist_df['日時'], errors='coerce')
    hist_df = hist_df.dropna(subset=['dt'])
    target_end = min(e_dt, now)
    target_hist = hist_df[(hist_df['dt'] >= s_dt) & (hist_df['dt'] <= target_end)]
    if not target_hist.empty:
        elapsed_sales = target_hist.groupby('商品名')['数量'].sum().to_dict()

total_minutes = max(1, int((e_dt - s_dt).total_seconds() / 60))

elapsed_weight = 0.0
total_weight = 0.0

curr = s_dt
while curr < e_dt:
    next_minute = curr + timedelta(minutes=1)
    if next_minute > e_dt:
        next_minute = e_dt
    minute_length = (next_minute - curr).total_seconds() / 60
    middle = curr + (next_minute - curr) / 2
    weight = 1.5 if is_peak_time(middle) else 1.0
    total_weight += weight * minute_length
    if curr < now:
        actual_end = min(next_minute, now)
        actual_minutes = (actual_end - curr).total_seconds() / 60
        if actual_minutes > 0:
            elapsed_weight += weight * actual_minutes
    curr = next_minute

total_duration = (e_dt - s_dt).total_seconds()
remaining_duration = (e_dt - now).total_seconds()
time_progress = max(0.0, min(1.0, remaining_duration / total_duration)) if total_duration > 0 else 0.5

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["レジ（会計）", "在庫管理", "販売履歴", "整理券確認", "販売予測", "価格提案"])

# --- Tab 1: レジ ---
with tab1:
    st.header(f"高速お会計 (操作端末: {current_terminal})")
    if not is_admin:
        st.info("💡 閲覧モード中のため、レジ操作は無効化されています。")
    
    inv = st.session_state.get('inventory', default_inventory).dropna(subset=['商品名'])
    term_inv = st.session_state.get('terminal_inventory', pd.DataFrame())
    current_product_names = [str(name) for name in inv['商品名']]
    
    st.session_state.temp_cart = {name: st.session_state.temp_cart.get(name, 0) for name in current_product_names}

    for index, row in inv.iterrows():
        p_name = str(row['商品名'])
        p_price = safe_int(row['価格'])
        p_total_stock = safe_int(row['在庫数'])
        
        t_match = term_inv[term_inv['商品名'] == p_name] if not term_inv.empty else pd.DataFrame()
        p_term_stock = safe_int(t_match.iloc[0][current_terminal]) if not t_match.empty and current_terminal in t_match.columns else 0

        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        if p_term_stock <= 0:
            c1.write(f"**{p_name}** (¥{p_price} / 🔴 {current_terminal}在庫切れ)")
        else:
            c1.write(f"**{p_name}** (¥{p_price} / {current_terminal}在庫:{p_term_stock} | 全体:{p_total_stock})")

        if c2.button("－", key=f"sub_{p_name}", disabled=not is_admin):
            if st.session_state.temp_cart.get(p_name, 0) > 0:
                st.session_state.temp_cart[p_name] -= 1
                st.rerun()

        c3.write(f"### {st.session_state.temp_cart.get(p_name, 0)}")

        if c4.button("＋", key=f"add_{p_name}", disabled=(p_term_stock <= 0 or not is_admin)):
            if st.session_state.temp_cart.get(p_name, 0) < p_term_stock:
                st.session_state.temp_cart[p_name] += 1
                st.rerun()

    st.divider()
    total_price = 0
    for name, qty in st.session_state.temp_cart.items():
        if qty > 0 and name in inv['商品名'].values:
            match_row = inv[inv['商品名'] == name]
            if not match_row.empty:
                price = safe_int(match_row['価格'].iloc[0])
                total_price += price * qty

    st.info(f"合計金額: **¥{total_price}**")

    if current_terminal in ["レジ1", "レジ2"]:
        col_btn1, col_btn3 = st.columns(2)
        with col_btn1:
            if st.button("整理券なしで会計", disabled=not is_admin, use_container_width=True):
                if not any(q > 0 for q in st.session_state.temp_cart.values()):
                    st.error("商品が選択されていません。")
                else:
                    if process_checkout(current_terminal, issue_ticket=False):
                        st.session_state.temp_cart = {name: 0 for name in current_product_names}
                        st.success("会計完了！")
                        st.rerun()

        with col_btn3:
            if st.button("かごを空にする", disabled=not is_admin, use_container_width=True):
                st.session_state.temp_cart = {name: 0 for name in current_product_names}
                st.rerun()
    else:
        col_btn1, col_btn2, col_btn3 = st.columns(3)

        with col_btn1:
            if st.button("整理券なしで会計", disabled=not is_admin):
                if not any(q > 0 for q in st.session_state.temp_cart.values()):
                    st.error("商品が選択されていません。")
                else:
                    if process_checkout(current_terminal, issue_ticket=False):
                        st.session_state.temp_cart = {name: 0 for name in current_product_names}
                        st.success("会計完了！")
                        st.rerun()

        with col_btn2:
            if st.button("整理券を発行して会計", disabled=not is_admin):
                if not any(q > 0 for q in st.session_state.temp_cart.values()):
                    st.error("商品が選択されていません。")
                else:
                    ticket_res = process_checkout(current_terminal, issue_ticket=True)
                    if ticket_res:
                        st.session_state.temp_cart = {name: 0 for name in current_product_names}
                        st.success(f"会計完了！整理券番号: **{ticket_res}**")
                        st.rerun()

        with col_btn3:
            if st.button("かごを空にする", disabled=not is_admin):
                st.session_state.temp_cart = {name: 0 for name in current_product_names}
                st.rerun()

# --- Tab 2: 在庫管理 ---
with tab2:
    st.header("在庫管理（全体 ＆ 各端末の割り当て）")
    st.info("💡 「本部」は編集不可にして自動計算するのが一番安全です。")

    inv_df = st.session_state.get('inventory', default_inventory).copy()
    term_df = st.session_state.get('terminal_inventory', pd.DataFrame()).copy()

    merged_df = pd.merge(inv_df, term_df, on="商品名", how="left")
    for t in DEFAULT_TERMINALS:
        if t not in merged_df.columns:
            merged_df[t] = 0
        else:
            merged_df[t] = merged_df[t].apply(lambda x: safe_int(x, 0))

    if is_admin:
        edited_df = st.data_editor(
            merged_df,
            use_container_width=True,
            num_rows="dynamic",
            key="unified_inventory_editor",
            column_config={
                "商品名": st.column_config.TextColumn("商品名", disabled=True),
                "価格": st.column_config.NumberColumn("価格", min_value=0, step=10),
                "在庫数": st.column_config.NumberColumn("全体在庫", min_value=0, step=1),
                "本部": st.column_config.NumberColumn("本部", min_value=0, step=1, disabled=True),
                "レジ1": st.column_config.NumberColumn("レジ1", min_value=0, step=1),
                "レジ2": st.column_config.NumberColumn("レジ2", min_value=0, step=1),
            }
        )

        if not edited_df.equals(merged_df):
            try:
                with FileLock(LOCK_FILE, timeout=5):
                    inv_l, hist_l, mp_l, start_inv_l, term_inv_l, tc_l, sis_l = _load_data_core(strict_mode=True)
                    has_error = False

                    for idx, row in edited_df.iterrows():
                        name = row['商品名']
                        if pd.isna(name) or str(name).strip() == "":
                            continue
                        
                        if idx < len(merged_df):
                            orig_row = merged_df.iloc[idx]
                            if row.equals(orig_row):
                                continue
                        else:
                            orig_row = {'価格': 0, '在庫数': 0, 'レジ1': 0, 'レジ2': 0}

                        delta_price = safe_int(row['価格']) - safe_int(orig_row['価格'])
                        delta_total = safe_int(row['在庫数']) - safe_int(orig_row['在庫数'])
                        delta_r1 = safe_int(row['レジ1']) - safe_int(orig_row['レジ1'])
                        delta_r2 = safe_int(row['レジ2']) - safe_int(orig_row['レジ2'])

                        inv_idx = inv_l.index[inv_l['商品名'] == name]
                        if not inv_idx.empty:
                            i = inv_idx[0]
                            inv_l.at[i, '価格'] = max(0, safe_int(inv_l.at[i, '価格']) + delta_price)
                            inv_l.at[i, '在庫数'] = max(0, safe_int(inv_l.at[i, '在庫数']) + delta_total)
                        else:
                            new_inv = pd.DataFrame([{'商品名': name, '価格': safe_int(row['価格']), '在庫数': safe_int(row['在庫数'])}])
                            inv_l = pd.concat([inv_l, new_inv], ignore_index=True)
                            i = len(inv_l) - 1

                        term_idx = term_inv_l.index[term_inv_l['商品名'] == name]
                        if not term_idx.empty:
                            ti = term_idx[0]
                            new_r1 = max(0, safe_int(term_inv_l.at[ti, 'レジ1']) + delta_r1)
                            new_r2 = max(0, safe_int(term_inv_l.at[ti, 'レジ2']) + delta_r2)
                            
                            term_inv_l.at[ti, 'レジ1'] = new_r1
                            term_inv_l.at[ti, 'レジ2'] = new_r2
                        else:
                            new_term = pd.DataFrame([{'商品名': name, '本部': 0, 'レジ1': safe_int(row['レジ1']), 'レジ2': safe_int(row['レジ2'])}])
                            term_inv_l = pd.concat([term_inv_l, new_term], ignore_index=True)
                            ti = len(term_inv_l) - 1
                            new_r1 = safe_int(row['レジ1'])
                            new_r2 = safe_int(row['レジ2'])

                        latest_total = safe_int(inv_l.at[i, '在庫数'])
                        honbu = latest_total - new_r1 - new_r2

                        if honbu < 0:
                            st.error(f"⚠️ 「{name}」の割り当て数が最新の全体在庫({latest_total})を超過します。")
                            has_error = True
                            break
                        
                        term_inv_l.at[ti, '本部'] = honbu

                    if not has_error:
                        save_data(inv_l, hist_l, mp_l, tc_l, sis_l, start_inv_l, term_inv_l, use_lock=False)
                        st.session_state.inventory = inv_l
                        st.session_state.terminal_inventory = term_inv_l
                        st.success("在庫と端末割り当てを更新しました。")
                        st.rerun()
            except Timeout:
                st.error("❌ 他端末が処理中のため更新できませんでした。")
            except Exception as e:
                st.error(f"❌ 更新処理中に予期せぬエラーが発生しました: {e}")
    else:
        st.dataframe(merged_df, use_container_width=True)

# --- Tab 3: 販売履歴 ---
with tab3:
    st.header("販売履歴")
    history_df = st.session_state.get('history', pd.DataFrame())
    if not history_df.empty:
        st.metric("総売上金額", f"¥{history_df['合計金額'].sum()}")
        for i, row in history_df.iloc[::-1].iterrows():
            c1, c2 = st.columns([4, 1])
            t_label = f"券#{row['整理券番号']}" if row['整理券番号'] != "なし" else "整理券なし"
            term_label = row.get('端末', '本部')
            status_label = " [受け渡し済]" if row.get('受け渡し済', False) else ""
            h_id = row.get('履歴ID', '')
            
            c1.write(f"{t_label}{status_label} | {row['日時']} | 端末: {term_label} | {row['商品名']} | {row['数量']}個 | ¥{row['合計金額']}")
            if is_admin:
                if c2.button("削除", key=f"del_{h_id}"):
                    try:
                        with FileLock(LOCK_FILE, timeout=5):
                            inv_l, hist_l, mp_l, start_inv_l, term_inv_l, tc_l, sis_l = _load_data_core(strict_mode=True)
                            match_hist = hist_l[hist_l['履歴ID'] == h_id]
                            if not match_hist.empty:
                                target_idx = match_hist.index[0]
                                p_name = hist_l.at[target_idx, '商品名']
                                p_qty = safe_int(hist_l.at[target_idx, '数量'])
                                p_term = hist_l.at[target_idx, '端末'] if '端末' in hist_l.columns else "本部"

                                match = inv_l['商品名'] == p_name
                                if match.any():
                                    idx = inv_l.index[match][0]
                                    inv_l.at[idx, '在庫数'] = safe_int(inv_l.at[idx, '在庫数']) + p_qty
                                
                                t_match = term_inv_l['商品名'] == p_name
                                if t_match.any():
                                    t_idx = term_inv_l.index[t_match][0]
                                    if p_term in term_inv_l.columns:
                                        term_inv_l.at[t_idx, p_term] = safe_int(term_inv_l.at[t_idx, p_term]) + p_qty
                                    else:
                                        term_inv_l.at[t_idx, '本部'] = safe_int(term_inv_l.at[t_idx, '本部']) + p_qty

                                hist_l = hist_l.drop(target_idx).reset_index(drop=True)
                                
                                save_data(inv_l, hist_l, mp_l, tc_l, sis_l, start_inv_l, term_inv_l, use_lock=False)
                                st.session_state.inventory = inv_l
                                st.session_state.history = hist_l
                                st.session_state.terminal_inventory = term_inv_l
                                st.rerun()
                    except Timeout:
                        st.error("❌ 混雑のため削除処理を中断しました。")
                    except Exception as e:
                        st.error(f"❌ 削除処理中にエラーが発生しました: {e}")
    else:
        st.write("履歴はありません。")

# --- Tab 4: 整理券確認 ---
with tab4:
    st.header("整理券確認・受け渡し管理")
    history_df = st.session_state.get('history', pd.DataFrame())
    if not history_df.empty:
        valid_tickets = []
        for t in history_df['整理券番号'].unique():
            cleaned_t = ticket_to_int(t)
            if cleaned_t is not None:
                valid_tickets.append(cleaned_t)
                    
        if valid_tickets:
            for t_num in sorted(list(set(valid_tickets)), reverse=True):
                ticket_rows = history_df[
                    history_df['整理券番号'].apply(ticket_to_int) == ticket_to_int(t_num)
                ]
                is_all_delivered = all(ticket_rows['受け渡し済'])
                expander_title = f"整理券番号: {t_num}" + (" ✅ 【完了】" if is_all_delivered else " ⏳ 【未】")
                with st.expander(expander_title):
                    new_status = st.checkbox("受け渡しを完了にする", value=is_all_delivered, key=f"check_{t_num}", disabled=not is_admin)
                    if is_admin and (new_status != is_all_delivered):
                        try:
                            with FileLock(LOCK_FILE, timeout=5):
                                inv_l, hist_l, mp_l, start_inv_l, term_inv_l, tc_l, sis_l = _load_data_core(strict_mode=True)
                                target_indices = hist_l[hist_l['整理券番号'].apply(ticket_to_int) == ticket_to_int(t_num)].index
                                hist_l.loc[target_indices, '受け渡し済'] = new_status
                                save_data(inv_l, hist_l, mp_l, tc_l, sis_l, start_inv_l, term_inv_l, use_lock=False)
                                st.session_state.history = hist_l
                                st.rerun()
                        except Timeout:
                            st.error("❌ 混雑のため更新処理を中断しました。")
                        except Exception as e:
                            st.error(f"❌ ステータス更新中にエラーが発生しました: {e}")
                    st.table(ticket_rows[['商品名', '数量', '合計金額']])

# --- Tab 5: 販売予測 ---
with tab5:
    st.header("販売予測 ＆ 予想在庫残数")
    
    try:
        inv_latest, _, _, start_inv_latest, _, _, sis_latest = load_data(strict_mode=True)
        if not sis_latest and is_admin:
            if st.button("現在の在庫数を「営業開始時在庫」として確定する"):
                try:
                    with FileLock(LOCK_FILE, timeout=5):
                        inv_l, hist_l, mp_l, _, term_inv_l, tc_l, _ = _load_data_core(strict_mode=True)
                        save_data(inv_l, hist_l, mp_l, tc_l, True, inv_l, term_inv_l, use_lock=False)
                        st.session_state.start_inventory = inv_l.copy()
                        st.session_state.start_inventory_set = True
                        st.success("営業開始時の在庫を確定しました！")
                        st.rerun()
                except Exception as e:
                    st.error(f"❌ 開始時在庫の確定に失敗しました: {e}")

        res = []
        for _, row in inv_latest.iterrows():
            p_name = row['商品名']
            current_stock = safe_int(row['在庫数'])
            start_val = start_inv_latest[start_inv_latest['商品名'] == p_name]['在庫数'] if not start_inv_latest.empty and '商品名' in start_inv_latest.columns else pd.Series()
            start_stock = safe_int(start_val.values[0]) if not start_val.empty else current_stock
            sold = elapsed_sales.get(p_name, 0)
            manual_loss = max(0, (start_stock - sold) - current_stock)
            
            if elapsed_weight > 0:
                estimated_sales = (sold / elapsed_weight) * total_weight
            else:
                estimated_sales = 0
                
            est_total = int(estimated_sales) + manual_loss
            
            if elapsed_weight > 0:
                expected_remaining = max(0, current_stock - int((sold / elapsed_weight) * (total_weight - elapsed_weight)))
            else:
                expected_remaining = current_stock
                
            res.append({
                '商品名': p_name,
                '開始時在庫': start_stock,
                '期間内販売数': sold,
                '予測総販売数': est_total,
                '終了時予想残り': expected_remaining
            })
        if res: st.table(pd.DataFrame(res))
    except Exception as e:
        st.error(f"❌ 販売予測の計算中にエラーが発生しました: {e}")

# --- Tab 6: 価格提案 ---
with tab6:
    st.header("価格提案")
    try:
        inv_latest, _, mp_latest, start_inv_latest, _, _, _ = load_data(strict_mode=True)
        
        res = []
        for _, row in inv_latest.iterrows():
            p_name = row['商品名']
            price = mp_latest.get(p_name, safe_int(row['価格']))
            current_stock = safe_int(row['在庫数'])
            start_val = start_inv_latest[start_inv_latest['商品名'] == p_name]['在庫数'] if not start_inv_latest.empty and '商品名' in start_inv_latest.columns else pd.Series()
            start_stock = safe_int(start_val.values[0]) if not start_val.empty else current_stock
            sold = elapsed_sales.get(p_name, 0)
            
            if elapsed_weight > 0:
                future_sales_est = int((sold / elapsed_weight) * (total_weight - elapsed_weight))
                estimated_total_sales = (sold / elapsed_weight) * total_weight
            else:
                future_sales_est = 0
                estimated_total_sales = 0
                
            expected_remaining = max(0, current_stock - future_sales_est)
            
            status, strong_price, weak_price = "現状維持", "-", "-"
            if expected_remaining > 0 and sold > 0:
                status = "要値下げ"
                strong_rate = max(0.5, 1.0 - ((1.0 - time_progress) * (expected_remaining / start_stock) * 0.45)) if start_stock > 0 else 0.5
                strong_price = f"¥{int((price * strong_rate) / 10) * 10}"
                
                if estimated_total_sales > 0 and current_stock > 0:
                    weak_rate = max(0.5, min(0.95, 1.0 / (current_stock / estimated_total_sales)))
                    weak_price = f"¥{int((price * weak_rate) / 10) * 10}"
                else:
                    weak_price = f"¥{price}"
                    
            res.append({'商品名': p_name, 'ステータス': status, '強気提案': strong_price, '弱気提案': weak_price})
        if res: st.table(pd.DataFrame(res))
    except Exception as e:
        st.error(f"❌ 価格提案の計算中にエラーが発生しました: {e}")
