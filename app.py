from flask import Flask, jsonify, request, redirect, session
from flask_cors import CORS
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, date
import calendar
import pytz
import os
import json
import time
import hashlib
from urllib.parse import quote
from fyers_apiv3 import fyersModel

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get('FLASK_SECRET', 'atr-scanner-secret-key-2024')

# ========================================
# 🔑 FYERS CREDENTIALS
# ========================================
FYERS_APP_ID = os.environ.get('FYERS_APP_ID', 'VS55VDHYCW-100')
FYERS_SECRET_KEY = os.environ.get('FYERS_SECRET_KEY', '724FOKKSFS')
FYERS_REDIRECT_URL = os.environ.get('FYERS_REDIRECT_URL', 'https://trade.fyers.in/api-login/redirect-uri/index.html')
FYERS_PIN = os.environ.get('FYERS_PIN', '2504')
FYERS_REFRESH_TOKEN_FILE = 'fyers_refresh_token.txt'
FYERS_ACCESS_TOKEN_FILE = 'fyers_access_token.txt'

# ========================================
# Scanner Settings (for Nifty 50 and Bank Nifty)
# ========================================
SCANNER_CONFIG = {
    'NIFTY': {
        'instrument_key': 'NSE:NIFTY50-INDEX',
        'timeframe': '1minute',
        'resample_minutes': 15,
        'fast_period': 3,
        'fast_mult': 1.0,
        'slow_period': 25,
        'slow_mult': 2.0,
        'lot_size': 50,
        'strike_step': 50,
        'options_key': 'NSE:NIFTY50-INDEX'
    },
    'BANKNIFTY': {
        'instrument_key': 'NSE:NIFTYBANK-INDEX',
        'timeframe': '1minute',
        'resample_minutes': 5,
        'fast_period': 5,
        'fast_mult': 0.7,
        'slow_period': 20,
        'slow_mult': 3.5,
        'lot_size': 25,
        'strike_step': 100,
        'options_key': 'NSE:NIFTYBANK-INDEX'
    }
}

IST = pytz.timezone('Asia/Kolkata')

# Token storage
token_data = {
    'access_token': None,
    'token_time': None
}

# Cache
scan_cache = {
    'signals': [],
    'last_scan': None,
    'daily_trades': {}
}

options_cache = {
    'signals': [],
    'last_fetch': None
}

