#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Zignal — one-command setup
#
# Usage:  bash setup.sh          (first install)
#         bash setup.sh --rekey  (keep venv, re-enter API keys only)
#
# What it does:
#   1. Checks Python 3.11+
#   2. Creates .venv and installs all dependencies
#   3. Walks you through entering API keys and writes config/config.yaml
#   4. Smoke-tests the install (imports + config load)
#   5. Prints the command to launch the dashboard
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── colours ──────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
  BLU='\033[0;34m'; CYN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
  RED=''; GRN=''; YLW=''; BLU=''; CYN=''; BOLD=''; NC=''
fi

hdr()  { echo -e "\n${BOLD}${BLU}── $1 ──────────────────────────────${NC}"; }
ok()   { echo -e "  ${GRN}✓${NC}  $1"; }
warn() { echo -e "  ${YLW}⚠${NC}  $1"; }
info() { echo -e "  ${CYN}→${NC}  $1"; }
die()  { echo -e "\n  ${RED}✗  ERROR: $1${NC}\n"; exit 1; }
blank(){ echo ""; }

REKEY=0
[[ "${1:-}" == "--rekey" ]] && REKEY=1

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Zignal — Setup${NC}"
echo "  Sets up the trading dashboard on this machine."
echo "  You will need API keys from Polygon.io and (optionally) Anthropic."
echo "  Takes about 1–2 minutes."

# ─────────────────────────────────────────────────────────────────────────────
hdr "Step 1 — Python version"

PYTHON=""
# Build candidate list: explicit versioned paths + PATH lookups
CANDIDATES=(
  /opt/homebrew/bin/python3.14
  /opt/homebrew/bin/python3.13
  /opt/homebrew/bin/python3.12
  /opt/homebrew/bin/python3.11
  /usr/local/bin/python3.14
  /usr/local/bin/python3.13
  /usr/local/bin/python3.12
  /usr/local/bin/python3.11
  python3.14
  python3.13
  python3.12
  python3.11
  python3
  python
)
for cmd in "${CANDIDATES[@]}"; do
  if command -v "$cmd" &>/dev/null 2>&1 || [ -x "$cmd" ]; then
    ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

[ -z "$PYTHON" ] && die "Python 3.11 or newer is required but was not found.
  Download it from https://www.python.org/downloads/ and re-run this script."

PYVER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
ok "Python $PYVER  ($PYTHON)"

# ─────────────────────────────────────────────────────────────────────────────
if [ "$REKEY" -eq 0 ]; then
  hdr "Step 2 — Virtual environment"

  if [ -d ".venv" ]; then
    ok ".venv already exists — skipping creation"
    info "Run 'bash setup.sh --rekey' to just re-enter API keys"
  else
    info "Creating .venv ..."
    "$PYTHON" -m venv .venv
    ok ".venv created"
  fi

  # ─────────────────────────────────────────────────────────────────────────
  hdr "Step 3 — Install dependencies"

  PIP=".venv/bin/pip"
  [ ! -f "$PIP" ] && die ".venv/bin/pip not found — try deleting .venv and re-running"

  info "Upgrading pip ..."
  "$PIP" install --quiet --upgrade pip

  info "Installing packages from requirements.txt ..."
  "$PIP" install --quiet -r requirements.txt

  ok "All packages installed"
fi   # end of --rekey skip

# ─────────────────────────────────────────────────────────────────────────────
hdr "Step 4 — API keys & configuration"

CFG="config/config.yaml"
TMPL="config/config.yaml.template"
[ ! -f "$TMPL" ] && die "config/config.yaml.template not found — is this the right directory?"

if [ -f "$CFG" ] && [ "$REKEY" -eq 0 ]; then
  echo ""
  echo -e "  ${YLW}config/config.yaml already exists.${NC}"
  echo -e "  Press ${BOLD}Enter${NC} to keep it as-is, or type ${BOLD}yes${NC} to re-enter all keys."
  read -r -p "  Re-configure? [Enter/yes]: " recfg
  if [[ ! "$recfg" =~ ^[Yy] ]]; then
    ok "Keeping existing config.yaml"
    SKIP_CONFIG=1
  else
    SKIP_CONFIG=0
  fi
else
  SKIP_CONFIG=0
fi

