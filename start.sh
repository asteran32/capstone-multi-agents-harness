#!/usr/bin/env bash
# Build the frontend and start FastAPI, worker, PostgreSQL and the sandbox.
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
사용법: ./start.sh

프런트엔드와 백엔드를 빌드하고 필요한 Docker 서비스를 시작합니다.
프런트엔드는 FastAPI에서 함께 제공됩니다.
최초 실행 시에만 .env를 생성하며, 기존 설정과 DB 데이터는 유지합니다.

필수: 실행 중인 Docker Desktop/Engine 및 Docker Compose
최초 설정 시 추가 필요: Python 3
종료: 프로젝트 폴더에서 make down
EOF
}

if [[ $# -gt 0 ]]; then
    if [[ $# -eq 1 && ( "$1" == "--help" || "$1" == "-h" ) ]]; then
        usage
        exit 0
    fi
    usage >&2
    exit 2
fi

fail() {
    printf '오류: %s\n' "$1" >&2
    exit 1
}

cd -- "$PROJECT_DIR"

command -v docker >/dev/null 2>&1 || fail 'Docker를 설치한 뒤 다시 실행하세요.'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose 플러그인이 필요합니다.'
docker info >/dev/null 2>&1 || fail 'Docker에 연결할 수 없습니다. Docker Desktop/Engine 실행 상태와 접근 권한을 확인하세요.'

if [[ ! -f .env ]]; then
    command -v python3 >/dev/null 2>&1 || fail '최초 .env 생성을 위해 Python 3가 필요합니다.'
    printf '초기 설정을 생성합니다.\n'
    python3 scripts/configure.py
fi

compose=(docker compose --project-directory "$PROJECT_DIR" --env-file "$PROJECT_DIR/.env" -f "$PROJECT_DIR/compose.yaml")
trap 'printf "실행 중 오류가 발생했습니다. 프로젝트 폴더에서 docker compose logs --tail=80으로 로그를 확인하세요.\n" >&2' ERR

# Validate without printing resolved credentials to the terminal.
"${compose[@]}" config --quiet

printf '\n테스트 실행 이미지를 빌드합니다.\n'
"${compose[@]}" build sandbox-image

printf '\n프런트엔드·백엔드와 필요한 서비스를 시작합니다.\n'
# Selecting API and worker also starts their declared dependencies.
"${compose[@]}" up --build -d api worker

printf '\n서비스 상태:\n'
"${compose[@]}" ps

address="$("${compose[@]}" port api 8000)"
printf '\n접속 주소: http://%s\n' "$address"
printf '로그인 정보: 프로젝트 .env의 APP_USERNAME / APP_PASSWORD\n'
printf '로그 확인: docker compose logs -f api worker\n'
printf '종료: make down\n'
