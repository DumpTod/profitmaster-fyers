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

# ========================================
# FYERS CREDENTIALS (Your Fresh Credentials)
# ========================================
FYERS_APP_ID     = os.environ.get('API_KEY', 'B64YVF96PK-100')
FYERS_SECRET_KEY = os.environ.get('API_SECRET', 'QLMGPDNWC7')
FYERS_REDIRECT_URL = 'https://trade.fyers.in/api-login/redirect-uri/index.html'

# ========================================
# SCANNER CONFIGURATION (Exact Copy from Original)
# ========================================
SCANNER_CONFIG = {
    'NIFTY50': {
        'instrument_key': 'NSE:NIFTY50-INDEX',
        'option_key': 'NSE:NIFTY50-INDEX',
        'resample_minutes': 5,
        'fast_period': 5,
        'fast_mult': 1.5,
        'slow_period': 25,
        'slow_mult': 4.0,
        'lot_size': 65,
        'strike_step': 50
    },
    'BANKNIFTY': {
        'instrument_key': 'NSE:NIFTYBANK-INDEX',
        'option_key': 'NSE:NIFTYBANK-INDEX',
        'resample_minutes': 5,
        'fast_period': 5,
        'fast_mult': 1.5,
        'slow_period': 20,
        'slow_mult': 4.0,
        'lot_size': 30,
        'strike_step': 100
    }
}

# ========================================
# GLOBAL VARIABLES
# ========================================
IST = pytz.timezone('Asia/Kolkata')
TOKEN_FILE = '/tmp/token.json'
REFRESH_FILE = '/tmp/refresh_token.txt'

token_data = {'access_token': None, 'token_time': None, 'refresh_token': None}
scan_cache = {'signals': [], 'last_scan': None}
options_cache = {'signals': [], 'last_fetch': None}

# 🔥 FIX #1: ADD GLOBAL FYERS CLIENT
fyers_client = None

# ========================================
# TOKEN MANAGEMENT (Fixed & Robust)
# ========================================

def save_token(access_token, refresh_token=None):
    """Save token to memory AND file"""
    global token_data
    token_data['access_token'] = access_token
    token_data['token_time'] = datetime.now(IST).isoformat()
    
    if refresh_token:
        token_data['refresh_token'] = refresh_token
        try:
            with open(REFRESH_FILE, 'w') as f:
                f.write(refresh_token)
            print(f"✓ Refresh token saved at {datetime.now(IST).strftime('%H:%M:%S IST')}")
        except Exception as e:
            print(f"✗ Failed to save refresh token: {e}")
    
    try:
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f)
        print(f"✓ Access token saved at {datetime.now(IST).strftime('%H:%M:%S IST')}")
    except Exception as e:
        print(f"✗ Failed to save access token: {e}")


def load_token():
    """Load tokens from file on startup"""
    global token_data
    try:
        with open(TOKEN_FILE, 'r') as f:
            data = json.load(f)
            token_data['access_token'] = data.get('access_token')
            token_data['token_time'] = data.get('token_time')
            token_data['refresh_token'] = data.get('refresh_token')
        print(f"✓ Token loaded from file")
    except Exception as e:
        print(f"⚠ No token file found - requires login")
    
    # Also try loading refresh token separately
    if not token_data['refresh_token']:
        try:
            with open(REFRESH_FILE, 'r') as f:
                token_data['refresh_token'] = f.read().strip()
            print(f"✓ Refresh token loaded from file")
        except:
            pass


def auto_refresh_access_token():
    """Auto-refresh using refresh token + PIN"""
    global token_data, fyers_client
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
                'pin': os.environ.get('FYERS_PIN', '')
            },
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        
        if r.status_code == 200 and r.json().get('s') == 'ok':
            new_access_token = f"{FYERS_APP_ID}:{r.json()['access_token']}"
            save_token(new_access_token)
            # 🔥 FIX #2: REINITIALIZE CLIENT AFTER REFRESH
            init_fyers()
            return True
        
        return False
    except Exception as e:
        print(f"✗ Auto-refresh failed: {e}")
        return False


# Load tokens on startup
load_token()

# Auto-refresh on startup if needed
if not token_data['access_token'] and token_data['refresh_token']:
    print("⟳ No access token found on startup, attempting auto-refresh...")
    auto_refresh_access_token()


