#!/usr/bin/env bash
# pumpfade 페이퍼 봇 설치 (Ubuntu/Debian VPS). 저장소 폴더 안에서 실행:  bash deploy/install_pumpfade.sh
# - 가벼운 전용 가상환경(.venv-pumpfade)에 requests, python-dotenv 만 설치
# - systemd 서비스 pumpfade.service 등록 (부팅 시 자동 시작, 죽으면 30초 뒤 재시작). 시작은 직접 합니다.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PY="${PYTHON:-python3}"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 이상이 필요합니다: sudo apt install -y python3.12 python3.12-venv 후 PYTHON=python3.12 bash deploy/install_pumpfade.sh"
  exit 1
fi
[ -d .venv-pumpfade ] || "$PY" -m venv .venv-pumpfade
.venv-pumpfade/bin/pip install --upgrade pip >/dev/null
.venv-pumpfade/bin/pip install "requests>=2.31" "python-dotenv>=1.0" >/dev/null
mkdir -p data
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo ".env 생성: 텔레그램 알림을 원하면 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 채우세요"; }
if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
  chown -R "$RUN_USER": .venv-pumpfade data .env
fi

UNIT=/etc/systemd/system/pumpfade.service
TMP="$(mktemp)"
sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$RUN_USER|g" deploy/pumpfade.service > "$TMP"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
if [ -d /run/systemd/system ]; then
  $SUDO install -m 644 "$TMP" "$UNIT"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable pumpfade >/dev/null
  echo "systemd 서비스 등록: $UNIT (아직 시작하지 않았습니다)"
else
  echo "systemd 가 없어 서비스 등록을 건너뜁니다. 직접 실행: .venv-pumpfade/bin/python -m pumpfade paper"
fi
rm -f "$TMP"

cat <<MSG

다음 순서로 진행하세요 (pumpfade/README.md 참고):
  1) .venv-pumpfade/bin/python -m pumpfade scan        # 어제 신호 확인 (주문 없음)
  2) sudo systemctl start pumpfade                     # 페이퍼 봇 시작
  3) journalctl -u pumpfade -f                         # 로그 보기
  4) .venv-pumpfade/bin/python -m pumpfade report      # 성과 보기
MSG
