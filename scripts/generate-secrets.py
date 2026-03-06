#!/usr/bin/env python3
"""
Generate Supabase secrets for n8n-claw.
Usage: python3 generate-secrets.py [--update-env]
"""
import hmac, hashlib, base64, json, secrets, os, sys, time

def generate_jwt(payload, secret):
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

POSTGRES_PASSWORD = secrets.token_urlsafe(16)
JWT_SECRET = base64.b64encode(secrets.token_bytes(32)).decode()
now = int(time.time())

ANON_KEY = generate_jwt(
    {"role": "anon", "iss": "supabase", "iat": now, "exp": now + 87600 * 3600},
    JWT_SECRET
)
SERVICE_KEY = generate_jwt(
    {"role": "service_role", "iss": "supabase", "iat": now, "exp": now + 87600 * 3600},
    JWT_SECRET
)

print(f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}")
print(f"SUPABASE_JWT_SECRET={JWT_SECRET}")
print(f"SUPABASE_ANON_KEY={ANON_KEY}")
print(f"SUPABASE_SERVICE_KEY={SERVICE_KEY}")

if '--update-env' in sys.argv:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
    if os.path.exists(env_path):
        import re
        with open(env_path, 'r') as f:
            content = f.read()
        for key, val in [
            ('POSTGRES_PASSWORD', POSTGRES_PASSWORD),
            ('SUPABASE_JWT_SECRET', JWT_SECRET),
            ('SUPABASE_ANON_KEY', ANON_KEY),
            ('SUPABASE_SERVICE_KEY', SERVICE_KEY),
        ]:
            content = re.sub(rf'^{key}=.*$', f'{key}={val}', content, flags=re.MULTILINE)
        with open(env_path, 'w') as f:
            f.write(content)
        print(f"\nUpdated {env_path}", file=sys.stderr)
    else:
        print(f"ERROR: {env_path} not found", file=sys.stderr)
        sys.exit(1)