# ========================================
# EXPIRY LOGIC
# ========================================
TRADING_HOLIDAYS = {
    date(2024,1,26), date(2024,3,25), date(2024,4,14), date(2024,4,17),
    date(2024,5,1),  date(2024,6,17), date(2024,8,15), date(2024,10,2),
    date(2024,10,24),date(2024,11,1), date(2024,11,15),date(2024,12,25),
    date(2025,1,26), date(2025,2,26), date(2025,3,14), date(2025,3,31),
    date(2025,4,10), date(2025,4,14), date(2025,4,18), date(2025,5,1),
    date(2025,8,15), date(2025,10,2), date(2025,10,23),date(2025,12,25),
    date(2026,1,26), date(2026,3,25),
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
    """Last Thursday for Nifty/BankNifty."""
    expiry = last_weekday_of_month(year, month, 3)  # Thursday
    while not is_trading_day(expiry):
        expiry -= timedelta(days=1)
    return expiry

def get_active_expiry(symbol, signal_date=None):
    """Return active monthly expiry. Roll to next month in last 5 trading days."""
    if signal_date is None:
        signal_date = datetime.now(IST).date()
    if isinstance(signal_date, str):
        signal_date = date.fromisoformat(signal_date[:10])
    y, m = signal_date.year, signal_date.month
    expiry = get_monthly_expiry(symbol, y, m)
    td_left = sum(
        1 for i in range((expiry - signal_date).days + 1)
        if is_trading_day(signal_date + timedelta(days=i))
    )
    if td_left <= 5:
        if m < 12:
            expiry = get_monthly_expiry(symbol, y, m + 1)
        else:
            expiry = get_monthly_expiry(symbol, y + 1, 1)
    return expiry

def round_to_strike(price, step):
    return round(round(price / step) * step, 2)

# ========================================
# FYERS TOKEN MANAGEMENT
# ========================================
def generate_app_id_hash():
    """Generate SHA-256 hash of APP_ID:SECRET_KEY"""
    app_id_hash = hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()
    return app_id_hash

def load_refresh_token():
    """Load refresh token from environment variable"""
    try:
        refresh_token = os.environ.get('FYERS_REFRESH_TOKEN', '')
        if refresh_token:
            return refresh_token.strip()
    except Exception as e:
        print(f"Error loading refresh token: {e}")
    return None

def save_refresh_token(token):
    """Save refresh token to file"""
    try:
        with open(FYERS_REFRESH_TOKEN_FILE, 'w') as f:
            f.write(token)
        return True
    except Exception as e:
        print(f"Error saving refresh token: {e}")
        return False

def load_access_token():
    """Load access token from file"""
    try:
        if os.path.exists(FYERS_ACCESS_TOKEN_FILE):
            with open(FYERS_ACCESS_TOKEN_FILE, 'r') as f:
                return f.read().strip()
    except Exception as e:
        print(f"Error loading access token: {e}")
    return None

def save_access_token(token):
    """Save access token to file"""
    try:
        with open(FYERS_ACCESS_TOKEN_FILE, 'w') as f:
            f.write(token)
        return True
    except Exception as e:
        print(f"Error saving access token: {e}")
        return False

def refresh_access_token():
    """Refresh Fyers access token using refresh token"""
    try:
        refresh_token = load_refresh_token()
        if not refresh_token:
            return None
            
        app_id_hash = generate_app_id_hash()
        
        # Create session for token refresh
        fyers = fyersModel.FyersModel(
            client_id=FYERS_APP_ID,
            secret_key=FYERS_SECRET_KEY,
            redirect_uri=FYERS_REDIRECT_URL,
            state="sample_state",
            grant_type="refresh_token",
            response_type="code",
            refresh_token=refresh_token,
            pin=FYERS_PIN
        )
        
        # Generate new access token
        response = fyers.generate_authcode()
        
        if 'access_token' in response:
            access_token = response['access_token']
            save_access_token(access_token)
            return access_token
        else:
            print(f"Token refresh failed: {response}")
            return None
            
    except Exception as e:
        print(f"Error refreshing access token: {e}")
        return None

def init_fyers():
    """Initialize Fyers with current access token"""
    try:
        access_token = load_access_token()
        if not access_token:
            # Try to refresh token
            access_token = refresh_access_token()
            if not access_token:
                return None
                
        fyers = fyersModel.FyersModel(
            client_id=FYERS_APP_ID,
            token=access_token,
            log_path="/tmp"
        )
        return fyers
    except Exception as e:
        print(f"Error initializing Fyers: {e}")
        return None

def get_fyers_headers():
    access_token = load_access_token()
    if not access_token:
        return None
    return {
        'Authorization': f"Bearer {access_token}",
        'Accept': 'application/json'
    }

# ========================================
# AUTH ROUTES
# ========================================
@app.route('/refresh')
def refresh_token():
    import hashlib
    state = 'sample_state'
    auth_url = (
        f"https://api-t1.fyers.in/api/v3/generate-authcode"
        f"?client_id={API_KEY}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&state={state}"
    )
    return redirect(auth_url)

# ========================================
# CORE: ATR TRAILING STOP CALCULATOR
# ========================================
def calculate_atr_trailing(df, fast_period, fast_mult, slow_period, slow_mult):
    df   = df.copy()
    high  = df['high'].values
    low   = df['low'].values
    close = df['close'].values
    n     = len(df)

    if n < max(fast_period, slow_period) + 5:
        return df

    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))

    def wilder_rma(arr, period):
        a = np.zeros(n)
        if n >= period:
            a[period-1] = np.mean(arr[:period])
            for i in range(period, n):
                a[i] = (a[i-1]*(period-1) + arr[i]) / period
        return a

    fast_sl = fast_mult * wilder_rma(tr, fast_period)
    slow_sl = slow_mult * wilder_rma(tr, slow_period)

    def trail(atr_sl):
        t = np.zeros(n)
        for i in range(1, n):
            sc, pt, ps = close[i], t[i-1], close[i-1]
            if   sc > pt and ps > pt: t[i] = max(pt, sc - atr_sl[i])
            elif sc < pt and ps < pt: t[i] = min(pt, sc + atr_sl[i])
            elif sc > pt:             t[i] = sc - atr_sl[i]
            else:                     t[i] = sc + atr_sl[i]
        return t

    trail1 = trail(fast_sl)
    trail2 = trail(slow_sl)

    df['trail1']   = trail1
    df['trail2']   = trail2
    df['fast_atr'] = wilder_rma(tr, fast_period)
    df['slow_atr'] = wilder_rma(tr, slow_period)

    df['buy_signal']  = False
    df['sell_signal'] = False
    for i in range(1, n):
        if trail1[i] > trail2[i] and trail1[i-1] <= trail2[i-1]:
            df.iloc[i, df.columns.get_loc('buy_signal')]  = True
        if trail1[i] < trail2[i] and trail1[i-1] >= trail2[i-1]:
            df.iloc[i, df.columns.get_loc('sell_signal')] = True

    df['bar_color'] = 'neutral'
    for i in range(n):
        if trail1[i] > trail2[i] and close[i] > trail2[i] and low[i]  > trail2[i]: df.iloc[i, df.columns.get_loc('bar_color')] = 'green'
        elif trail1[i] > trail2[i] and close[i] > trail2[i] and low[i]  < trail2[i]: df.iloc[i, df.columns.get_loc('bar_color')] = 'blue'
        elif trail2[i] > trail1[i] and close[i] < trail2[i] and high[i] < trail2[i]: df.iloc[i, df.columns.get_loc('bar_color')] = 'red'
        elif trail2[i] > trail1[i] and close[i] < trail2[i] and high[i] > trail2[i]: df.iloc[i, df.columns.get_loc('bar_color')] = 'yellow'

    df['regime'] = np.where(trail1 > trail2, 'BULL', 'BEAR')
    return df

