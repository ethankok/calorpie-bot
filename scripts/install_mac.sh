#!/bin/zsh
# One-time setup on the Mac: stores the BotFather token in Keychain and starts the bot under launchd.
# Usage: scripts/install_mac.sh            (prompts for the token)
set -euo pipefail
cd "$(dirname "$0")/.."
if ! security find-generic-password -a "$USER" -s TELEGRAM_BOT_TOKEN_CALBOT -w >/dev/null 2>&1; then
  read -rs "token?Paste the bot token from @BotFather: "; echo
  [[ "$token" == *:* ]] || { echo "That doesn't look like a bot token"; exit 1; }
  security add-generic-password -U -a "$USER" -s TELEGRAM_BOT_TOKEN_CALBOT -w "$token"
fi
security find-generic-password -a "$USER" -s GEMINI_API_KEY_CALBOT -w >/dev/null || { echo "Missing GEMINI_API_KEY_CALBOT in Keychain"; exit 1; }
if ! security find-generic-password -a "$USER" -s ACCESS_CODE_CALBOT -w >/dev/null 2>&1; then
  # 8 random letters/digits, skipping look-alikes (0/o, 1/l/i) so it's easy to type from a screenshot
  code=$(LC_ALL=C tr -dc 'a-hjkmnp-z2-9' </dev/urandom | head -c 8)
  security add-generic-password -U -a "$USER" -s ACCESS_CODE_CALBOT -w "$code"
fi
uv sync --frozen
mkdir -p data
plist=~/Library/LaunchAgents/local.calorie-bot.plist
sed -e "s|__DIR__|$PWD|g" -e "s|__USER__|$USER|g" deploy/calorie-bot.plist > "$plist"
launchctl bootout "gui/$(id -u)/local.calorie-bot" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
sleep 4
tail -5 data/bot.log
echo
echo "Access code to share: $(security find-generic-password -a "$USER" -s ACCESS_CODE_CALBOT -w)"
echo "Logs: tail -f data/bot.log    Stop: launchctl bootout gui/\$(id -u)/local.calorie-bot"
