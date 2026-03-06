# n8n-claw Deployment Log

## Objective
Deploy the [n8n-claw](https://github.com/freddy-schuetz/n8n-claw) self-hosted AI agent stack onto an existing n8n Proxmox LXC server. n8n was already installed as a native systemd service; only the supplementary Supabase stack (PostgreSQL, PostgREST, Kong, Supabase Studio) and the n8n workflows needed to be deployed.

**Target server:** Proxmox LXC — Debian 13 (trixie) — `YOUR_SERVER_IP`
**n8n:** native systemd service, SQLite DB at `/.n8n/database.sqlite`, env at `/opt/n8n.env`
**n8n public URL:** `https://n8n.yourdomain.com` (behind Cloudflare proxy)

---

## What Was Done

### 1. SSH Hardening
- Set root password via Proxmox console
- Installed `openssh-server`, enabled password auth temporarily
- Deployed SSH public key via `ssh-copy-id`
- Hardened: `PasswordAuthentication no`, `PermitRootLogin prohibit-password` in `/etc/ssh/sshd_config`

### 2. Docker & Git Installation
- OS is Debian 13 (trixie) — used Docker CE 29.3 + Docker Compose v5
- Cloned repo to `/opt/n8n-claw`

### 3. Supabase Stack Deployment
- Generated secrets using Python standard library (no PyJWT needed):
  - `POSTGRES_PASSWORD`, `SUPABASE_JWT_SECRET`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`
- Created `/opt/n8n-claw/.env` with generated secrets
- Generated `supabase/kong.deployed.yml` replacing `{{SUPABASE_ANON_KEY}}` and `{{SUPABASE_SERVICE_KEY}}`
- Created `docker-compose.supabase.yml` (excludes the n8n service from the original compose file)
- Started Supabase stack: `docker compose -f docker-compose.supabase.yml up -d`
- Services running: PostgreSQL 15, PostgREST v14.5, Kong 2.8.1, Supabase Studio, Postgres Meta

### 4. Database Migrations
- Enabled `pgvector` extension: `CREATE EXTENSION IF NOT EXISTS vector;`
- Ran `001_schema.sql` with `ON_ERROR_STOP=off` (avoids error on `CREATE SCHEMA public` already existing)
- Ran `002_seed.sql` — 4 rows inserted into `soul` table
- Restarted PostgREST container to refresh schema cache

### 5. Workflow Import
- n8n's real database is at `/.n8n/database.sqlite` (not `/root/.n8n/`)
- All imports use: `N8N_USER_FOLDER=/ n8n import:workflow --input=<file>`
- Imported all 6 workflows in order: MCP Client → MCP: Weather → MCP Builder → ReminderFactory → WorkflowBuilder → n8n-claw Agent

### 6. Translation (German → English)
All workflow content was translated from German to English:
- `mcp-weather-example.json` — labels, city, language, error messages
- `workflow-builder.json` — BuildPrompt jsCode, Claude Code system prompt
- `n8n-claw-agent.json` — tool hints, error messages
- `mcp-builder.json` — LLM prompts, status messages

### 7. Placeholder Substitution
All `{{PLACEHOLDER}}` and `REPLACE_*` values were filled:

| Placeholder | Value |
|---|---|
| `{{N8N_API_KEY}}` | Real key from `user_api_keys` table |
| `{{N8N_URL}}` | `https://n8n.yourdomain.com` |
| `{{N8N_INTERNAL_URL}}` | `http://localhost:5678` |
| `{{SUPABASE_URL}}` | `http://YOUR_SERVER_IP:8000` |
| `{{SUPABASE_SERVICE_KEY}}` | From `/opt/n8n-claw/.env` |
| `REPLACE_MCP_BUILDER_ID` | `<auto-assigned by n8n after import>` |
| `REPLACE_REMINDER_FACTORY_ID` | `<auto-assigned by n8n after import>` |
| `REPLACE_WORKFLOW_BUILDER_ID` | `<auto-assigned by n8n after import>` |
| `REPLACE-WITH-BRAVE` | `no-brave-key` (no Brave API key) |

### 8. Credential Wiring
Credentials created in n8n UI and mapped to workflows:

| Credential Name | Type | Used by |
|---|---|---|
| `n8n-claw Bot` | Telegram API | n8n-claw Agent (Telegram Trigger + Reply) |
| `Postgres (Supabase)` | PostgreSQL | All DB nodes (host: `127.0.0.1:5432`) |
| `Anthropic account` | Anthropic API | Claude node |
| `HTTP Header Auth (Kong)` | HTTP Header Auth | Kong API gateway (header: `apikey`) |
| `Claude Code Runner SSH` | SSH (claudeCodeSshApi) | WorkflowBuilder Claude Code node |

> **Important:** PostgreSQL host must be `127.0.0.1` not your server IP — the container binds to localhost only.

### 9. Claude Code CLI
- Installed: `npm install -g @anthropic-ai/claude-code`
- Logged in via `claude auth login` (OAuth, Pro subscription)
- Reference cheatsheet: `/workspace/n8n-reference/CHEATSHEET.md`
- The `n8n-nodes-claude-code-cli` community node was already installed in n8n

### 10. n8n Public API Key
The original deployment used an API key with audience `mcp-server-api` (no REST API permissions).
A proper `public-api` key was generated and inserted:
- **How n8n derives the JWT secret:** takes every other character of `encryptionKey` → SHA-256 hash
- Key stored in `user_api_keys` table with `audience='public-api'` and all workflow scopes
- Required scopes: `workflow:create`, `workflow:read`, `workflow:update`, `workflow:delete`, `workflow:list`, `workflow:activate`, `workflow:deactivate`, `workflow:move`
- This key is used by: MCP Builder (Create Sub-Workflow, Create MCP Workflow, Patch & Retest, Patch Tool Schema) and the Agent's Self Modify tool

### 11. MCP Registry & Supabase Agent Config
After deployment, the Supabase tables needed manual fixes:

**`mcp_registry` table:**
- `mcp_url` had `{{N8N_URL}}` placeholder → replaced with `http://localhost:5678/mcp/<path>`
- All MCP URLs use **localhost**, not the public Cloudflare domain (Cloudflare Access blocks internal calls)

**`agents` table (`mcp_instructions` row):**
- Content was in German → translated to English
- `{{N8N_URL}}` placeholder → replaced with `http://localhost:5678`

---

## ⚠️ Critical: The Two-Table Rule

**Every time you update workflow nodes in SQLite, you MUST update BOTH tables:**

```python
# Always update BOTH — n8n reads workflow_history for the active version
conn.execute("UPDATE workflow_entity SET nodes=? WHERE id=?", [json.dumps(nodes), wf_id])
conn.execute("UPDATE workflow_history SET nodes=? WHERE workflowId=?", [json.dumps(nodes), wf_id])
```

n8n uses `workflow_entity.versionId` to look up the active version in `workflow_history`. If only `workflow_entity` is updated, n8n will still serve the old version from `workflow_history`. This affects: credentials, API keys, node parameters — anything stored in the `nodes` JSON.

---

## Common Errors & Fixes

### 1. Workflow imported to wrong database
**Error:** Workflows not appearing in n8n UI  
**Cause:** n8n's real data folder is `/.n8n/` not `/root/.n8n/`  
**Fix:** Prefix all n8n CLI commands with `N8N_USER_FOLDER=/`

### 2. pgvector not available
**Error:** `could not open extension control file ... vector.control`  
**Fix:** `CREATE EXTENSION IF NOT EXISTS vector;` before running migrations

### 3. Schema already exists
**Error:** `CREATE SCHEMA public` fails on clean Postgres  
**Fix:** Run migrations with `ON_ERROR_STOP=off` flag in psql

### 4. PostgREST returns PGRST205 (schema cache stale)
**Fix:** `docker restart n8n-claw-rest`

### 5. Postgres credential refused (ECONNREFUSED)
**Cause:** PostgreSQL container binds to `127.0.0.1:5432`, not the LXC IP  
**Fix:** Set PostgreSQL credential host to `127.0.0.1`

### 6. Disk full (99%) during npm install
**Fix:** `apt-get clean && npm cache clean --force` — freed ~1GB. Extended LXC disk 9.8GB → 30GB.

### 7. Conflicting Trigger Path on webhook activation
**Cause:** Old/deleted workflow IDs left stale entries in the `webhook_entity` table  
**Fix:**
```sql
-- Find ghost entries (workflowId not in workflow_entity)
SELECT w.workflowId, w.webhookPath
FROM webhook_entity w
LEFT JOIN workflow_entity we ON we.id = w.workflowId
WHERE we.id IS NULL;

-- Delete them
DELETE FROM webhook_entity WHERE workflowId='<ghost-id>';
```
Then restart n8n to clear in-memory state.

### 8. Credential placeholder not replaced (`REPLACE_WITH_YOUR_CREDENTIAL_ID`)
**Cause:** The workflow JSON had placeholder credential IDs in TWO places (see Two-Table Rule above)  
**Fix:**
```python
CRED_MAP = {
    'telegramApi':  '<your-telegram-cred-id>',   # n8n-claw Telegram credential
    'postgres':     '<your-postgres-cred-id>',    # Postgres (Supabase)
    'anthropicApi': '<your-anthropic-cred-id>',   # Anthropic account
}
# Fix in both workflow_entity AND workflow_history
```

### 9. n8n API returning 401/403 — wrong key audience
**Cause:** The original `mcp-server-api` key has no REST API permissions.  
**Fix:** Generate a `public-api` JWT and insert it with the correct scopes:
```python
# n8n JWT secret derivation:
baseKey = ''.join(encryptionKey[i] for i in range(0, len(encryptionKey), 2))
jwtSecret = hashlib.sha256(baseKey.encode()).hexdigest()
# Then sign: { sub: userId, iss: 'n8n', aud: 'public-api', jti: uuid }
# Insert into user_api_keys with audience='public-api' and all workflow scopes
```

### 10. n8n API 403 on POST even with public-api key
**Cause:** `scopes` column was NULL in `user_api_keys` — each endpoint checks its required scope  
**Fix:**
```python
scopes = ["workflow:create", "workflow:read", "workflow:update", "workflow:delete",
          "workflow:list", "workflow:activate", "workflow:deactivate", "workflow:move"]
conn.execute("UPDATE user_api_keys SET scopes=? WHERE id=?", [json.dumps(scopes), key_id])
```

### 11. Telegram webhook returning 403 Forbidden
**Cause:** n8n Telegram trigger (v1.1) uses a secret token for webhook validation. After direct SQLite edits + restarts, the token goes out of sync.  
**Fix:** In n8n UI, toggle the workflow **OFF** then back **ON** — forces n8n to re-register the Telegram webhook with a fresh secret token.

### 12. MCP calls blocked by Cloudflare (403)
**Cause:** MCP URLs in `mcp_registry` pointed to `https://n8n.yourdomain.com/mcp/...` — Cloudflare Access blocks unauthenticated internal calls.  
**Fix:** Always use `http://localhost:5678/mcp/<path>` for internal MCP URLs.
```sql
UPDATE mcp_registry SET mcp_url = 'http://localhost:5678/mcp/wetter' WHERE path = 'wetter';
```
Also update the MCP Builder's `Register MCP` node to write `'http://localhost:5678/mcp/' + path`.

### 13. `Self Modify` tool / MCP Builder auth errors
**Cause:** jsCode nodes had the old `mcp-server-api` key hardcoded. This affects:
- `Self Modify` tool in n8n-claw Agent (lists/manages workflows)
- `Patch & Retest` and `Patch Tool Schema` nodes in MCP Builder

**Fix:** Replace old key with new `public-api` key in **both** `workflow_entity` and `workflow_history`:
```python
fixed = nodes_str.replace(OLD_MCP_SERVER_KEY, NEW_PUBLIC_API_KEY)
conn.execute("UPDATE workflow_entity SET nodes=? WHERE id=?", [fixed, wf_id])
conn.execute("UPDATE workflow_history SET nodes=? WHERE workflowId=?", [fixed, wf_id])
```

### 14. Bot responding in German
**Cause:** `soul` table seeded with German personality text  
**Fix:** Updated all 4 rows in `soul` table to English with `auto` language detection

### 15. mcp_instructions / mcp_registry in German with placeholders
**Cause:** Supabase seed data was German and contained `{{N8N_URL}}` placeholder  
**Fix:**
```sql
UPDATE mcp_registry SET mcp_url='http://localhost:5678/mcp/wetter', server_name='Weather',
  description='Current weather via Open-Meteo' WHERE path='wetter';

UPDATE agents SET content='...(English content with localhost URLs)...'
  WHERE key='mcp_instructions';
```

---

## Architecture

```
Telegram Bot
    │
    ▼ webhook POST
https://n8n.yourdomain.com  (Cloudflare → LXC YOUR_SERVER_IP)
    │
    ▼
n8n systemd service (:5678)
    ├── 🤖 n8n-claw Agent (Telegram Trigger)
    │       ├── Load Soul / Config / Profile / History  ─→ Postgres (Supabase, :5432)
    │       ├── Claude (Anthropic API)
    │       ├── Tools: Memory Search/Save, HTTP Tool, Self Modify, Reminder,
    │       │         WorkflowBuilder, MCP Builder, MCP Client
    │       └── Telegram Reply
    │
    ├── 🏗️ MCP Builder  ─→ n8n REST API (localhost:5678) + Kong (:8000) ─→ Postgres
    ├── 🔌 MCP Client   ─→ MCP servers via localhost
    ├── ⛅ MCP: Weather  ─→ Open-Meteo API
    ├── 🔔 ReminderFactory
    └── 🔨 WorkflowBuilder  ─→ Claude Code CLI (SSH to localhost)

Supabase Stack (Docker, same host):
  Kong          :8000  (API gateway, kong.deployed.yml)
  PostgREST     :3000  (REST API over Postgres)
  PostgreSQL 15 :5432  (bound to 127.0.0.1)
  Supabase Studio :3001
  Postgres Meta :5555

Key Supabase tables:
  soul           — bot personality (4 rows)
  agents         — mcp_instructions, agent config
  mcp_registry   — registered MCP servers (mcp_url must use localhost!)
  memory_long    — long-term memory (vector search)
  user_profile   — user preferences
  conversation_history — chat history
```

---

## Key File Locations

| File | Purpose |
|---|---|
| `/.n8n/database.sqlite` | n8n main database (workflows, credentials, executions) |
| `/.n8n/config` | n8n encryption key (used to derive JWT secret) |
| `/opt/n8n.env` | n8n systemd environment variables |
| `/opt/n8n-claw/.env` | Supabase secrets (POSTGRES_PASSWORD, JWT keys) |
| `/opt/n8n-claw/docker-compose.supabase.yml` | Supabase stack (no n8n service) |
| `/opt/n8n-claw/supabase/kong.deployed.yml` | Kong config with real API keys |
| `/opt/n8n-claw/workflows/` | Translated + configured workflow JSON files |
| `/workspace/n8n-reference/CHEATSHEET.md` | Claude Code CLI context cheatsheet |

---

## Workflow IDs (current)

| Workflow | ID |
|---|---|
| 🤖 n8n-claw Agent | `<auto-assigned after import>` |
| 🏗️ MCP Builder | `<auto-assigned after import>` |
| 🔌 MCP Client | `<auto-assigned after import>` |
| ⛅ MCP: Weather | `<auto-assigned after import>` |
| 🔔 ReminderFactory | `<auto-assigned after import>` |
| 🔨 WorkflowBuilder | `<auto-assigned after import>` |

---

## Workflow Activation Order
Always activate in this order to avoid dependency errors:
1. 🔌 MCP Client
2. ⛅ MCP: Weather
3. 🏗️ MCP Builder
4. 🔔 ReminderFactory
5. 🔨 WorkflowBuilder
6. 🤖 n8n-claw Agent

---

## Useful Commands

```bash
# Restart Supabase stack
docker compose -f /opt/n8n-claw/docker-compose.supabase.yml restart

# Restart n8n
systemctl restart n8n

# Check n8n logs
journalctl -u n8n -f

# Check Supabase containers
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# Check Telegram bot webhook status
BOTTOKEN=$(node -e "
  const c=require('crypto'),jwt=require('/usr/lib/node_modules/n8n/node_modules/jsonwebtoken/index.js');
  const db=require('/.n8n/database.sqlite'); // use sqlite3 cli instead
")
# Easier: decrypt from n8n credentials via the decrypt script

# Test n8n API key
curl -s -o /dev/null -w '%{http_code}' \
  -H 'X-N8N-API-KEY: <key>' \
  http://localhost:5678/api/v1/workflows?limit=1

# Open n8n SQLite DB
sqlite3 /.n8n/database.sqlite

# Scan all workflows for old mcp-server-api key
python3 -c "
import sqlite3
OLD='ImF1ZCI6Im1jcC1zZXJ2ZXItYXBpIi'
conn=sqlite3.connect('/.n8n/database.sqlite')
for table,col,id_col in [('workflow_entity','nodes','id'),('workflow_history','nodes','workflowId')]:
    for row in conn.execute(f'SELECT {id_col} FROM {table}'):
        r=conn.execute(f'SELECT {col} FROM {table} WHERE {id_col}=?',[row[0]]).fetchone()
        if r and r[0] and OLD in r[0]: print(f'OLD KEY in {table}: {row[0]}')
"

# Re-import a workflow
N8N_USER_FOLDER=/ n8n import:workflow --input=/opt/n8n-claw/workflows/<file>.json

# Check mcp_registry
docker exec n8n-claw-db psql -U postgres -d postgres -c \
  'SELECT server_name, path, mcp_url FROM mcp_registry;'

# Fix mcp_url to use localhost (if Cloudflare blocks it)
docker exec n8n-claw-db psql -U postgres -d postgres -c \
  "UPDATE mcp_registry SET mcp_url='http://localhost:5678/mcp/<path>' WHERE path='<path>';"
```
