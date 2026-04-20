# BEFORE:
today_df = df[df['date'] == today]
if len(today_df) == 0:
    print(f"⚠ No candles for today {today}, using tail(20)")
    today_df = df.tail(20)

# AFTER:
if len(df) >= 50:
    scan_df = df.tail(50).copy()
    print(f"✓ Scanning last 50 candles for {symbol} (was: today-only)")
else:
    scan_df = df.copy()
    print(f"⚠ Only {len(df)} candles available for {symbol}, using all")