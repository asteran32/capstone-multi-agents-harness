#!/usr/bin/env bash
# Create the four approved local commits from the project files.
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$PROJECT_DIR"

if [[ ! -e .git ]]; then
    git init -b main
fi

if [[ "$(git rev-parse --show-toplevel)" != "$PROJECT_DIR" ]]; then
    printf '오류: 프로젝트 루트의 Git 저장소에서 실행해야 합니다.\n' >&2
    exit 1
fi

git var GIT_AUTHOR_IDENT >/dev/null
git var GIT_COMMITTER_IDENT >/dev/null

if ! git diff --cached --quiet; then
    printf '오류: 이미 스테이징된 변경이 있습니다. 먼저 확인한 뒤 다시 실행하세요.\n' >&2
    exit 1
fi

if ! git check-ignore -q .env; then
    printf '오류: .env가 Git 제외 대상으로 설정되어 있어야 합니다.\n' >&2
    exit 1
fi

commit_files() {
    local message="$1"
    shift
    git add -- "$@"
    if git diff --cached --quiet; then
        printf '변경 없음: %s\n' "$message"
    else
        git commit -m "$message"
    fi
}

commit_files 'chore: initialize project documentation and ignore rules' \
    .gitignore .dockerignore docs/

commit_files 'feat: implement guardrail backend and HumanEval catalog' \
    backend/ policies/ data/ scripts/verify_postgres.py

commit_files 'feat: add experiment frontend' \
    frontend/

commit_files 'feat: add Docker sandbox and startup scripts' \
    sandbox/ Dockerfile compose.yaml Makefile start.sh .env.example README.md \
    scripts/configure.py scripts/verify_sandbox.py scripts/commit_initial.sh

printf '\n커밋 기록:\n'
git log -4 --oneline
printf '\n남은 변경:\n'
git status --short
