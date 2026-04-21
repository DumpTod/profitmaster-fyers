from flask import Flask, jsonify, request, redirect
from flask_cors import CORS
import requests as req
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, date
import calendar
import pytz
import os
import json
import hashlib
from fyers_apiv3 import fyersModel

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get('FLASK_SECRET', 'atr-scanner-secret-key-2024')

FYERS_APP_ID     = os.environ.get('API_KEY', 'VS55VDHYCW-100')
FYERS_SECRET_KEY = os.environ.get('API_SECRET', '724FOKKSFS')
FYERS_REDIRECT_URL = 'https://trade.fyers.in/api-login/redirect-uri/index.html'

SCANNER_CONFIG = {
    'NIFTY50': {
        'instrument_key': 'NSE:NIFTY50-INDEX',
        'option_key': 'NSE:NIFTY50-INDEX',
        'resample_minutes': 5,
        'fast_period': 5, 'fast_mult': 1.5,
        'slow_period': 25, 'slow_mult': 4.0,
        'lot_size': 75, 'strike_step': 50
    },
    'BANKNIFTY': {
        'instrument_key': 'NSE:NIFTYBANK-INDEX',
        'option_key': 'NSE:NIFTYBANK-INDEX',
        'resample_minutes': 5,
        'fast_period': 5, 'fast_mult': 1.5,
        'slow_period': 20, 'slow_mult': 4.0,
        'lot_size': 30, 'strike_step': 100
    }
}

IST = pytz.timezone('Asia/Kolkata')
TOKEN_FILE = '/tmp/token.json'
REFRESH_FILE = '/tmp/refresh_token.txt'

token_data = {'access_token': None, 'token_time': None, 'refresh_token': None}
scan_cache = {'signals': [], 'last_scan': None}
options_cache = {'signals': [], 'last_fetch': None}


def save_token(access_token, refresh_token=None):
    token_data['access_token'] = access_token
    token_data['token_time'] = datetime.now(IST).isoformat()
    if refresh_token:
        token_data['refresh_token'] = refresh_token
        try:
            with open(REFRESH_FILE, 'w') as f:
                f.write(refresh_token)
            print(f"✓ Refresh token saved")
        except Exception as e:
            print(f"✗ Failed to save refresh token: {e}")
    try:
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f)
        print(f"✓ Access token saved")
    except Exception as e:
        print(f"✗ Failed to save access token: {e}")


def load_token():
    try:
        with open(TOKEN_FILE, 'r') as f:
            data = json.load(f)
            token_data['access_token'] = data.get('access_token')
            token_data['token_time'] = data.get('token_time')
            token_data['refresh_token'] = data.get('refresh_token')
        print(f"✓ Token loaded from file")
    except Exception as e:
        print(f"⚠ No token file found")
    if not token_data['refresh_token']:
        try:
            with open(REFRESH_FILE, 'r') as f:
                token_data['refresh_token'] = f.read().strip()
        except:
            pass