# ========================================
# DATA FETCHING (FYERS)
# ========================================
def fetch_candles(instrument_key, interval='1minute', days=2):
    fyers = init_fyers()
    if not fyers:
        return pd.DataFrame()

    try:
        # Convert interval to Fyers format
        interval_map = {
            '1minute': '1',
            '5minute': '5',
            '15minute': '15',
            '30minute': '30',
            '60minute': '60'
        }
        
        end_date = datetime.now(IST)
        start_date = end_date - timedelta(days=days)
        
        # Format dates for Fyers
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')
        
        data = {
            "symbol": instrument_key,
            "resolution": interval_map.get(interval, '1'),
            "date_format": "1",
            "range_from": start_date_str,
            "range_to": end_date_str,
            "cont_flag": "1"
        }
        
        response = fyers.historical_data(data)
        
        if response.get('s') == 'ok':
            candles = response.get('candles', [])
            if not candles:
                return pd.DataFrame()
                
            rows = []
            for candle in candles:
                # Fyers format: [timestamp, open, high, low, close, volume]
                rows.append({
                    'datetime': datetime.fromtimestamp(candle[0]),
                    'open': candle[1],
                    'high': candle[2],
                    'low': candle[3],
                    'close': candle[4],
                    'volume': candle[5]
                })
            
            df = pd.DataFrame(rows)
            df['datetime'] = pd.to_datetime(df['datetime'])
            df = df.sort_values('datetime').drop_duplicates(subset='datetime').reset_index(drop=True)
            df['time_val'] = df['datetime'].dt.hour * 100 + df['datetime'].dt.minute
            df = df[(df['time_val'] >= 915) & (df['time_val'] <= 1530)].drop(columns=['time_val'])
            return df.reset_index(drop=True)
        else:
            print(f"Fyers API error: {response}")
            return pd.DataFrame()
            
    except Exception as e:
        print(f"Fetch error: {e}")
        return pd.DataFrame()