def init_fyers():
    """🔥 FIX #3: Initialize Fyers client and SET IT GLOBALLY"""
    global fyers_client, token_data
    
    if not token_data['access_token']:
        print("✗ init_fyers: No access token available")
        fyers_client = None
        return None
    
    try:
       def init_fyers():
    global fyers_client, token_data
    
    if not token_data['access_token']:
        print("✗ init_fyers: No access token available")
        fyers_client = None
        return None
    
    try:
        raw_token = token_data['access_token']
        if ':' in raw_token:
            raw_token = raw_token.split(':', 1)[1]
        
        fyers_client = fyersModel.FyersModel(
            client_id=FYERS_APP_ID,
            token=raw_token,
            log_path='/tmp'
        )
        print(f"✅ Fyers client initialized at {datetime.now(IST).strftime('%H:%M:%S IST')}")
        return fyers_client
    except Exception as e:
        print(f"✗ init_fyers error: {e}")
        fyers_client = None
        return None


# Initialize client on startup if token exists
if token_data['access_token']:
    init_fyers()


# ========================================
# TRADING HOLIDAYS (Updated through 2026)
# ========================================
TRADING_HOLIDAYS = {
    date(2024,1,26), date(2024,3,25), date(2024,4,14), date(2024,4,17),
    date(2024,5,1),  date(2024,6,17), date(2024,8,15), date(2024,10,2),
    date(2024,10,24),date(2024,11,1), date(2024,11,15),date(2024,12,25),
    date(2025,1,26), date(2025,2,26), date(2025,3,14), date(2025,3,31),
    date(2025,4,10), date(2025,4,14), date(2025,4,18), date(2025,5,1),
    date(2025,8,15), date(2025,10,2), date(2025,10,23),date(2025,12,25),
    date(2026,1,26), date(2026,3,3),   date(2026,3,26),  date(2026,3,31),
    date(2026,4,3),   date(2026,4,14),  date(2026,5,1),   date(2026,5,28),
    date(2026,6,26),  date(2026,9,14),  date(2026,10,2),  date(2026,10,20),
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
    expiry = last_weekday_of_month(year, month, 3)  # Thursday
    while not is_trading_day(expiry):
        expiry -= timedelta(days=1)
    return expiry

def get_active_expiry(symbol, signal_date=None):
    if signal_date is None:
        signal_date = datetime.now(IST).date()
    if isinstance(signal_date, str):
        signal_date = date.fromisoformat(signal_date[:10])
    
    y, m = signal_date.year, signal_date.month
    
    monthly_expiry = get_monthly_expiry(symbol, y, m)
    
    if signal_date <= monthly_expiry:
        return monthly_expiry
    else:
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
        return get_monthly_expiry(symbol, y, m)


def round_to_strike(price, step):
    return round(price / step) * step


# ========================================
# OPTION CHAIN FETCH (Fyers V3)
# ========================================

def get_option_chain_for_strikes(underlying_key, expiry_date, atm_strike, strikes_range, option_type='CE'):
    """
    Fetch option chain for specific strikes around ATM.
    
    Args:
        underlying_key: e.g. 'NSE:NIFTY50-INDEX' or 'NSE:NIFTYBANK-INDEX'
        expiry_date: date object
        atm_strike: int (ATM strike price)
        strikes_range: list of strike offsets, e.g. [-100, -50, 0, 50, 100]
        option_type: 'CE' or 'PE'
    
    Returns:
        DataFrame with columns: strike, ltp, bid, ask, iv, oi, volume
    """
    global fyers_client
    
    # 🔥 FIX #4: VALIDATE CLIENT BEFORE API CALL
    if not fyers_client:
        print("✗ get_option_chain: Fyers client not initialized")
        return pd.DataFrame()
    
    if 'NIFTY50' in underlying_key:
        option_symbol_base = 'NIFTY'
        exchange = 'NSE'
    elif 'NIFTYBANK' in underlying_key:
        option_symbol_base = 'BANKNIFTY'
        exchange = 'NSE'
    else:
        print(f"✗ Unknown underlying: {underlying_key}")
        return pd.DataFrame()
    
    exp_str = expiry_date.strftime('%y%b').upper()
    
    results = []
    for offset in strikes_range:
        strike = atm_strike + offset
        symbol = f"{exchange}:{option_symbol_base}{exp_str}{strike}{option_type}"
        
        try:
            data = {
                "symbol": symbol,
                "resolution": "D",
                "date_format": "1",
                "range_from": (datetime.now(IST) - timedelta(days=1)).strftime('%Y-%m-%d'),
                "range_to": datetime.now(IST).strftime('%Y-%m-%d'),
                "cont_flag": "1"
            }
            
            response = fyers_client.history(data=data)
            
            if response and response.get('s') == 'ok' and 'candles' in response:
                candles = response['candles']
                if len(candles) > 0:
                    latest = candles[-1]
                    ltp = latest[4]  # close price
                    
                    results.append({
                        'strike': strike,
                        'symbol': symbol,
                        'ltp': ltp,
                        'offset': offset
                    })
        except Exception as e:
            print(f"✗ Error fetching {symbol}: {e}")
            continue
    
    return pd.DataFrame(results)


# ========================================
# ATR CALCULATION (Identical to Colab)
# ========================================

def calculate_atr(df, period=14):
    """Calculate ATR exactly as in validated Colab"""
    df = df.copy()
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['atr'] = df['tr'].rolling(window=period, min_periods=1).mean()
    return df


# ========================================
# DATA FETCHING (Fyers V3)
# ========================================

def fetch_candles(symbol, resolution, days=30):
    """
    🔥 FIX #5: VALIDATE CLIENT + BETTER ERROR HANDLING
    Fetch historical candles from Fyers
    
    Args:
        symbol: e.g. 'NSE:NIFTY50-INDEX'
        resolution: '1', '5', '15', '30', '60', 'D'
        days: number of days to fetch
    
    Returns:
        DataFrame with columns: datetime, open, high, low, close, volume
    """
    global fyers_client
    
    # VALIDATE CLIENT
    if not fyers_client:
        print(f"✗ fetch_candles: Fyers client not initialized")
        return pd.DataFrame()
    
    try:
        now = datetime.now(IST)
        start = now - timedelta(days=days)
        
        # Convert resolution to Fyers format
        res_map = {
            '1': '1',
            '1minute': '1',
            '5': '5',
            '5minute': '5',
            '15': '15',
            '15minute': '15',
            '1D': 'D',
            'D': 'D'
        }
        
        fyers_resolution = res_map.get(resolution, resolution)
        
        data = {
            "symbol": symbol,
            "resolution": fyers_resolution,
            "date_format": "1",
            "range_from": start.strftime('%Y-%m-%d'),
            "range_to": now.strftime('%Y-%m-%d'),
            "cont_flag": "1"
        }
        
        print(f"📊 Fetching {symbol} {resolution} data...")
        response = fyers_client.history(data=data)
        
        if not response:
            print(f"✗ No response from Fyers API for {symbol}")
            return pd.DataFrame()
        
        if response.get('s') != 'ok':
            print(f"✗ Fyers API error for {symbol}: {response.get('message', 'Unknown error')}")
            return pd.DataFrame()
        
        if 'candles' not in response or not response['candles']:
            print(f"✗ No candle data for {symbol}")
            return pd.DataFrame()
        
        candles = response['candles']
        
        df = pd.DataFrame(candles, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['datetime'], unit='s', utc=True).dt.tz_convert(IST)
        
        print(f"✅ Fetched {len(df)} candles for {symbol}")
        return df
        
    except Exception as e:
        print(f"✗ fetch_candles error for {symbol}: {e}")
        return pd.DataFrame()


# ========================================
# SIGNAL GENERATION (Core Logic - Unchanged)
# ========================================

def generate_signals():
    """
    🔥 FIX #6: CHECK CLIENT STATUS BEFORE PROCESSING
    Generate ATR trailing stop signals for configured symbols
    """
    global fyers_client
    
    # CRITICAL: Validate client first
    if not fyers_client:
        print("✗ generate_signals: Fyers client not initialized - cannot fetch data")
        return []
    
    results = []
    now = datetime.now(IST)
    
    for symbol_name, config in SCANNER_CONFIG.items():
        print(f"\n{'='*60}")
        print(f"🔍 Scanning {symbol_name}")
        print(f"{'='*60}")
        
        try:
            # Fetch 1-minute data
            df_1m = fetch_candles(config['instrument_key'], '1', days=30)
            
            if len(df_1m) == 0:
                print(f"⚠️ No data for {symbol_name} - skipping")
                continue
            
            # Resample to configured timeframe
            df = df_1m.set_index('datetime').resample(f"{config['resample_minutes']}min").agg({
                'open': 'first',
                'high': 'max',
                'low': 'min',
                'close': 'last',
                'volume': 'sum'
            }).dropna().reset_index()
            
            if len(df) < max(config['fast_period'], config['slow_period']) + 20:
                print(f"⚠️ Insufficient data for {symbol_name}")
                continue
            
            # Calculate ATR
            df = calculate_atr(df, period=14)
            
            # Fast ATR Trailing Stop
            df['fast_atr_mult'] = df['atr'] * config['fast_mult']
            df['fast_long_stop'] = df['close'] - df['fast_atr_mult']
            df['fast_short_stop'] = df['close'] + df['fast_atr_mult']
            
            df['fast_trend'] = 0
            for i in range(1, len(df)):
                if df.loc[i, 'close'] > df.loc[i-1, 'fast_short_stop']:
                    df.loc[i, 'fast_trend'] = 1
                    df.loc[i, 'fast_long_stop'] = max(df.loc[i, 'fast_long_stop'], df.loc[i-1, 'fast_long_stop']) if df.loc[i-1, 'fast_trend'] == 1 else df.loc[i, 'fast_long_stop']
                elif df.loc[i, 'close'] < df.loc[i-1, 'fast_long_stop']:
                    df.loc[i, 'fast_trend'] = -1
                    df.loc[i, 'fast_short_stop'] = min(df.loc[i, 'fast_short_stop'], df.loc[i-1, 'fast_short_stop']) if df.loc[i-1, 'fast_trend'] == -1 else df.loc[i, 'fast_short_stop']
                else:
                    df.loc[i, 'fast_trend'] = df.loc[i-1, 'fast_trend']
                    if df.loc[i, 'fast_trend'] == 1:
                        df.loc[i, 'fast_long_stop'] = max(df.loc[i, 'fast_long_stop'], df.loc[i-1, 'fast_long_stop'])
                    else:
                        df.loc[i, 'fast_short_stop'] = min(df.loc[i, 'fast_short_stop'], df.loc[i-1, 'fast_short_stop'])
            
            # Slow ATR Trailing Stop
            df['slow_atr_mult'] = df['atr'] * config['slow_mult']
            df['slow_long_stop'] = df['close'] - df['slow_atr_mult']
            df['slow_short_stop'] = df['close'] + df['slow_atr_mult']
            
            df['slow_trend'] = 0
            for i in range(1, len(df)):
                if df.loc[i, 'close'] > df.loc[i-1, 'slow_short_stop']:
                    df.loc[i, 'slow_trend'] = 1
                    df.loc[i, 'slow_long_stop'] = max(df.loc[i, 'slow_long_stop'], df.loc[i-1, 'slow_long_stop']) if df.loc[i-1, 'slow_trend'] == 1 else df.loc[i, 'slow_long_stop']
                elif df.loc[i, 'close'] < df.loc[i-1, 'slow_long_stop']:
                    df.loc[i, 'slow_trend'] = -1
                    df.loc[i, 'slow_short_stop'] = min(df.loc[i, 'slow_short_stop'], df.loc[i-1, 'slow_short_stop']) if df.loc[i-1, 'slow_trend'] == -1 else df.loc[i, 'slow_short_stop']
                else:
                    df.loc[i, 'slow_trend'] = df.loc[i-1, 'slow_trend']
                    if df.loc[i, 'slow_trend'] == 1:
                        df.loc[i, 'slow_long_stop'] = max(df.loc[i, 'slow_long_stop'], df.loc[i-1, 'slow_long_stop'])
                    else:
                        df.loc[i, 'slow_short_stop'] = min(df.loc[i, 'slow_short_stop'], df.loc[i-1, 'slow_short_stop'])
            
            # Generate signals
            df['signal'] = 0
            df.loc[(df['fast_trend'] == 1) & (df['fast_trend'].shift(1) == -1), 'signal'] = 1   # BUY
            df.loc[(df['fast_trend'] == -1) & (df['fast_trend'].shift(1) == 1), 'signal'] = -1  # SELL
            
            # Get latest signal
            signal_rows = df[df['signal'] != 0].tail(1)
            
            if len(signal_rows) == 0:
                print(f"⚠️ No recent signals for {symbol_name}")
                continue
            
            last_signal = signal_rows.iloc[-1]
            
            if last_signal['signal'] == 1:  # BUY
                direction = 'BUY-LONG'
                entry = round(last_signal['close'], 2)
                sl = round(last_signal['fast_long_stop'], 2)
                t1 = round(last_signal['slow_short_stop'], 2)
                t2 = round(entry + 2 * (entry - sl), 2)
                
            else:  # SELL
                direction = 'SELL-SHORT'
                entry = round(last_signal['close'], 2)
                sl = round(last_signal['fast_short_stop'], 2)
                t1 = round(last_signal['slow_long_stop'], 2)
                t2 = round(entry - 2 * (sl - entry), 2)
            
            signal_data = {
                'symbol': symbol_name,
                'direction': direction,
                'entry': entry,
                'sl': sl,
                'target_1': t1,
                'target_2': t2,
                'scan_date': last_signal['datetime'].isoformat(),
                'current_price': round(df.iloc[-1]['close'], 2),
                'atr': round(last_signal['atr'], 2),
                'timeframe': f"{config['resample_minutes']}m",
                'lot_size': config['lot_size']
            }
            
            results.append(signal_data)
            
            print(f"✅ Signal generated: {direction} @ {entry}")
            
        except Exception as e:
            print(f"✗ Error processing {symbol_name}: {e}")
            continue
    
    print(f"\n{'='*60}")
    print(f"📊 Total signals generated: {len(results)}")
    print(f"{'='*60}\n")
    
    return results


# ========================================
# OPTION SIGNALS (Based on Futures)
# ========================================

def generate_option_signals(futures_signals):
    """Generate option trading signals based on futures signals"""
    global fyers_client
    
    if not fyers_client:
        print("✗ generate_option_signals: Fyers client not initialized")
        return []
    
    option_signals = []
    
    for sig in futures_signals:
        symbol = sig.get('symbol', '')
        direction = sig.get('direction', '')
        
        config = SCANNER_CONFIG.get(symbol, {})
        if not config:
            continue
        
        try:
            expiry = get_active_expiry(symbol)
            current_price = sig.get('current_price', sig.get('entry', 0))
            atm_strike = round_to_strike(current_price, config['strike_step'])
            
            if direction == 'BUY-LONG':
                option_type = 'CE'
                strikes = [0, config['strike_step']]
            else:
                option_type = 'PE'
                strikes = [0, -config['strike_step']]
            
            chain = get_option_chain_for_strikes(
                config['option_key'],
                expiry,
                atm_strike,
                strikes,
                option_type
            )
            
            if len(chain) == 0:
                continue
            
            # ATM option
            atm_opt = chain[chain['offset'] == 0]
            if len(atm_opt) > 0:
                opt = atm_opt.iloc[0]
                option_signals.append({
                    'futures_symbol': symbol,
                    'option_symbol': opt['symbol'],
                    'strike': opt['strike'],
                    'type': option_type,
                    'position': 'ATM',
                    'entry': round(opt['ltp'], 2),
                    'direction': direction,
                    'expiry': expiry.isoformat(),
                    'lot_size': config['lot_size']
                })
            
            # OTM option
            otm_opt = chain[chain['offset'] == strikes[1]]
            if len(otm_opt) > 0:
                opt = otm_opt.iloc[0]
                option_signals.append({
                    'futures_symbol': symbol,
                    'option_symbol': opt['symbol'],
                    'strike': opt['strike'],
                    'type': option_type,
                    'position': 'OTM',
                    'entry': round(opt['ltp'], 2),
                    'direction': direction,
                    'expiry': expiry.isoformat(),
                    'lot_size': config['lot_size']
                })
                
        except Exception as e:
            print(f"✗ Error generating option signals for {symbol}: {e}")
            continue
    
    return option_signals


# ========================================
# SCANNER STATUS HELPER
# ========================================

def get_scanner_status():
    """Get current scanner status"""
    if not token_data['access_token']:
        return 'NO_TOKEN'
    
    now = datetime.now(IST)
    current_time = now.time()
    
    if now.weekday() >= 5 or now.date() in TRADING_HOLIDAYS:
        return 'MARKET_CLOSED'
    
    if current_time < datetime.strptime('09:15', '%H:%M').time():
        return 'PRE_MARKET'
    elif current_time > datetime.strptime('15:30', '%H:%M').time():
        return 'POST_MARKET'
    else:
        return 'ACTIVE'


# ========================================
# AUTH ROUTES (OAuth Flow)
# ========================================

@app.route('/refresh')
def refresh_route():
    """Start Fyers OAuth flow"""
    session = fyersModel.SessionModel(
        client_id=FYERS_APP_ID,
        redirect_uri=FYERS_REDIRECT_URL,
        response_type='code',
        state='sample',
        secret_key=FYERS_SECRET_KEY,
        grant_type='authorization_code'
    )
    
    auth_url = session.generate_authcode()
    print(f"🔑 Generated auth URL: {auth_url}")
    return redirect(auth_url)


@app.route('/callback')
def callback_route():
    """
    🔥 FIX #7: CRITICAL FIX - Reinitialize client after getting access token
    Handle OAuth callback and exchange auth code for access token
    """
    global token_data, fyers_client
    
    auth_code = request.args.get('auth_code')
    
    if not auth_code:
        return jsonify({'status': 'error', 'message': 'No auth code in callback'}), 400
    
    try:
        session = fyersModel.SessionModel(
            client_id=FYERS_APP_ID,
            redirect_uri=FYERS_REDIRECT_URL,
            response_type='code',
            state='sample',
            secret_key=FYERS_SECRET_KEY,
            grant_type='authorization_code'
        )
        
        session.set_token(auth_code)
        response = session.generate_token()
        
        if not response or response.get('s') != 'ok':
            error_msg = response.get('message', 'Unknown error') if response else 'No response'
            return jsonify({'status': 'error', 'message': f'Token generation failed: {error_msg}'}), 400
        
        access_token = response['access_token']
        refresh_token = response.get('refresh_token', '')
        
        # Format access token correctly
        full_access_token = f"{FYERS_APP_ID}:{access_token}"
        
        # Save tokens
        save_token(full_access_token, refresh_token)
        
        # 🔥 CRITICAL FIX: REINITIALIZE CLIENT WITH NEW TOKEN
        init_fyers()
        
        print(f"✅ Access token obtained and client reinitialized at {datetime.now(IST).strftime('%H:%M:%S IST')}")
        
        return f'''
        <html><head><title>Auth Success</title></head>
        <body style="font-family:monospace;background:#0a0a0a;color:#22c55e;padding:40px;text-align:center;">
        <h1>✅ Authentication Successful!</h1>
        <p>Access Token: {full_access_token[:20]}...</p>
        <p>Client Status: {'✅ Initialized' if fyers_client else '❌ Failed'}</p>
        <p><a href="/api/signals" style="color:#22c55e;text-decoration:none;padding:12px 24px;background:#166534;border-radius:6px;display:inline-block;margin-top:20px;">📊 View Signals</a></p>
        <p><a href="/" style="color:#3b82f6;text-decoration:none;padding:12px 24px;background:#1e3a8a;border-radius:6px;display:inline-block;margin-top:10px;">🏠 Home</a></p>
        </body></html>
        '''
        
    except Exception as e:
        print(f"✗ Callback error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/set-token', methods=['GET', 'POST'])
def set_token_route():
    """Manual token entry"""
    global token_data, fyers_client
    
    if request.method == 'POST':
        access_token = request.form.get('access_token', '').strip()
        refresh_token = request.form.get('refresh_token', '').strip()
        
        if not access_token:
            return jsonify({'status': 'error', 'message': 'Access token required'}), 400
        
        # Format token if needed
        if ':' not in access_token:
            access_token = f"{FYERS_APP_ID}:{access_token}"
        
        save_token(access_token, refresh_token if refresh_token else None)
        
        # 🔥 CRITICAL FIX: REINITIALIZE CLIENT WITH NEW TOKEN
        init_fyers()
        
        return redirect('/')
    
    # GET request - show form
    return f'''
    <html><head><title>Set Token</title></head>
    <body style="font-family:monospace;background:#0a0a0a;color:#a3a3a3;padding:40px;">
    <h2 style="color:#22c55e;">🔑 Manual Token Entry</h2>
    <form method="POST">
        <p><label>Access Token:<br><input type="text" name="access_token" style="width:500px;padding:8px;background:#171717;color:#fff;border:1px solid #404040;border-radius:4px;" required></label></p>
        <p><label>Refresh Token (Optional):<br><input type="text" name="refresh_token" style="width:500px;padding:8px;background:#171717;color:#fff;border:1px solid #404040;border-radius:4px;"></label></p>
        <p><button type="submit" style="padding:10px 24px;background:#166534;color:#22c55e;border:none;border-radius:6px;cursor:pointer;">💾 Save Token</button></p>
    </form>
    <p><a href="/" style="color:#3b82f6;text-decoration:none;">← Back</a></p>
    </body></html>
    '''


@app.route('/debug-fyers')
def debug_fyers():
    """Debug endpoint to check Fyers connection"""
    global fyers_client, token_data
    
    debug_info = {
        'token_set': token_data['access_token'] is not None,
        'token_preview': token_data['access_token'][:30] + '...' if token_data['access_token'] else None,
        'token_time': token_data.get('token_time'),
        'refresh_token_set': token_data.get('refresh_token') is not None,
        'client_initialized': fyers_client is not None,
        'server_time_ist': datetime.now(IST).isoformat()
    }
    
    # Try a test API call
    if fyers_client:
        try:
            test_data = {
                "symbol": "NSE:NIFTY50-INDEX",
                "resolution": "D",
                "date_format": "1",
                "range_from": (datetime.now(IST) - timedelta(days=2)).strftime('%Y-%m-%d'),
                "range_to": datetime.now(IST).strftime('%Y-%m-%d'),
                "cont_flag": "1"
            }
            
            response = fyers_client.history(data=test_data)
            
            debug_info['test_api_call'] = {
                'success': response.get('s') == 'ok' if response else False,
                'response_status': response.get('s') if response else 'No response',
                'candles_count': len(response.get('candles', [])) if response else 0,
                'error': response.get('message') if response and response.get('s') != 'ok' else None
            }
        except Exception as e:
            debug_info['test_api_call'] = {'error': str(e)}
    
    return jsonify(debug_info)


# ========================================
# ROUTES
# ========================================

@app.route('/')
def home():
    """Home page with links"""
    status = get_scanner_status()
    client_status = '✅ Active' if fyers_client else '❌ Not Initialized'
    
    return f'''
    <html><head><title>ProfitMaster Fyers Scanner</title></head>
    <body style="font-family:monospace;background:#0a0a0a;color:#a3a3a3;padding:40px;text-align:center;">
    <h1 style="color:#22c55e;font-size:32px;margin-bottom:10px;">🚀 PROFITMASTER FYERS SCANNER</h1>
    <p style="color:#666;margin-bottom:30px;">ATR Trailing Stop Strategy - Live</p>
    <div style="background:#171717;padding:20px;border-radius:8px;border:1px solid #262626;max-width:600px;margin:20px auto;">
    <p><strong>Scanner Status:</strong> <span style="color:{'#22c55e' if status == 'ACTIVE' else '#eab308' if status == 'PRE_MARKET' else '#ef4444'};">{status}</span></p>
    <p><strong>Fyers Client:</strong> <span style="color:{'#22c55e' if fyers_client else '#ef4444'};">{client_status}</span></p>
    <p><strong>Server Time:</strong> {datetime.now(IST).strftime('%d %b %Y %H:%M:%S IST')}</p>
    </div>
    <p><a href="/refresh" style="color:#22c55e;text-decoration:none;padding:10px 24px;background:#166534;border-radius:6px;display:inline-block;margin:5px">🔑 Login via Fyers</a></p>
    <p><a href="/set-token" style="color:#3b82f6;text-decoration:none;padding:10px 24px;background:#1e3a8a;border-radius:6px;display:inline-block;margin:5px;">🔑 Set Token Manually</a></p>
    <p><a href="/api/signals" style="color:#22d3ee;text-decoration:none;padding:10px 24px;background:#164e63;border-radius:6px;display:inline-block;margin:5px;">📊 Get Signals</a></p>
    <p><a href="/debug-fyers" style="color:#a78bfa;text-decoration:none;padding:10px 24px;background:#4c1d95;border-radius:6px;display:inline-block;margin:5px;">🔍 Debug</a></p>
    </body></html>
    '''


@app.route('/api/status')
def api_status():
    return jsonify({
        'status': 'success',
        'scanner_status': get_scanner_status(),
        'server_time_ist': datetime.now(IST).isoformat(),
        'token_set': token_data['access_token'] is not None,
        'client_initialized': fyers_client is not None,
        'token_time': token_data.get('token_time'),
        'scanner_model': 'ATR Trailing Stop (Walk-Forward Validated)',
        'config': {
            sym: {
                'timeframe': f"{cfg['resample_minutes']}m",
                'fast': f"({cfg['fast_period']}, {cfg['fast_mult']})",
                'slow': f"({cfg['slow_period']}, {cfg['slow_mult']})",
                'strike_step': cfg['strike_step']
            } for sym, cfg in SCANNER_CONFIG.items()
        }
    })


@app.route('/api/signals')
def api_signals():
    """
    🔥 FIX #8: Better error handling for signals endpoint
    """
    now = datetime.now(IST)
    status = get_scanner_status()
    
    if status == 'NO_TOKEN':
        return jsonify({
            'status': 'error',
            'scanner_status': 'NO_TOKEN',
            'message': 'Please login via /refresh or set token manually',
            'signals': [],
            'timestamp': now.isoformat()
        })
    
    if not fyers_client:
        return jsonify({
            'status': 'error',
            'scanner_status': status,
            'message': 'Fyers client not initialized - please re-login',
            'signals': [],
            'timestamp': now.isoformat()
        })
    
    if scan_cache['last_scan'] and (now - scan_cache['last_scan']).total_seconds() < 60:
        return jsonify({
            'status': 'success',
            'scanner_status': status,
            'signals': scan_cache['signals'],
            'cached': True,
            'timestamp': now.isoformat()
        })
    
    if status in ['ACTIVE', 'PRE_MARKET']:
        signals = generate_signals()
    else:
        signals = scan_cache.get('signals', [])
    
    scan_cache['signals'] = signals
    scan_cache['last_scan'] = now
    
    return jsonify({
        'status': 'success',
        'scanner_status': status,
        'signals': signals,
        'cached': False,
        'timestamp': now.isoformat()
    })


@app.route('/api/option-signals')
def api_option_signals():
    now = datetime.now(IST)
    
    if not fyers_client:
        return jsonify({
            'status': 'error',
            'message': 'Fyers client not initialized',
            'option_signals': [],
            'timestamp': now.isoformat()
        })
    
    if options_cache['last_fetch'] and (now - options_cache['last_fetch']).total_seconds() < 120:
        return jsonify({
            'status': 'success',
            'option_signals': options_cache['signals'],
            'cached': True,
            'timestamp': now.isoformat()
        })
    
    futures = scan_cache.get('signals', [])
    opt_signals = generate_option_signals(futures)
    
    options_cache['signals'] = opt_signals
    options_cache['last_fetch'] = now
    
    return jsonify({
        'status': 'success',
        'option_signals': opt_signals,
        'cached': False,
        'timestamp': now.isoformat()
    })


@app.route('/api/track', methods=['POST'])
def api_track():
    """Track signal performance"""
    if not fyers_client:
        return jsonify({'status': 'error', 'message': 'Fyers client not initialized'})
    
    try:
        data = request.json
        if not data or 'signals' not in data:
            return jsonify({'status': 'error', 'message': 'No signals'})
        
        results = []
        
        for sig in data['signals']:
            symbol = sig.get('symbol', '')
            config = SCANNER_CONFIG.get(symbol, '')
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
                    current_price = float(df_after.iloc[-1]['close'])
                    results.append({'_id': sig.get('_id'), 'status': 'pending', 'current_price': current_price, 'live_pnl_pct': 0, 'track_status': 'entry_not_met'})
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
                
                results.append({
                    '_id': sig.get('_id'),
                    'status': trade_status,
                    'exit_price': exit_price,
                    'current_price': current_price,
                    'live_pnl_pct': pnl_pct,
                    'track_status': 'tracked'
                })
                
            except Exception as e:
                results.append({'_id': sig.get('_id'), 'status': 'pending', 'track_status': f'error:{str(e)}'})
        
        return jsonify({'status': 'success', 'results': results})
    
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


# ========================================
# STARTUP BLOCK
# ========================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    print(f"\n{'='*70}")
    print(f"🚀 PROFITMASTER FYERS SCANNER STARTING")
    print(f"{'='*70}")
    print(f"Port: {port}")
    print(f"Token: {'✅ Active' if token_data['access_token'] else '🔴 Not Set'}")
    print(f"Refresh Token: {'✅ Available' if token_data.get('refresh_token') else '🔴 Not Set'}")
    print(f"Fyers Client: {'✅ Initialized' if fyers_client else '🔴 Not Initialized'}")
    print(f"Server Time: {datetime.now(IST).strftime('%d %b %Y %H:%M:%S IST')}")
    print(f"{'='*70}\n")
    
    # Start keep-alive thread (every 14 minutes)
    import threading
    import time
    
    def keep_alive_ping():
        while True:
            try:
                req.get(f"http://localhost:{os.environ.get('PORT', 5000)}/api/status", timeout=10)
                print(f"🔄 Keep-alive ping sent at {datetime.now(IST).strftime('%H:%M:%S IST')}")
            except:
                pass
            time.sleep(840)  # 14 minutes
    
    keep_alive_thread = threading.Thread(target=keep_alive_ping, daemon=True)
    keep_alive_thread.start()
    print("✅ Keep-alive pinger started (every 14 minutes)")

    print("\n🚀 Starting Flask server...")
    app.run(host='0.0.0.0', port=port, debug=False)