def auto_refresh_access_token():
    refresh_token = token_data.get('refresh_token')
    if not refresh_token:
        return False
    try:
        app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()
        r = req.post(
            'https://api-t1.fyers.in/api/v3/validate-refresh-token',
            json={
                'grant_type': 'refresh_token',
                'appIdHash': app_id_hash,
                'refresh_token': refresh_token,
                'pin': os.environ.get('FYERS_PIN', ''),
            },
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        if r.status_code == 200 and r.json().get('s') == 'ok':
            new_access_token = f"{FYERS_APP_ID}:{r.json()['access_token']}"
            save_token(new_access_token)
            return True
        return False
    except:
        return False


load_token()
if not token_data['access_token'] and token_data['refresh_token']:
    auto_refresh_access_token()


def init_fyers():
    if not token_data['access_token']:
        return None
    try:
        return fyersModel.FyersModel(
            client_id=FYERS_APP_ID,
            token=token_data['access_token'],
            log_path='/tmp'
        )
    except:
        return None


TRADING_HOLIDAYS = {
    date(2024,1,26), date(2024,3,25), date(2024,4,14), date(2024,4,17),
    date(2024,5,1), date(2024,6,17), date(2024,8,15), date(2024,10,2),
    date(2024,10,24), date(2024,11,1), date(2024,11,15), date(2024,12,25),
    date(2025,1,26), date(2025,2,26), date(2025,3,14), date(2025,3,31),
    date(2025,4,10), date(2025,4,14), date(2025,4,18), date(2025,5,1),
    date(2025,8,15), date(2025,10,2), date(2025,10,23), date(2025,12,25),
    date(2026,1,26), date(2026,3,3), date(2026,3,26), date(2026,3,31),
    date(2026,4,3), date(2026,4,14), date(2026,5,1), date(2026,5,28),
    date(2026,6,26), date(2026,9,14), date(2026,10,2), date(2026,10,20),
    date(2026,11,10), date(2026,11,24), date(2026,12,25),
}

def is_trading_day(d):
    return d.weekday() < 5 and d not in TRADING_HOLIDAYS

def last_weekday_of_month(year, month, weekday):
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d

def get_monthly_expiry(symbol, year, month):
    expiry = last_weekday_of_month(year, month, 3)
    while not is_trading_day(expiry):
        expiry -= timedelta(days=1)
    return expiry

def get_active_expiry(symbol, signal_date=None):
    if signal_date is None:
        signal_date = datetime.now(IST).date()
    if isinstance(signal_date, str):
        signal_date = date.fromisoformat(signal_date[:10])
    y, m = signal_date.year, signal_date.month
    expiry = get_monthly_expiry(symbol, y, m)
    td_left = sum(1 for i in range((expiry - signal_date).days + 1) if is_trading_day(signal_date + timedelta(days=i)))
    if td_left <= 5:
        expiry = get_monthly_expiry(symbol, y, m+1) if m < 12 else get_monthly_expiry(symbol, y+1, 1)
    return expiry

def round_to_strike(price, step):
    return round(round(price / step) * step, 2)


# ========================================
# ✅ UPDATED AUTH ROUTES (Auto-converts auth_code → tokens!)
# ========================================
@app.route('/refresh')
def refresh_token():
    return redirect(f"https://api-t1.fyers.in/api/v3/generate-authcode?client_id={FYERS_APP_ID}&redirect_uri={FYERS_REDIRECT_URL}&response_type=code&state=sample_state")


@app.route('/callback')
def callback():
    """Receive auth code from Fyers and convert to access token AUTOMATICALLY"""
    auth_code = request.args.get('code', '')
    
    if not auth_code:
        return redirect('/set-token')
    
    try:
        app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()
        
        print(f"🔑 Received auth_code, exchanging for tokens...")
        
        r = req.post(
            'https://api-t1.fyers.in/api/v3/validate-authcode',
            json={
                'grant_type': 'authorization_code',
                'appIdHash': app_id_hash,
                'code': auth_code,
                'pin': ''
            },
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        
        if r.status_code == 200 and r.json().get('s') == 'ok':
            data = r.json()
            access_token = f"{FYERS_APP_ID}:{data['access_token']}"
            refresh_token = data.get('refresh_token')
            
            save_token(access_token, refresh_token)
            
            print(f"✅ SUCCESS! Got real access_token and refresh_token!")
            
            return f'''
            <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
            <h1 style="font-size:48px">✅ Login Successful!</h1>
            <p style="color:#22c55e;font-size:18px;margin-top:20px">Access token generated automatically!</p>
            <p style="color:#aaa">Time: {datetime.now(IST).strftime('%d %b %Y %H:%M:%S IST')}</p>
            <a href="/" style="color:#22c55e;font-size:18px;margin-top:30px;display:inline-block;padding:12px 30px;background:#166534;border-radius:6px;font-weight:600">📊 Go to Scanner →</a>
            </body></html>
            '''
        else:
            error_msg = r.json().get('message', 'Unknown error')
            print(f"❌ Auth code exchange failed: {error_msg}")
            return f'''
            <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
            <h1 style="color:#ef4444">❌ Login Failed</h1>
            <p style="color:#ef4444">{error_msg}</p>
            <a href="/refresh" style="color:#f59e0b;margin-top:20px;display:inline-block">Try Again</a>
            </body></html>
            '''
            
    except Exception as e:
        print(f"❌ Callback error: {e}")
        return redirect('/set-token')


@app.route('/set-token')
def set_token():
    """Manual token entry - also handles auth_code conversion"""
    access_token = request.args.get('token', '').strip()
    refresh_token = request.args.get('refresh', '').strip()
    auth_code = request.args.get('code', '').strip()
    
    # If user pasted an auth_code, convert it automatically
    if auth_code and not access_token:
        try:
            app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()
            r = req.post(
                'https://api-t1.fyers.in/api/v3/validate-authcode',
                json={
                    'grant_type': 'authorization_code',
                    'appIdHash': app_id_hash,
                    'code': auth_code,
                    'pin': ''
                },
                headers={'Content-Type': 'application/json'},
                timeout=15
            )
            
            if r.status_code == 200 and r.json().get('s') == 'ok':
                data = r.json()
                access_token = f"{FYERS_APP_ID}:{data['access_token']}"
                refresh_token = data.get('refresh_token')
                
                save_token(access_token, refresh_token)
                
                return f'''
                <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
                <h1>✅ Auth Code Converted!</h1>
                <p style="color:#22c55e">Got real access_token & refresh_token!</p>
                <a href="/" style="color:#22c55e;font-size:18px;margin-top:20px;display:inline-block">📊 Go to Scanner →</a>
                </body></html>
                '''
        except Exception as e:
            pass
    
    if not access_token:
        return '''
        <html><body style="font-family:sans-serif;padding:40px;background:#0f1f3d;color:white">
        <h2>Set Fyers Token</h2>
        
        <div style="background:#1a2a4a;padding:20px;border-radius:8px;margin-bottom:20px">
        <h3 style="color:#22c55e">✅ Option A: Auto-Login (Recommended)</h3>
        <p style="color:#aaa">Click below to login via Fyers. Tokens are generated automatically!</p>
        <p style="margin-top:10px"><a href="/refresh" style="color:#fff;text-decoration:none;padding:10px 24px;background:#166534;border-radius:6px;display:inline-block;font-weight:600">🔑 Login via Fyers</a></p>
        </div>
        
        <hr style="border-color:#333;margin:25px 0">
        
        <div style="background:#1a2a4a;padding:20px;border-radius:8px">
        <h3 style="color:#f59e0b">⚙️ Option B: Manual Entry</h3>
        <p style="color:#aaa"><strong>IMPORTANT:</strong> Paste ACCESS TOKEN only (not auth_code!). Format: APPID:eyJ...</p>
        <form method="GET" action="/set-token" style="margin-top:15px">
            <p><label>Access Token:</label><br>
            <input name="token" style="width:500px;padding:8px;margin-top:5px;background:#0f1f3d;color:white;border:1px solid #3b82f6;border-radius:4px"></p>
            <p><label>Refresh Token:</label><br>
            <input name="refresh" style="width:500px;padding:8px;margin-top:5px;background:#0f1f3d;color:white;border:1px solid #3b82f6;border-radius:4px"></p>
            <p><button type="submit" style="padding:10px 24px;background:#22c55e;color:white;border:none;border-radius:6px;cursor:pointer;font-weight:600">💾 Save Token</button></p>
        </form>
        </div>
        </body></html>
        '''

    save_token(access_token, refresh_token if refresh_token else None)
    return f'''
    <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
    <h1 style="font-size:48px">✅ Token Saved!</h1>
    <p style="color:#22c55e;font-size:18px">Time: {datetime.now(IST).strftime('%d %b %Y %H:%M:%S IST')}</p>
    <a href="/" style="color:#22c55e;font-size:18px;margin-top:20px;display:inline-block;padding:12px 30px;background:#166534;border-radius:6px;font-weight:600">📊 Go to Scanner →</a>
    </body></html>
    '''


@app.route('/auto-refresh')
def trigger_auto_refresh():
    success = auto_refresh_access_token()
    return jsonify({'status': 'success' if success else 'error', 'timestamp': datetime.now(IST).isoformat()})


@app.route('/debug-fyers')
def debug_fyers():
    result = {
        'token_exists': bool(token_data.get('access_token')),
        'token_prefix': token_data.get('access_token', '')[:40] + '...' if token_data.get('access_token') else None,
        'token_time': token_data.get('token_time'),
    }
    
    fyers = init_fyers()
    result['fyers_client_created'] = fyers is not None
    
    if fyers:
        try:
            test_data = fyers.history(data={
                'symbol': 'NSE:NIFTY50-INDEX',
                'resolution': '1',
                'date_format': '1',
                'range_from': (datetime.now(IST) - timedelta(days=1)).strftime('%Y-%m-%d'),
                'range_to': datetime.now(IST).strftime('%Y-%m-%d'),
                'cont_flag': '1'
            })
            result['history_status'] = test_data.get('s')
            result['history_message'] = test_data.get('message', '')
            result['candle_count'] = len(test_data.get('candles', []))
        except Exception as e:
            result['error'] = str(e)
    
    return jsonify(result)


def get_fyers_expiry_timestamp(fyers, option_key, target_expiry_date):
    try:
        resp = fyers.optionchain(data={'symbol': option_key, 'strikecount': 1, 'timestamp': ''})
        if resp.get('s') != 'ok':
            return None
        expiry_map = {}
        for item in resp['data'].get('expiryData', []):
            d = datetime.strptime(item['date'], '%d-%m-%Y').date()
            expiry_map[d] = item['expiry']
        if not expiry_map:
            return None
        closest = min(expiry_map.keys(), key=lambda d: abs((d - target_expiry_date).days))
        return expiry_map[closest]
    except:
        return None


def get_tp1_option(symbol, tp1_price, option_type, expiry_date):
    fyers = init_fyers()
    if not fyers:
        return None, None, None
    config = SCANNER_CONFIG.get(symbol, {})
    step = config.get('strike_step', 50)
    try:
        expiry_ts = get_fyers_expiry_timestamp(fyers, config.get('option_key', ''), expiry_date)
        if not expiry_ts:
            return None, None, None
        tp1_rounded = round_to_strike(tp1_price, step)
        resp = fyers.optionchain(data={'symbol': config.get('option_key', ''), 'strikecount': 20, 'timestamp': expiry_ts})
        if resp.get('s') != 'ok':
            return None, None, None
        chain = resp['data'].get('optionsChain', [])
        filtered = [r for r in chain if r.get('option_type') == option_type]
        if not filtered:
            return None, None, None
        best = min(filtered, key=lambda r: abs(r['strike_price'] - tp1_rounded))
        return best['strike_price'], best.get('ltp', 0), best.get('symbol', '')
    except:
        return None, None, None


def calculate_atr_trailing(df, fast_period, fast_mult, slow_period, slow_mult):
    df = df.copy()
    hi, lo, cl = df['high'].values, df['low'].values, df['close'].values
    n = len(df)
    if n < max(fast_period, slow_period) + 5:
        return df
    tr = np.empty(n)
    tr[0] = hi[0] - lo[0]
    for i in range(1, n):
        tr[i] = max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
    def rma(arr, period):
        a = np.zeros(n)
        if n < period:
            return a
        a[period-1] = arr[:period].mean()
        for i in range(period, n):
            a[i] = (a[i-1]*(period-1) + arr[i]) / period
        return a
    fast_atr = rma(tr, fast_period) * fast_mult
    slow_atr = rma(tr, slow_period) * slow_mult
    def trail(atr_sl):
        t = np.zeros(n)
        for i in range(1, n):
            sc, pt, ps = cl[i], t[i-1], cl[i-1]
            if sc > pt and ps > pt: t[i] = max(pt, sc - atr_sl[i])
            elif sc < pt and ps < pt: t[i] = min(pt, sc + atr_sl[i])
            elif sc > pt: t[i] = sc - atr_sl[i]
            else: t[i] = sc + atr_sl[i]
        return t
    t1, t2 = trail(fast_atr), trail(slow_atr)
    df['trail1'], df['trail2'] = t1, t2
    df['fast_atr'], df['slow_atr'] = fast_atr / fast_mult, slow_atr / slow_mult
    buy, sell = np.zeros(n, bool), np.zeros(n, bool)
    for i in range(1, n):
        if t1[i] > t2[i] and t1[i-1] <= t2[i-1]: buy[i] = True
        if t1[i] < t2[i] and t1[i-1] >= t2[i-1]: sell[i] = True
    df['buy_signal'], df['sell_signal'] = buy, sell
    bar_color = []
    for i in range(n):
        if t1[i] > t2[i] and cl[i] > t2[i] and lo[i] > t2[i]: bar_color.append('green')
        elif t1[i] > t2[i] and cl[i] > t2[i] and lo[i] < t2[i]: bar_color.append('blue')
        elif t2[i] > t1[i] and cl[i] < t2[i] and hi[i] < t2[i]: bar_color.append('red')
        elif t2[i] > t1[i] and cl[i] < t2[i] and hi[i] > t2[i]: bar_color.append('yellow')
        else: bar_color.append('neutral')
    df['bar_color'], df['regime'] = bar_color, np.where(t1 > t2, 'BULL', 'BEAR')
    return df


def fetch_candles(instrument_key, interval='1minute', days=90, retry_on_fail=True):
    fyers = init_fyers()
    if not fyers:
        return pd.DataFrame()
    interval_map = {'1minute': '1', '5minute': '5', '15minute': '15', '30minute': '30', '60minute': '60'}
    end_date = datetime.now(IST)
    start_date = end_date - timedelta(days=days)
    data = {'symbol': instrument_key, 'resolution': interval_map.get(interval, '1'), 'date_format': '1', 'range_from': start_date.strftime('%Y-%m-%d'), 'range_to': end_date.strftime('%Y-%m-%d'), 'cont_flag': '1'}
    try:
        response = fyers.history(data=data)
        if response.get('s') != 'ok':
            if retry_on_fail and 'unauthorized' in str(response.get('message', '')).lower():
                if auto_refresh_access_token():
                    return fetch_candles(instrument_key, interval, days, retry_on_fail=False)
            return pd.DataFrame()
        candles = response.get('candles', [])
        if not candles:
            return pd.DataFrame()
        rows = []
        for c in candles:
            dt = pd.to_datetime(c[0], unit='s')
            dt = dt.tz_localize('UTC').tz_convert('Asia/Kolkata').tz_localize(None)
            rows.append({'datetime': dt, 'open': c[1], 'high': c[2], 'low': c[3], 'close': c[4], 'volume': c[5]})
        df = pd.DataFrame(rows)
        df = df.sort_values('datetime').drop_duplicates('datetime').reset_index(drop=True)
        t = df['datetime'].dt.hour * 100 + df['datetime'].dt.minute
        return df[(t >= 915) & (t <= 1530)].reset_index(drop=True)
    except:
        return pd.DataFrame()


def resample_candles(df_1m, minutes):
    if len(df_1m) == 0:
        return pd.DataFrame()
    df = df_1m.copy().set_index('datetime')
    r = df.resample(f'{minutes}min').agg(open=('open','first'), high=('high','max'), low=('low','min'), close=('close','last'), volume=('volume','sum')).dropna().reset_index()
    t = r['datetime'].dt.hour * 100 + r['datetime'].dt.minute
    return r[(t >= 915) & (t <= 1530)].reset_index(drop=True)


def generate_signals():
    now = datetime.now(IST)
    today = now.date()
    signals = []

    print(f"\n{'='*60}")
    print(f"SIGNAL SCAN: {now.strftime('%d %b %Y %H:%M:%S IST')}")
    print(f"{'='*60}")

    for symbol, config in SCANNER_CONFIG.items():
        try:
            print(f"\n📊 Scanning {symbol}...")
            
            # ✅ FIX #1: days=90
            df_1m = fetch_candles(config['instrument_key'], '1minute', days=90)
            
            if len(df_1m) < 50:
                print(f"✗ Insufficient candles: {len(df_1m)}")
                continue

            df = resample_candles(df_1m, config['resample_minutes'])
            if len(df) < max(config['fast_period'], config['slow_period']) + 10:
                continue

            df = calculate_atr_trailing(df, config['fast_period'], config['fast_mult'], config['slow_period'], config['slow_mult'])
            df['date'] = pd.to_datetime(df['datetime']).dt.date
            
            # ✅ FIX #2: Last 200 candles
            if len(df) >= 200:
                scan_df = df.tail(200).copy()
            else:
                scan_df = df.copy()

            signal_count = 0
            for _, row in scan_df.iterrows():
                if not (row.get('buy_signal', False) or row.get('sell_signal', False)):
                    continue

                direction = 'BUY-LONG' if row['buy_signal'] else 'SELL-SHORT'
                entry = round(float(row['close']), 2)
                trail2 = round(float(row['trail2']), 2)
                trail1 = round(float(row['trail1']), 2)

                if direction == 'BUY-LONG':
                    sl, risk = trail2, entry - sl
                    target_1, target_2 = round(entry + risk * 1.5, 2), round(entry + risk * 2.5, 2)
                else:
                    sl, risk = trail2, sl - entry
                    target_1, target_2 = round(entry - risk * 1.5, 2), round(entry - risk * 2.5, 2)

                risk = abs(risk)
                if risk == 0:
                    continue

                reward, rr = abs(target_2 - entry), round(reward / risk, 2)
                confidence = 0.5
                bar_c = row.get('bar_color', 'neutral')

                if direction == 'BUY-LONG':
                    if bar_c == 'green': confidence += 0.2
                    elif bar_c == 'blue': confidence += 0.1
                else:
                    if bar_c == 'red': confidence += 0.2
                    elif bar_c == 'yellow': confidence += 0.1
                if rr >= 2: confidence += 0.1
                if rr >= 3: confidence += 0.1
                confidence = min(confidence, 0.95)

                if confidence >= 0.8: grade, grade_score = 'A+', 95
                elif confidence >= 0.7: grade, grade_score = 'A', 85
                elif confidence >= 0.6: grade, grade_score = 'B', 70
                else: grade, grade_score = 'C', 55

                signal_dt = pd.to_datetime(row['datetime'])
                if signal_dt.tzinfo is None:
                    signal_dt = IST.localize(signal_dt)

                signals.append({
                    '_id': f"{symbol}_{signal_dt.strftime('%Y%m%d_%H%M')}",
                    'symbol': symbol, 'direction': direction, 'model': 'ATR-TS',
                    'entry': entry, 'sl': sl, 'target_1': target_1, 'target_2': target_2, 'target': target_2,
                    'risk_reward': f"1:{rr}", 'confidence': round(confidence, 2),
                    'grade': grade, 'grade_score': grade_score,
                    'scan_date': signal_dt.isoformat(), 'scan_time': signal_dt.strftime('%H:%M'),
                    'trail1': trail1, 'trail2': trail2,
                    'fast_atr': round(float(row['fast_atr']), 2), 'slow_atr': round(float(row['slow_atr']), 2),
                    'bar_color': bar_c, 'regime': row.get('regime', 'UNKNOWN'),
                    'timeframe': f"{config['resample_minutes']}m", 'lot_size': config['lot_size'],
                    'scanner_type': 'atr_trailing', 'outcome': 'pending'
                })
                signal_count += 1

            print(f"✓ {symbol}: {signal_count} signal(s)")
        except Exception as e:
            print(f"✗ Error scanning {symbol}: {e}")

    signals.sort(key=lambda x: x.get('scan_date', ''), reverse=True)
    existing = scan_cache.get('signals', [])
    existing_ids = {s['_id'] for s in signals}
    for s in existing:
        if s['_id'] not in existing_ids and s.get('scan_date', '')[:10] == datetime.now(IST).strftime('%Y-%m-%d'):
            signals.append(s)
    signals.sort(key=lambda x: x.get('scan_date', ''), reverse=True)
    print(f"\n✓ TOTAL: {len(signals)} signals")
    return signals


def generate_option_signals(futures_signals):
    results = []
    for sig in futures_signals:
        symbol = sig.get('symbol', '')
        config = SCANNER_CONFIG.get(symbol)
        if not config:
            continue
        direction = sig.get('direction', '')
        opt_type = 'CE' if direction == 'BUY-LONG' else 'PE'
        tp1 = float(sig.get('target_1', 0))
        lot = config['lot_size']
        expiry = get_active_expiry(symbol, datetime.now(IST).date())
        strike, ltp, opt_symbol = get_tp1_option(symbol, tp1, opt_type, expiry)
        results.append({
            '_id': sig['_id'] + '_OPT', 'symbol': symbol, 'direction': direction,
            'opt_type': opt_type, 'action': 'BUY ' + opt_type,
            'spot': float(sig.get('entry', 0)), 'tp1': tp1,
            'strike': strike, 'ltp': round(ltp, 2) if ltp else None,
            'opt_symbol': opt_symbol, 'expiry': expiry.strftime('%d %b %Y'),
            'days_to_expiry': (expiry - datetime.now(IST).date()).days,
            'lot_size': lot, 'max_risk': round(ltp * lot, 0) if ltp else None,
            'scan_date': sig.get('scan_date', ''), 'scan_time': sig.get('scan_time', ''),
            'grade': sig.get('grade', ''), 'grade_score': sig.get('grade_score', 0), 'confidence': sig.get('confidence', 0)
        })
    return results


def get_scanner_status():
    now = datetime.now(IST)
    time_val = now.hour * 100 + now.minute
    if not token_data['access_token']:
        return 'NO_TOKEN'
    if now.weekday() >= 5:
        return 'MARKET_CLOSED'
    if now.date() in TRADING_HOLIDAYS:
        return 'MARKET_CLOSED'
    if 915 <= time_val <= 1530:
        return 'ACTIVE'
    if 900 <= time_val < 915:
        return 'PRE_MARKET'
    return 'MARKET_CLOSED'


@app.route('/')
def home():
    ts = '✅ Token Active' if token_data['access_token'] else '🔴 Token Expired'
    tt = token_data.get('token_time', 'Never')
    return f'''
    <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f1f3d;color:white">
    <h1>⚡ StrikeTrail</h1><p style="color:#aaa">ATR Trailing Stop Scanner</p>
    <div style="background:#1a2a4a;padding:20px;margin:20px auto;max-width:500px;border-radius:8px">
    <p style="margin:10px"><strong>Status:</strong> {ts}</p>
    <p style="margin:10px"><strong>Login Time:</strong> {tt}</p>
    <p style="margin:10px"><strong>Server Time:</strong> {datetime.now(IST).strftime('%d %b %Y %H:%M:%S')}</p>
    </div>
    <p><a href="/refresh" style="color:#22c55e;text-decoration:none;padding:10px 24px;background:#166534;border-radius:6px;display:inline-block;margin:5px">🔑 Login via Fyers</a></p>
    <p><a href="/api/signals" style="color:#22d3ee;text-decoration:none;padding:10px 24px;background:#164e63;border-radius:6px;display:inline-block;margin:5px">📊 Signals</a></p>
    <p><a href="/debug-fyers" style="color:#a78bfa;text-decoration:none;padding:10px 24px;background:#4c1d95;border-radius:6px;display:inline-block;margin:5px">🔍 Debug</a></p>
    </body></html>
    '''


@app.route('/api/status')
def api_status():
    return jsonify({
        'status': 'success', 'scanner_status': get_scanner_status(),
        'server_time_ist': datetime.now(IST).isoformat(),
        'token_set': token_data['access_token'] is not None,
        'token_time': token_data.get('token_time')
    })


@app.route('/api/signals')
def api_signals():
    now = datetime.now(IST)
    status = get_scanner_status()
    if status == 'NO_TOKEN':
        return jsonify({'status': 'success', 'scanner_status': 'NO_TOKEN', 'signals': [], 'timestamp': now.isoformat()})
    if scan_cache['last_scan'] and (now - scan_cache['last_scan']).total_seconds() < 60:
        return jsonify({'status': 'success', 'scanner_status': status, 'signals': scan_cache['signals'], 'cached': True, 'timestamp': now.isoformat()})
    if status in ['ACTIVE', 'PRE_MARKET']:
        signals = generate_signals()
    else:
        signals = scan_cache.get('signals', [])
    scan_cache['signals'] = signals
    scan_cache['last_scan'] = now
    return jsonify({'status': 'success', 'scanner_status': status, 'signals': signals, 'cached': False, 'timestamp': now.isoformat()})


@app.route('/api/option-signals')
def api_option_signals():
    now = datetime.now(IST)
    if options_cache['last_fetch'] and (now - options_cache['last_fetch']).total_seconds() < 120:
        return jsonify({'status': 'success', 'option_signals': options_cache['signals'], 'cached': True, 'timestamp': now.isoformat()})
    futures = scan_cache.get('signals', [])
    opt_signals = generate_option_signals(futures)
    options_cache['signals'] = opt_signals
    options_cache['last_fetch'] = now
    return jsonify({'status': 'success', 'option_signals': opt_signals, 'cached': False, 'timestamp': now.isoformat()})


@app.route('/api/track', methods=['POST'])
def api_track():
    try:
        data = request.json
        if not data or 'signals' not in data:
            return jsonify({'status': 'error', 'message': 'No signals'})
        results = []
        for sig in data['signals']:
            symbol = sig.get('symbol', '')
            config = SCANNER_CONFIG.get(symbol)
            if not config:
                results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': 'no_config'})
                continue
            try:
                signal_time = pd.to_datetime(sig.get('scan_date')).replace(tzinfo=None)
                df_1m = fetch_candles(config['instrument_key'], '1minute', days=10)
                if len(df_1m) == 0:
                    results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': 'no_data'})
                    continue
                df_1m['datetime'] = pd.to_datetime(df_1m['datetime']).dt.tz_localize(None)
                df_after = df_1m[df_1m['datetime'] > signal_time].reset_index(drop=True)
                if len(df_after) == 0:
                    results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': 'no_candles_after'})
                    continue
                entry_met = False
                entry_idx = None
                direction = sig.get('direction', '')
                entry = float(sig.get('entry', 0))
                sl = float(sig.get('sl', 0))
                t2 = float(sig.get('target_2', sig.get('target', 0)))
                for idx, row in df_after.iterrows():
                    if direction == 'BUY-LONG' and row['high'] >= entry:
                        entry_met = True; entry_idx = idx; break
                    elif direction == 'SELL-SHORT' and row['low'] <= entry:
                        entry_met = True; entry_idx = idx; break
                if not entry_met:
                    results.append({'_id': sig.get('_id'), 'status': 'pending', 'current_price': float(df_after.iloc[-1]['close']), 'track_status': 'entry_not_met'})
                    continue
                entry_pos = df_after.index.get_loc(entry_idx)
                df_post = df_after.iloc[entry_pos:].reset_index(drop=True)
                trade_status = 'open'
                exit_price = None
                current_price = float(df_post.iloc[-1]['close'])
                for _, row in df_post.iterrows():
                    if direction == 'BUY-LONG':
                        if row['high'] >= t2: trade_status = 'target_hit'; exit_price = t2; break
                        if row['low'] <= sl: trade_status = 'stop_hit'; exit_price = sl; break
                    else:
                        if row['low'] <= t2: trade_status = 'target_hit'; exit_price = t2; break
                        if row['high'] >= sl: trade_status = 'stop_hit'; exit_price = sl; break
                pnl_pct = round((current_price - entry) / entry * 100, 2) if direction == 'BUY-LONG' else round((entry - current_price) / entry * 100, 2)
                results.append({'_id': sig.get('_id'), 'status': trade_status, 'exit_price': exit_price, 'current_price': current_price, 'live_pnl_pct': pnl_pct, 'track_status': 'tracked'})
            except Exception as e:
                results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': f'error:{str(e)}'})
        return jsonify({'status': 'success', 'results': results})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n🚀 SCANNER STARTING on port {port}")
    print(f"Token: {'✅' if token_data['access_token'] else '🔴 Not Set'}")
    app.run(host='0.0.0.0', port=port, debug=False)