def resample_candles(df_1m, minutes):
    if len(df_1m) == 0:
        return pd.DataFrame()
    df = df_1m.copy()
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.set_index('datetime')
    resampled = df.resample(f'{minutes}min').agg(
        open=('open','first'), high=('high','max'),
        low=('low','min'), close=('close','last'), volume=('volume','sum')
    ).dropna().reset_index()
    resampled['time_val'] = resampled['datetime'].dt.hour * 100 + resampled['datetime'].dt.minute
    resampled = resampled[(resampled['time_val'] >= 915) & (resampled['time_val'] <= 1530)]
    return resampled.drop(columns=['time_val']).reset_index(drop=True)

# ========================================
# OPTION DATA FETCHING (Placeholder for now)
# ========================================
def get_option_contracts_live(symbol, expiry_date_str):
    """Placeholder for option contracts - to be implemented"""
    return {}

def get_option_ltp(symbol, spot_price, option_type, expiry_date_str, otm=False):
    """Placeholder for option LTP - to be implemented"""
    return None, 0, None

# ========================================
# SIGNAL GENERATION
# ========================================
def generate_signals():
    now     = datetime.now(IST)
    signals = []

    for symbol, config in SCANNER_CONFIG.items():
        try:
            df_1m = fetch_candles(config['instrument_key'], '1minute', days=5)
            if len(df_1m) < 50:
                continue

            df = resample_candles(df_1m, config['resample_minutes'])
            if len(df) < max(config['fast_period'], config['slow_period']) + 10:
                continue

            df = calculate_atr_trailing(
                df, config['fast_period'], config['fast_mult'],
                config['slow_period'], config['slow_mult']
            )

            today    = now.date()
            df['date'] = pd.to_datetime(df['datetime']).dt.date
            today_df = df[df['date'] == today]
            if len(today_df) == 0:
                today_df = df.tail(20)

            for idx, row in today_df.iterrows():
                if not (row.get('buy_signal', False) or row.get('sell_signal', False)):
                    continue

                direction     = 'BUY-LONG' if row['buy_signal'] else 'SELL-SHORT'
                entry         = round(row['close'], 2)
                trail2        = round(row['trail2'], 2)
                trail1        = round(row['trail1'], 2)
                fast_atr_val  = round(row['fast_atr'], 2)
                slow_atr_val  = round(row['slow_atr'], 2)

                if direction == 'BUY-LONG':
                    sl       = trail2
                    risk     = entry - sl
                    target_1 = round(entry + risk * 1.5, 2)
                    target_2 = round(entry + risk * 2.5, 2)
                else:
                    sl       = trail2
                    risk     = sl - entry
                    target_1 = round(entry - risk * 1.5, 2)
                    target_2 = round(entry - risk * 2.5, 2)

                risk = abs(risk)
                if risk == 0:
                    continue

                reward     = abs(target_2 - entry)
                rr         = round(reward / risk, 2) if risk > 0 else 0
                confidence = 0.5
                bar_c      = row.get('bar_color', 'neutral')

                if direction == 'BUY-LONG':
                    if bar_c == 'green': confidence += 0.2
                    elif bar_c == 'blue': confidence += 0.1
                else:
                    if bar_c == 'red':    confidence += 0.2
                    elif bar_c == 'yellow': confidence += 0.1

                if rr >= 2: confidence += 0.1
                if rr >= 3: confidence += 0.1
                confidence = min(confidence, 0.95)

                if confidence >= 0.8:   grade, grade_score = 'A+', 95
                elif confidence >= 0.7: grade, grade_score = 'A',  85
                elif confidence >= 0.6: grade, grade_score = 'B',  70
                else:                   grade, grade_score = 'C',  55

                signal_time = pd.to_datetime(row['datetime'])
                if signal_time.tzinfo is None:
                    signal_time = signal_time.tz_localize(IST)
                else:
                    signal_time = signal_time.tz_convert(IST)

                signals.append({
                    '_id':          f"{symbol}_{signal_time.strftime('%Y%m%d_%H%M')}",
                    'symbol':       symbol,
                    'direction':    direction,
                    'model':        'ATR-TS',
                    'entry':        entry,
                    'sl':           sl,
                    'target_1':     target_1,
                    'target_2':     target_2,
                    'target':       target_2,
                    'risk_reward':  f"1:{rr}",
                    'confidence':   round(confidence, 2),
                    'grade':        grade,
                    'grade_score':  grade_score,
                    'scan_date':    signal_time.isoformat(),
                    'scan_time':    signal_time.strftime('%H:%M'),
                    'trail1':       trail1,
                    'trail2':       trail2,
                    'fast_atr':     fast_atr_val,
                    'slow_atr':     slow_atr_val,
                    'bar_color':    bar_c,
                    'regime':       row.get('regime', 'UNKNOWN'),
                    'timeframe':    f"{config['resample_minutes']}m",
                    'lot_size':     config['lot_size'],
                    'scanner_type': 'atr_trailing',
                    'outcome':      'pending'
                })

        except Exception as e:
            print(f"Error scanning {symbol}: {e}")
            continue

    signals.sort(key=lambda x: x.get('scan_date', ''), reverse=True)

    # Merge with previously cached signals from today so none are lost
    existing = scan_cache.get('signals', [])
    existing_ids = {s['_id'] for s in signals}
    for s in existing:
        if s['_id'] not in existing_ids:
            # Keep old signals from today
            sig_date = s.get('scan_date','')[:10]
            today_str = datetime.now(IST).strftime('%Y-%m-%d')
            if sig_date == today_str:
                signals.append(s)

    signals.sort(key=lambda x: x.get('scan_date', ''), reverse=True)
    return signals

