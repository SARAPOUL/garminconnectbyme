#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

SERVER_HOST="${SERVER_HOST:-mac-server}"
REMOTE_STACK_DIR="~/server/stacks/garminconnectbyme"

echo "==================================================="
echo "🚀 1. Sync ไฟล์ Token (.garminconnect) และ .env ไปยัง $SERVER_HOST"
echo "==================================================="
ssh "$SERVER_HOST" "mkdir -p $REMOTE_STACK_DIR/.garminconnect"

if [ -d "$PROJECT_DIR/.garminconnect" ]; then
  rsync -av "$PROJECT_DIR/.garminconnect/" "$SERVER_HOST:$REMOTE_STACK_DIR/.garminconnect/"
  echo "✅ Sync โฟลเดอร์ .garminconnect สำเร็จ"
fi

if [ -f "$PROJECT_DIR/.env" ]; then
  rsync -av "$PROJECT_DIR/.env" "$SERVER_HOST:$REMOTE_STACK_DIR/.env"
  echo "✅ Sync ไฟล์ .env สำเร็จ"
fi

echo ""
echo "==================================================="
echo "📦 2. Commit & Push โค้ดขึ้น GitHub (Trigger Auto-Deploy)"
echo "==================================================="
if [ -n "$(git status --porcelain)" ]; then
  COMMIT_MSG="${1:-Update Garmin auto-login and token refresh for local server}"
  git add -A
  git commit -m "$COMMIT_MSG"
  git push origin main
  echo "✅ Push โค้ดขึ้น GitHub สำเร็จ (GitHub Runner บน $SERVER_HOST จะ Rebuild อัตโนมัติ)"
else
  echo "ℹ️ ไม่มีโค้ดเปลี่ยนแปลงใหม่สำหรับ Commit"
  echo "🔄 สั่ง Restart Container บน $SERVER_HOST เพื่อโหลด Token ล่าสุด..."
  ssh "$SERVER_HOST" "export PATH=\"/usr/local/bin:\$HOME/.orbstack/bin:\$PATH\" && cd $REMOTE_STACK_DIR && docker compose restart"
  echo "✅ Restart Container สำเร็จ"
fi
