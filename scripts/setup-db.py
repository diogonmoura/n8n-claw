#!/usr/bin/env python3
"""
Fix n8n SQLite database after n8n first run:
  1. Insert the public-api key into user_api_keys
  2. Import workflow JSONs with placeholders filled
  3. Fix credential IDs in workflow_entity AND workflow_history
  4. Fix workflow cross-references (MCP Builder ID, etc.)
  5. Activate all workflows

Usage: python3 setup-db.py --env-file ../.env [--n8n-folder /.n8n] [--workflows-dir ../workflows]
"""
import argparse, json, os, re, sqlite3, subprocess, sys, tempfile, uuid


def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env


parser = argparse.ArgumentParser()
parser.add_argument('--env-file', default='.env')
parser.add_argument('--n8n-folder', default='/.n8n')
parser.add_argument('--workflows-dir', default='../workflows')
args = parser.parse_args()

env = load_env(args.env_file)
db_path = os.path.join(args.n8n_folder, 'database.sqlite')
wf_dir = os.path.abspath(args.workflows_dir)
conn = sqlite3.connect(db_path)

# ── Step 1: Insert public-api key ─────────────────────────────────────────────
print("=== Step 1: Insert public-api key ===")
api_key = subprocess.run(
    [sys.executable,
     os.path.join(os.path.dirname(os.path.abspath(__file__)), 'generate-n8n-api-key.py'),
     '--n8n-folder', args.n8n_folder],
    capture_output=True, text=True
).stdout.strip()

if not api_key.startswith('eyJ'):
    print("ERROR: Could not generate API key", file=sys.stderr)
    sys.exit(1)

user_id = conn.execute("SELECT id FROM user LIMIT 1").fetchone()[0]
scopes = json.dumps([
    "workflow:create", "workflow:read", "workflow:update", "workflow:delete",
    "workflow:list", "workflow:activate", "workflow:deactivate", "workflow:move",
])
conn.execute(
    """INSERT OR REPLACE INTO user_api_keys
       (id, userId, apiKey, label, scopes, audience, createdAt, updatedAt)
       VALUES ('public-api-key-1', ?, ?, 'Setup Public API Key', ?, 'public-api',
               STRFTIME('%Y-%m-%d %H:%M:%f','NOW'), STRFTIME('%Y-%m-%d %H:%M:%f','NOW'))""",
    [user_id, api_key, scopes]
)
conn.commit()
print(f"  API key inserted for user {user_id}")

# ── Step 2: Get credential IDs ────────────────────────────────────────────────
print("\n=== Step 2: Get credential IDs ===")
creds = {}
for row in conn.execute("SELECT id, name, type FROM credentials_entity"):
    creds[row[2]] = row[0]  # type -> id (last one wins if multiple)
    print(f"  {row[2]}: {row[0]} ({row[1]})")

SUPABASE_URL = env.get('SUPABASE_URL', 'http://localhost:8000')
N8N_URL = env.get('N8N_URL', 'https://n8n.yourdomain.com')

# ── Step 3: Import and fix workflows ─────────────────────────────────────────
print("\n=== Step 3: Import and fix workflows ===")
wf_files = [
    'mcp-client.json',
    'mcp-weather.json',
    'mcp-builder.json',
    'reminder-factory.json',
    'workflow-builder.json',
    'n8n-claw-agent.json',
]