def generate_option_signals(futures_signals):
    """
    For each futures signal, fetch ATM + OTM option LTP and build option signal card.
    """
    now            = datetime.now(IST)
    today          = now.date()
    option_signals = []

    for sig in futures_signals:
        symbol    = sig.get('symbol', '')
        config    = SCANNER_CONFIG.get(symbol)
        if not config:
            continue

        direction   = sig.get('direction', '')
        opt_type    = 'CE' if direction == 'BUY-LONG' else 'PE'
        spot        = float(sig.get('entry', 0))
        step        = config.get('strike_step', 50)
        lot         = config.get('lot_size', 1)
        expiry      = get_active_expiry(symbol, today)
        expiry_str  = expiry.strftime('%Y-%m-%d')
        expiry_disp = expiry.strftime('%d %b %Y')
        days_to_exp = (expiry - today).days

        atm_strike = round_to_strike(spot, step)
        otm_strike = (atm_strike + step) if opt_type == 'CE' else (atm_strike - step)

        atm_ltp, atm_s, atm_key = get_option_ltp(symbol, spot, opt_type, expiry_str, otm=False)
        otm_ltp, otm_s, otm_key = get_option_ltp(symbol, spot, opt_type, expiry_str, otm=True)

        # Estimated P&L: use SL/target from futures signal scaled to option premium
        # ATM: target ×2 premium, SL = −50% premium
        atm_pnl_target = round(atm_ltp * 2.0 * lot, 0) if atm_ltp else None
        atm_pnl_sl     = round(atm_ltp * 0.5 * lot * -1, 0) if atm_ltp else None
        otm_pnl_target = round(otm_ltp * 2.5 * lot, 0) if otm_ltp else None
        otm_pnl_sl     = round(otm_ltp * 0.5 * lot * -1, 0) if otm_ltp else None

        option_signals.append({
            '_id':           sig.get('_id', '') + '_OPT',
            'futures_id':    sig.get('_id', ''),
            'symbol':        symbol,
            'direction':     direction,
            'opt_type':      opt_type,
            'spot':          spot,
            'expiry':        expiry_str,
            'expiry_display':expiry_disp,
            'days_to_expiry':days_to_exp,
            'scan_date':     sig.get('scan_date', ''),
            'scan_time':     sig.get('scan_time', ''),
            'grade':         sig.get('grade', ''),
            'grade_score':   sig.get('grade_score', 0),
            'confidence':    sig.get('confidence', 0),
            'lot_size':      lot,
            'atm': {
                'strike':     atm_s,
                'ltp':        round(atm_ltp, 2) if atm_ltp else None,
                'instrument': atm_key,
                'pnl_target': atm_pnl_target,
                'pnl_sl':     atm_pnl_sl,
                'max_risk':   round(atm_ltp * lot, 0) if atm_ltp else None,
            },
            'otm': {
                'strike':     otm_s,
                'ltp':        round(otm_ltp, 2) if otm_ltp else None,
                'instrument': otm_key,
                'pnl_target': otm_pnl_target,
                'pnl_sl':     otm_pnl_sl,
                'max_risk':   round(otm_ltp * lot, 0) if otm_ltp else None,
            }
        })

    return option_signals

