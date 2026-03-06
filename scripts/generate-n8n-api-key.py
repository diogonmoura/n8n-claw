#!/usr/bin/env python3
"""
Generate a valid n8n public-api JWT key after n8n has started.
Reads the encryption key from /.n8n/config (or N8N_USER_FOLDER/.n8n/config).
Usage: python3 generate-n8n-api-key.py [--n8n-folder /path/to/.n8n]
"""
import base64, hashlib, hmac, json, os, subprocess, sys, time, uuid


def get_jwt_secret(encryption_key):
    base_key = ''.join(encryption_key[i] for i in range(0, len(encryption_key), 2))
    return hashlib.sha256(base_key.encode()).hexdigest()


def sign_jwt(payload, secret):
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(',', ':')).encode()
    ).rstrip(b'=').decode()
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()
    ).rstrip(b'=').decode()
    msg = f"{header}.{body}"
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).rstrip(b'=').decode()
    return f"{msg}.{sig}"


n8n_folder = '/.n8n'
for i, arg in enumerate(sys.argv):
    if arg == '--n8n-folder' and i + 1 < len(sys.argv):
        n8n_folder = sys.argv[i + 1]

config_path = os.path.join(n8n_folder, 'config')
if not os.path.exists(config_path):
    print(f"ERROR: {config_path} not found. Has n8n started at least once?", file=sys.stderr)
    sys.exit(1)

with open(config_path) as f:
    config = json.load(f)

encryption_key = config['encryptionKey']
jwt_secret = get_jwt_secret(encryption_key)

# Get user ID from SQLite
db_path = os.path.join(n8n_folder, 'database.sqlite')
result = subprocess.run(
    ['sqlite3', db_path, "SELECT id FROM user LIMIT 1"],
    capture_output=True, text=True
)
user_id = result.stdout.strip()
if not user_id:
    print("ERROR: Could not get user ID from n8n database.", file=sys.stderr)
    sys.exit(1)

payload = {
    "sub": user_id,
    "iss": "n8n",
    "aud": "public-api",
    "jti": str(uuid.uuid4()),
    "iat": int(time.time()),
}
api_key = sign_jwt(payload, jwt_secret)

print(api_key)