if [ "${SKIP_CONFIG:-0}" -eq 0 ]; then

  blank
  echo -e "  You will be prompted for each key below."
  echo -e "  ${CYN}Optional keys${NC} can be skipped by pressing Enter — the"
  echo -e "  feature that uses them simply won't be available."

  # ── Polygon.io (REQUIRED) ─────────────────────────────────────────────────
  blank
  echo -e "  ${BOLD}[1/4] Polygon.io API key  ${GRN}(required)${NC}"
  echo -e "  Used for: stock price data (free tier is sufficient)"
  echo -e "  Get it:   https://polygon.io → sign up free → Dashboard → API Keys"
  blank
  POLY_KEY=""
  while [ -z "$POLY_KEY" ]; do
    read -r -p "  Polygon API key: " POLY_KEY
    [ -z "$POLY_KEY" ] && echo -e "  ${RED}  This key is required — please enter it.${NC}"
  done

  # ── Anthropic (OPTIONAL) ──────────────────────────────────────────────────
  blank
  echo -e "  ${BOLD}[2/4] Anthropic API key  ${CYN}(optional)${NC}"
  echo -e "  Used for: AI analysis layer (the 'AI Verdict' section of reports)"
  echo -e "  Get it:   https://console.anthropic.com → API Keys"
  echo -e "  Skip:     press Enter — rule-based analysis still works fully"
  blank
  read -r -p "  Anthropic API key [Enter to skip]: " ANTH_KEY
  [ -z "$ANTH_KEY" ] && warn "Skipped — AI analysis will not be available"

  # ── Alpaca (OPTIONAL) ─────────────────────────────────────────────────────
  blank
  echo -e "  ${BOLD}[3/4] Alpaca API key + secret  ${CYN}(optional)${NC}"
  echo -e "  Used for: paper/live trading execution (not needed for Analyze-only use)"
  echo -e "  Get it:   https://alpaca.markets → sign up → Paper Trading → API Keys"
  echo -e "  Skip:     press Enter — dashboard Analyze page works without it"
  blank
  read -r -p "  Alpaca API key    [Enter to skip]: " ALPA_KEY
  read -r -p "  Alpaca secret key [Enter to skip]: " ALPA_SEC
  [ -z "$ALPA_KEY" ] && warn "Skipped — paper/live trading will not be available"

  # ── Finnhub (OPTIONAL) ────────────────────────────────────────────────────
  blank
  echo -e "  ${BOLD}[4/4] Finnhub API key  ${CYN}(optional)${NC}"
  echo -e "  Used for: earnings calendar (EarningsSwing strategy only)"
  echo -e "  Get it:   https://finnhub.io → sign up free → Dashboard → API Key"
  echo -e "  Skip:     press Enter — not needed for the main Analyze dashboard"
  blank
  read -r -p "  Finnhub API key   [Enter to skip]: " FINN_KEY
  [ -z "$FINN_KEY" ] && warn "Skipped — EarningsSwing strategy will not be available"

  # ── Write config.yaml ─────────────────────────────────────────────────────
  blank
  info "Writing config/config.yaml ..."

  # Use Python (already verified) to substitute placeholders reliably
  "$PYTHON" - <<PYEOF
import re, pathlib

template = pathlib.Path("$TMPL").read_text()

subs = {
    "YOUR_POLYGON_API_KEY":  """${POLY_KEY}""",
    "YOUR_ANTHROPIC_API_KEY": """${ANTH_KEY}""",
    "YOUR_ALPACA_API_KEY":   """${ALPA_KEY:-YOUR_ALPACA_API_KEY}""",
    "YOUR_ALPACA_SECRET_KEY": """${ALPA_SEC:-YOUR_ALPACA_SECRET_KEY}""",
    "YOUR_FINNHUB_API_KEY":  """${FINN_KEY:-YOUR_FINNHUB_API_KEY}""",
}

out = template
for placeholder, value in subs.items():
    out = out.replace(placeholder, value)

# Anthropic key goes in the llm section
if """${ANTH_KEY}""":
    out = re.sub(
        r'(llm:\n  api_key:\s*)"[^"]*"',
        r'\1"${ANTH_KEY}"',
        out,
    )

pathlib.Path("$CFG").write_text(out)
print("  done")
PYEOF

  ok "config/config.yaml written"
fi   # end of SKIP_CONFIG

# ─────────────────────────────────────────────────────────────────────────────
hdr "Step 5 — Smoke test"

PYTHON_VENV=".venv/bin/python"
[ ! -f "$PYTHON_VENV" ] && PYTHON_VENV="$PYTHON"

info "Checking package imports ..."
"$PYTHON_VENV" - <<'PYEOF'
errors = []
packages = [
    ("streamlit",        "streamlit"),
    ("plotly",           "plotly"),
    ("pandas",           "pandas"),
    ("anthropic",        "anthropic"),
    ("polygon",          "polygon"),
    ("yaml",             "PyYAML"),
    ("alpaca",           "alpaca-py"),
]
for mod, pkg in packages:
    try:
        __import__(mod)
    except ImportError:
        errors.append(f"{pkg} ({mod})")

if errors:
    print("  MISSING: " + ", ".join(errors))
    raise SystemExit(1)
else:
    print("  All packages imported successfully")
PYEOF

info "Checking config loads ..."
"$PYTHON_VENV" - <<PYEOF
import yaml, pathlib, sys

cfg_path = pathlib.Path("$CFG")
if not cfg_path.exists():
    print("  config/config.yaml not found")
    sys.exit(1)

cfg = yaml.safe_load(cfg_path.read_text())
poly_key = cfg.get("polygon", {}).get("api_key", "")
if not poly_key or "YOUR_" in poly_key:
    print("  Polygon API key is not set — re-run 'bash setup.sh --rekey'")
    sys.exit(1)

print("  Config OK — Polygon key is set")
PYEOF

ok "Smoke test passed"

# ─────────────────────────────────────────────────────────────────────────────
blank
echo -e "${BOLD}${GRN}Setup complete!${NC}"
blank
echo -e "  ${BOLD}Launch the dashboard:${NC}"
echo ""
echo -e "    ${CYN}.venv/bin/python main.py --mode dashboard${NC}"
echo ""
echo -e "  Then open  ${BOLD}http://localhost:8501${NC}  in your browser."
blank
echo -e "  ${BOLD}Other commands:${NC}"
echo -e "    Re-enter API keys only:  ${CYN}bash setup.sh --rekey${NC}"
echo -e "    Run a backtest:          ${CYN}.venv/bin/python main.py --mode backtest${NC}"
blank
echo -e "  ${YLW}Note:${NC} config/config.yaml contains your API keys."
echo -e "  It is in .gitignore and will never be pushed to git."
blank