def get_scanner_status():
    now      = datetime.now(IST)
    hour     = now.hour
    minute   = now.minute
    day      = now.weekday()

    access_token = load_access_token()
    if not access_token:
        return 'NO_TOKEN'
    if day >= 5:
        return 'MARKET_CLOSED'

    time_val = hour * 100 + minute
    if 915 <= time_val <= 1530:   return 'ACTIVE'
    elif 900 <= time_val < 915:   return 'PRE_MARKET'
    else:                          return 'MARKET_CLOSED'

# ========================================
# API ROUTES
# ========================================
@app.route('/')
def home():
    return '''
    <html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#1a2a4a;color:white">
    <h1>⚡ Fyers ATR Trailing Stop Scanner</h1><p>API is running</p>
    <p><a href="/refresh" style="color:#22c55e">🔑 Refresh Token</a></p>
    <p><a href="/api/status" style="color:#3b82f6">📊 API Status</a></p>
    <p><a href="/api/signals" style="color:#f59e0b">📡 Get Signals</a></p>
    <p><a href="/api/option-signals" style="color:#a78bfa">🎯 Option Signals</a></p>
    </body></html>'''

@app.route('/api/status')
def api_status():
    now = datetime.now(IST)
    return jsonify({
        'status':         'success',
        'scanner_status': get_scanner_status(),
        'server_time_ist':now.isoformat(),
        'token_set':      load_access_token() is not None,
        'scanner_model':  'ATR Trailing Stop',
        'config': {
            sym: {
                'timeframe': f"{cfg['resample_minutes']}m",
                'fast':      f"({cfg['fast_period']}, {cfg['fast_mult']})",
                'slow':      f"({cfg['slow_period']}, {cfg['slow_mult']})"
            } for sym, cfg in SCANNER_CONFIG.items()
        }
    })

@app.route('/api/signals')
def api_signals():
    now    = datetime.now(IST)
    status = get_scanner_status()

    if status == 'NO_TOKEN':
        return jsonify({'status': 'success', 'scanner_status': 'NO_TOKEN',
                        'signals': [], 'timestamp': now.isoformat()})

    if (scan_cache['last_scan'] and
            (now - scan_cache['last_scan']).total_seconds() < 60):
        return jsonify({
            'status':        'success',
            'scanner_status': status,
            'signals':        scan_cache['signals'],
            'last_scan':      scan_cache['last_scan'].isoformat(),
            'daily_trades':   scan_cache.get('daily_trades', {}),
            'timestamp':      now.isoformat()
        })

    signals = generate_signals() if status in ['ACTIVE', 'SCANNING', 'PRE_MARKET'] else scan_cache.get('signals', [])
    scan_cache['signals']   = signals
    scan_cache['last_scan'] = now

    return jsonify({
        'status':        'success',
        'scanner_status': status,
        'signals':        signals,
        'last_scan':      now.isoformat(),
        'daily_trades':   scan_cache.get('daily_trades', {}),
        'timestamp':      now.isoformat()
    })

@app.route('/api/option-signals')
def api_option_signals():
    """
    Returns ATM + OTM option data for each active futures signal.
    Cached for 5 minutes.
    """
    now    = datetime.now(IST)
    status = get_scanner_status()

    if status == 'NO_TOKEN':
        return jsonify({'status': 'success', 'scanner_status': 'NO_TOKEN',
                        'option_signals': [], 'timestamp': now.isoformat()})

    # Use cached option signals if fresh (2 min only)
    if (options_cache['last_fetch'] and
            (now - options_cache['last_fetch']).total_seconds() < 120):
        return jsonify({
            'status':         'success',
            'scanner_status': status,
            'option_signals': options_cache['signals'],
            'last_fetch':     options_cache['last_fetch'].isoformat(),
            'timestamp':      now.isoformat()
        })

    # Clear contract cache to get fresh LTPs
    # Note: Placeholder implementation
    opt_signals = []

    return jsonify({
        'status':         'success',
        'scanner_status': status,
        'option_signals': opt_signals,
        'last_fetch':     now.isoformat(),
        'timestamp':      now.isoformat()
    })