new_ids = {}
for filename in wf_files:
    path = os.path.join(wf_dir, filename)
    if not os.path.exists(path):
        print(f"  WARN: {path} not found, skipping")
        continue
    with open(path) as f:
        wf = json.load(f)

    # Replace credential IDs from live DB
    for node in wf.get('nodes', []):
        for ctype, cval in node.get('credentials', {}).items():
            if isinstance(cval, dict) and cval.get('id') == 'REPLACE_WITH_YOUR_CREDENTIAL_ID':
                real_id = creds.get(ctype)
                if real_id:
                    cval['id'] = real_id

    # String-level placeholder substitution
    wf_str = json.dumps(wf)
    wf_str = wf_str.replace('{{SUPABASE_URL}}', SUPABASE_URL)
    wf_str = wf_str.replace('{{N8N_URL}}', N8N_URL)
    wf_str = wf_str.replace('{{N8N_PUBLIC_API_KEY}}', api_key)
    wf_str = wf_str.replace('{{SUPABASE_SERVICE_KEY}}', env.get('SUPABASE_SERVICE_KEY', ''))
    wf_str = wf_str.replace('{{SUPABASE_ANON_KEY}}', env.get('SUPABASE_ANON_KEY', ''))
    wf = json.loads(wf_str)

    # Import via n8n CLI
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
        json.dump(wf, tmp)
        tmp_path = tmp.name

    result = subprocess.run(
        ['n8n', 'import:workflow', f'--input={tmp_path}'],
        capture_output=True, text=True,
        env={**os.environ, 'N8N_USER_FOLDER': '/'},
    )
    os.unlink(tmp_path)

    wf_name = wf['name']
    row = conn.execute(
        "SELECT id FROM workflow_entity WHERE name=? ORDER BY createdAt DESC LIMIT 1",
        [wf_name]
    ).fetchone()
    if row:
        new_ids[filename] = row[0]
        print(f"  Imported {wf_name} -> {row[0]}")
    else:
        print(f"  WARN: could not find {wf_name} in DB after import")

# ── Step 4: Fix cross-workflow IDs ────────────────────────────────────────────
print("\n=== Step 4: Fix cross-workflow IDs ===")
id_map = {
    'REPLACE_MCP_BUILDER_ID':      new_ids.get('mcp-builder.json',      'REPLACE_MCP_BUILDER_ID'),
    'REPLACE_REMINDER_FACTORY_ID': new_ids.get('reminder-factory.json', 'REPLACE_REMINDER_FACTORY_ID'),
    'REPLACE_WORKFLOW_BUILDER_ID': new_ids.get('workflow-builder.json', 'REPLACE_WORKFLOW_BUILDER_ID'),
}

for (wf_id,) in conn.execute("SELECT id FROM workflow_entity").fetchall():
    row = conn.execute("SELECT nodes FROM workflow_entity WHERE id=?", [wf_id]).fetchone()
    if not row or not row[0]:
        continue
    fixed = row[0]
    changed = False
    for placeholder, real_id in id_map.items():
        if placeholder in fixed:
            fixed = fixed.replace(placeholder, real_id)
            changed = True
    if changed:
        conn.execute("UPDATE workflow_entity SET nodes=? WHERE id=?", [fixed, wf_id])
        hist = conn.execute(
            "SELECT nodes FROM workflow_history WHERE workflowId=?", [wf_id]
        ).fetchone()
        if hist and hist[0]:
            fixed_h = hist[0]
            for placeholder, real_id in id_map.items():
                fixed_h = fixed_h.replace(placeholder, real_id)
            conn.execute(
                "UPDATE workflow_history SET nodes=? WHERE workflowId=?", [fixed_h, wf_id]
            )
        print(f"  Fixed cross-refs in {wf_id}")

conn.commit()

# ── Step 5: Activate all n8n-claw workflows ───────────────────────────────────
print("\n=== Step 5: Activate all n8n-claw workflows ===")
for wf_id in new_ids.values():
    conn.execute("UPDATE workflow_entity SET active=1 WHERE id=?", [wf_id])
    print(f"  Activated {wf_id}")
conn.commit()
conn.close()

print("\nDatabase setup complete!")
print("\nNEXT STEPS (manual, in n8n UI):")
print("  1. Open n8n UI and create credentials:")
print("     - Telegram API       -> name: 'n8n-claw Bot', token from BotFather")
print("     - PostgreSQL         -> host: 127.0.0.1, port: 5432, db: postgres,")
print("                            user: postgres, password from .env")
print("     - Anthropic API      -> your API key")
print("     - HTTP Header Auth   -> name: 'Kong', header: apikey,")
print("                            value: SUPABASE_ANON_KEY from .env")
print("  2. Run: python3 scripts/phase2-wire-credentials.py --env-file .env")
print("  3. Toggle n8n-claw Agent OFF then ON in n8n UI (Telegram webhook sync)")
