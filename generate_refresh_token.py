import hashlib
from fyers_apiv3 import fyersModel

# Replace with your actual credentials
FYERS_APP_ID = "VS55VDHYCW-100"
FYERS_SECRET_KEY = "724FOKKSFS"
FYERS_REDIRECT_URL = "https://trade.fyers.in/api-login/redirect-uri/index.html"
FYERS_PIN = "2504"

def generate_app_id_hash():
    return hashlib.sha256(f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()

# Step 1: Generate auth code URL
fyers = fyersModel.FyersModel(
    client_id=FYERS_APP_ID,
    secret_key=FYERS_SECRET_KEY,
    redirect_uri=FYERS_REDIRECT_URL,
    state="sample_state",
    grant_type="authorization_code",
    response_type="code"
)

auth_url = fyers.generate_authcode()
print("Visit this URL and login:")
print(auth_url)

# Step 2: After login, you'll get a code in the redirect URL
# Extract the auth_code from the redirected URL
auth_code = input("Enter the auth code from URL: ")

# Step 3: Generate access token and refresh token
fyers = fyersModel.FyersModel(
    client_id=FYERS_APP_ID,
    secret_key=FYERS_SECRET_KEY,
    redirect_uri=FYERS_REDIRECT_URL,
    state="sample_state",
    grant_type="authorization_code",
    response_type="code",
    auth_code=auth_code,
    pin=FYERS_PIN
)

response = fyers.generate_authcode()
print("Response:", response)

if 'refresh_token' in response:
    with open('fyers_refresh_token.txt', 'w') as f:
        f.write(response['refresh_token'])
    print("Refresh token saved to fyers_refresh_token.txt")
