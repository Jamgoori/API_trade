#!/usr/bin/env bash
# hedgebot 설치 (Ubuntu/Debian VPS). 저장소 폴더 안에서 실행:  bash deploy/install.sh
# - Python 가상환경(.venv) 생성 후 설치
# - config.yaml / .env 가 없으면 예시에서 복사 (.env 는 본인만 읽을 수 있게 600 권한)
# - systemd 서비스 hedgebot.service 등록 (부팅 시 자동 시작, 죽으면 30초 뒤 재시작). 시작은 직접 합니다.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"

PY="${PYTHON:-python3}"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 이상이 필요합니다: sudo apt install -y python3.12 python3.12-venv 후 PYTHON=python3.12 bash deploy/install.sh"
  exit 1
fi

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -e . >/dev/null
echo "설치 완료: $REPO/.venv/bin/hedgebot"

if [ ! -f config.yaml ]; then
  sed 's/^mode: paper /mode: live  /' config.example.yaml > config.yaml
  echo "config.yaml 생성 (config.example.yaml 복사, mode: live)"
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env 생성: 키를 채워 넣으세요  ->  nano $REPO/.env"
fi
chmod 600 .env

UNIT=/etc/systemd/system/hedgebot.service
TMP="$(mktemp)"
sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$RUN_USER|g" deploy/hedgebot.service > "$TMP"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
if [ -d /run/systemd/system ]; then
  $SUDO install -m 644 "$TMP" "$UNIT"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable hedgebot >/dev/null
  echo "systemd 서비스 등록: $UNIT (아직 시작하지 않았습니다)"
else
  echo "systemd 가 없어 서비스 등록을 건너뜁니다. 직접 실행: .venv/bin/hedgebot run --mode live --i-understand-live"
fi
rm -f "$TMP"

cat <<MSG

다음 순서로 진행하세요 (README "실전 시작" 참고):
  1) nano .env                      # 키 입력 (채팅에 붙여넣지 마세요)
  2) .venv/bin/hedgebot check --mode live
  3) .venv/bin/hedgebot selftest --mode live --fill --i-understand-live
  4) sudo systemctl start hedgebot   # 24시간 실행 시작
     journalctl -u hedgebot -f       # 실시간 로그
MSG
