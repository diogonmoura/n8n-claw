#!/usr/bin/env bash
set -euo pipefail

MODE="native"
ENV_FILE="$(pwd)/.env"
N8N_FOLDER="/.n8n"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Parse arguments ───────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --docker)          MODE="docker" ;;
    --native)          MODE="native" ;;
    --n8n-folder=*)    N8N_FOLDER="${arg#*=}" ;;
    --env=*)           ENV_FILE="${arg#*=}" ;;
  esac
done

echo "╔══════════════════════════════════════════════════════╗"
echo "║       n8n-claw Setup — mode: ${MODE}                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── Load or create .env ───────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  echo "Creating .env from .env.example..."
  cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
  echo ""
  echo "Please edit $ENV_FILE and re-run setup.sh"
  echo "Required: N8N_URL, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SUPABASE_URL"
  exit 1
fi

set -a; source "$ENV_FILE"; set +a

# Validate required vars
REQUIRED_VARS=(N8N_URL ANTHROPIC_API_KEY TELEGRAM_BOT_TOKEN SUPABASE_URL)
for v in "${REQUIRED_VARS[@]}"; do
  if [ -z "${!v:-}" ]; then
    echo "ERROR: $v is not set in $ENV_FILE"
    exit 1
  fi
done

# ── Detect OS ─────────────────────────────────────────────────────────────────
detect_os() {
  if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "${ID}"
  else
    echo "unknown"
  fi
}
OS=$(detect_os)
echo "Detected OS: $OS"

# ── Install Docker CE ─────────────────────────────────────────────────────────
install_docker() {
  if command -v docker &>/dev/null; then
    echo "Docker already installed: $(docker --version)"
    return
  fi
  echo "Installing Docker CE..."
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg lsb-release git

  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${OS}/gpg" \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${OS} $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  echo "Docker installed"
}

# ── Install n8n (native) ──────────────────────────────────────────────────────
install_n8n_native() {
  if command -v n8n &>/dev/null; then
    echo "n8n already installed: $(n8n --version 2>/dev/null || echo unknown)"
    return
  fi
  echo "Installing Node.js 20 and n8n..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y -qq nodejs
  npm install -g n8n
  echo "n8n installed"
}

setup_n8n_systemd() {
  echo "Setting up n8n systemd service..."
  N8N_HOST_ONLY=$(echo "$N8N_URL" | sed 's|https\?://||' | sed 's|/.*||')
  cat > /opt/n8n.env << ENVEOF
N8N_SECURE_COOKIE=false
N8N_PORT=${N8N_PORT:-5678}
WEBHOOK_URL=${N8N_URL}
N8N_EDITOR_BASE_URL=${N8N_URL}
N8N_HOST=${N8N_HOST_ONLY}
N8N_PROTOCOL=https
N8N_PROXY_HOPS=1
ENVEOF

  cp "$SCRIPT_DIR/n8n-systemd/n8n.service" /etc/systemd/system/n8n.service
  systemctl daemon-reload
  systemctl enable n8n
  echo "n8n systemd service configured"
}

# ── Generate Supabase secrets ─────────────────────────────────────────────────
generate_secrets() {
  if [ -z "${POSTGRES_PASSWORD:-}" ] || [ -z "${SUPABASE_JWT_SECRET:-}" ]; then
    echo "Generating Supabase secrets..."
    python3 "$SCRIPT_DIR/scripts/generate-secrets.py" --update-env
    set -a; source "$ENV_FILE"; set +a
    echo "Secrets generated and saved to .env"
  else
    echo "Supabase secrets already set"
  fi
}

# ── Generate kong.deployed.yml ────────────────────────────────────────────────
generate_kong() {
  echo "Generating supabase/kong.deployed.yml..."
  sed \
    -e "s|{{SUPABASE_ANON_KEY}}|${SUPABASE_ANON_KEY}|g" \
    -e "s|{{SUPABASE_SERVICE_KEY}}|${SUPABASE_SERVICE_KEY}|g" \
    "$SCRIPT_DIR/supabase/kong.yml" > "$SCRIPT_DIR/supabase/kong.deployed.yml"
  echo "kong.deployed.yml generated"
}

# ── Start Supabase stack ──────────────────────────────────────────────────────
start_supabase() {
  echo "Starting Supabase stack (docker compose)..."
  docker compose -f "$SCRIPT_DIR/docker-compose.supabase.yml" up -d
  echo "Waiting for PostgreSQL to be ready..."
  for i in $(seq 1 30); do
    if docker exec n8n-claw-db pg_isready -U postgres &>/dev/null; then
      echo "PostgreSQL ready"
      return
    fi
    echo "  ... attempt $i/30"
    sleep 3
  done
  echo "ERROR: PostgreSQL did not become ready in time"
  exit 1
}