@app.route('/api/track', methods=['POST'])
def api_track():
    try:
        data = request.json
        if not data or 'signals' not in data:
            return jsonify({'status': 'error', 'message': 'No signals provided'})

        results = []

        for sig in data['signals']:
            symbol    = sig.get('symbol', '')
            config    = SCANNER_CONFIG.get(symbol)
            entry     = float(sig.get('entry', 0))
            sl        = float(sig.get('sl', 0))
            t1        = float(sig.get('target_1', sig.get('target', 0)))
            t2        = float(sig.get('target_2', sig.get('target', 0)))
            direction = sig.get('direction', '')
            scan_date = sig.get('scan_date', '')

            try:
                signal_time = pd.to_datetime(scan_date).replace(tzinfo=None)
                df_1m       = fetch_candles(config['instrument_key'], '1minute', days=5)

                if len(df_1m) == 0:
                    results.append({'_id': sig.get('_id'), 'status': 'pending',
                                     'exit_price': None, 'current_price': None,
                                     'live_pnl_pct': 0, 'track_status': 'no_candles_fetched'})
                    continue

                df_1m['datetime'] = pd.to_datetime(df_1m['datetime']).dt.tz_localize(None)
                df_after = df_1m[df_1m['datetime'] > signal_time].reset_index(drop=True)

                if len(df_after) == 0:
                    results.append({'_id': sig.get('_id'), 'status': 'pending',
                                     'exit_price': None, 'current_price': None,
                                     'live_pnl_pct': 0, 'track_status': 'no_candles_after_signal'})
                    continue

                entry_met  = False
                entry_idx  = None
                for idx, row in df_after.iterrows():
                    if direction == 'BUY-LONG' and row['high'] >= entry:
                        entry_met = True; entry_idx = idx; break
                    elif direction != 'BUY-LONG' and row['low'] <= entry:
                        entry_met = True; entry_idx = idx; break

                if not entry_met:
                    current_price = float(df_after.iloc[-1]['close'])
                    results.append({'_id': sig.get('_id'), 'status': 'pending',
                                     'exit_price': None, 'current_price': current_price,
                                     'live_pnl_pct': 0, 'track_status': 'entry_not_met'})
                    continue

                entry_pos     = df_after.index.get_loc(entry_idx)
                df_post_entry = df_after.iloc[entry_pos:].reset_index(drop=True)
                status_val    = 'open'
                exit_price    = None
                current_price = float(df_post_entry.iloc[-1]['close'])

                for idx, row in df_post_entry.iterrows():
                    if direction == 'BUY-LONG':
                        if row['high'] >= t2:  status_val = 'target_hit'; exit_price = t2; break
                        elif row['low'] <= sl: status_val = 'stop_hit';   exit_price = sl; break
                    else:
                        if row['low'] <= t2:   status_val = 'target_hit'; exit_price = t2; break
                        elif row['high'] >= sl:status_val = 'stop_hit';   exit_price = sl; break

                pnl_pct = round((current_price - entry) / entry * 100, 2) if direction == 'BUY-LONG' \
                     else round((entry - current_price) / entry * 100, 2)

                results.append({
                    '_id':          sig.get('_id'),
                    'status':       status_val,
                    'exit_price':   exit_price,
                    'current_price':current_price,
                    'live_pnl_pct': pnl_pct,
                    'track_status': 'tracked'
                })

            except Exception as e:
                results.append({'_id': sig.get('_id'), 'status': 'pending',
                                 'exit_price': None, 'current_price': None,
                                 'live_pnl_pct': 0, 'track_status': f'error:{str(e)}'})
                continue

        return jsonify({'status': 'success', 'results': results})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