# ── Run DB migrations ─────────────────────────────────────────────────────────
run_migrations() {
  echo "Running database migrations..."

  # Enable pgvector extension
  docker exec n8n-claw-db psql -U postgres -d postgres \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || true

  # Run schema (ON_ERROR_STOP=off to skip pre-existing objects)
  docker exec -i n8n-claw-db psql -U postgres -d postgres \
    --set ON_ERROR_STOP=off \
    < "$SCRIPT_DIR/supabase/migrations/001_schema.sql" 2>&1 | tail -5

  # Run English seed with variable substitution
  SEED=$(cat "$SCRIPT_DIR/supabase/migrations/002_seed_en.sql")
  SEED="${SEED//\{\{USER_TELEGRAM_ID\}\}/${USER_TELEGRAM_ID:-telegram:YOUR_ID}}"
  SEED="${SEED//\{\{USER_NAME\}\}/${USER_NAME:-user}}"
  SEED="${SEED//\{\{USER_DISPLAY_NAME\}\}/${USER_DISPLAY_NAME:-User}}"
  SEED="${SEED//\{\{USER_TIMEZONE\}\}/${USER_TIMEZONE:-UTC}}"
  SEED="${SEED//\{\{USER_CONTEXT\}\}/${USER_CONTEXT:-A user of n8n-claw}}"
  echo "$SEED" | docker exec -i n8n-claw-db psql -U postgres -d postgres 2>&1 | tail -5

  # Restart PostgREST to pick up new schema
  docker restart n8n-claw-rest
  echo "Migrations complete"
}

# ── Start n8n ─────────────────────────────────────────────────────────────────
start_n8n() {
  if [ "$MODE" = "native" ]; then
    systemctl start n8n || true
  else
    N8N_HOST=$(echo "$N8N_URL" | sed 's|https\?://||' | sed 's|/.*||') \
      docker compose -f "$SCRIPT_DIR/docker-compose.n8n.yml" up -d
  fi

  echo "Waiting for n8n to be ready (up to 90 seconds)..."
  for i in $(seq 1 30); do
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:${N8N_PORT:-5678}/healthz" \
       2>/dev/null | grep -qE "^2"; then
      echo "n8n ready"
      return
    fi
    echo "  ... attempt $i/30"
    sleep 3
  done
  echo "WARN: n8n health check timed out — continuing anyway"
}

# ── Main ──────────────────────────────────────────────────────────────────────
install_docker
generate_secrets

if [ "$MODE" = "native" ]; then
  install_n8n_native
  setup_n8n_systemd
fi

generate_kong
start_supabase
run_migrations
start_n8n

echo ""
echo "════════════════════════════════════════════════════════"
echo "  PHASE 2 — Manual steps required in n8n UI"
echo "════════════════════════════════════════════════════════"
echo ""
echo "1. Open n8n: ${N8N_URL}"
echo "   Complete first-run setup (create admin user)"
echo ""
echo "2. Create these credentials in n8n UI (Settings -> Credentials):"
echo "   a) Telegram API     -- name: 'n8n-claw Bot'"
echo "      accessToken: ${TELEGRAM_BOT_TOKEN}"
echo "   b) PostgreSQL       -- name: 'Postgres (Supabase)'"
echo "      host: 127.0.0.1, port: 5432, db: postgres"
echo "      user: postgres, password: ${POSTGRES_PASSWORD}"
echo "   c) Anthropic API    -- name: 'Anthropic account'"
echo "      apiKey: ${ANTHROPIC_API_KEY}"
echo "   d) HTTP Header Auth -- name: 'Kong (Supabase)'"
echo "      header: apikey, value: ${SUPABASE_ANON_KEY}"
echo ""
echo "3. After creating all credentials, run:"
echo "   python3 ${SCRIPT_DIR}/scripts/setup-db.py --env-file ${ENV_FILE}"
echo ""
echo "4. After setup-db.py completes, run:"
echo "   python3 ${SCRIPT_DIR}/scripts/phase2-wire-credentials.py --env-file ${ENV_FILE}"
echo ""
echo "5. In n8n UI, toggle 'n8n-claw Agent' OFF then ON"
echo "   (syncs Telegram webhook secret token)"
echo ""
echo "════════════════════════════════════════════════════════"
