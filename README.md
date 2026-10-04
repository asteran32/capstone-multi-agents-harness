# Guardrail Lab

HumanEval 문제에서 Problem designer → Code reviewer → Test runner를 순차 실행하고, 네 가지 Guardrail 모드의 출력과 판정 근거를 조회하는 최소 웹 데모입니다. [개발 명세](docs/web_guardrail_platform_development_spec_ko.md)와 [시스템 아키텍처](docs/web_guardrail_platform_system_architecture_ko.md)를 기준으로 구현했습니다.

FastAPI는 실행 접수·조회, 독립 Python worker는 LangGraph 실행을 담당합니다. PostgreSQL의 `app` schema에 실험 기록을, `checkpoint` schema에 재개 상태를 저장합니다. React 화면은 같은 FastAPI 주소에서 제공합니다. Redis, Celery, 사용자 관리, 자동 모델 선택 등 확장 기능은 포함하지 않습니다.

## 실행

Docker Desktop 또는 Docker Engine과 Compose가 필요합니다. Docker를 켠 뒤 저장소 루트에서 실행하세요. 최초 설정을 생성할 때는 Python 3도 필요합니다.

```sh
./start.sh
```

스크립트는 Docker 연결과 Compose 설정을 확인하고, `.env`가 없을 때만 초기 설정을 생성합니다. 다른 폴더에서도 `bash /프로젝트/경로/start.sh`로 실행할 수 있습니다. `./start.sh --help`로 사용법을 확인하세요.

출력된 접속 주소(기본 `http://localhost:8000`)를 열고 `.env`의 `APP_USERNAME`, `APP_PASSWORD`로 로그인합니다. 설정 스크립트는 임의 비밀번호와 내부 인증 토큰을 생성하며, 기존 `.env`는 덮어쓰지 않습니다. 첫 빌드에는 패키지와 이미지 다운로드가 필요합니다.

`start.sh`는 테스트 실행 이미지와 프런트엔드·백엔드를 빌드하고 PostgreSQL, migration·seed, API, worker, sandbox executor를 시작합니다. 프런트엔드는 API에서 함께 제공하므로 별도 프런트엔드 실행 명령이 필요하지 않습니다. 기존 `python3 scripts/configure.py`(최초 1회)와 `make up` 방식도 사용할 수 있습니다.

로그인하면 **Overview**에서 프로젝트 개요·목적과 시스템 아키텍처를 확인할 수 있습니다. 아키텍처 그래프는 개발 명세 6절의 LangGraph 실행 흐름으로, 도구 호출·L2 평가·Judge 재시도·FULL 복구 분기를 표시합니다. 상단 메뉴는 **Overview → 실험 → 일괄 실험 → 실행 기록** 순서이며, 기존 `?run=실행ID` 링크는 해당 실험 결과를 바로 엽니다.

**실험 열기** 또는 상단 **실험** 메뉴에서 문제·후보·모드를 고른 뒤 **새 실행 시작**을 누르세요. 기본 모델은 `mock`, 기본 모드는 `L1+L2`입니다.

```sh
# 상태와 로그
docker compose ps
docker compose logs -f api worker

# 종료. 데이터 volume은 유지합니다.
make down
```

포트는 기본적으로 로컬 호스트에만 공개합니다. 원격 공개 시에는 인증을 유지하고 HTTPS 진입점을 구성해야 합니다.

## 포함된 기능

| 영역 | 구현 |
|---|---|
| 실행 | Base / L1 / L1+L2 / FULL, round 및 역할 순서 고정, 취소·미완료 작업 재개 |
| L1 | 모드별 강화 프롬프트, 역할별 입력 DTO, 서버 측 도구 권한 |
| L2 | NFKC·casefold lexical 검사, 중첩 일치, Judge INFO/SCOPE/EXEC, 현재 응답의 정확한 인용 검증 |
| L3 | FULL의 최초 확정 위반에만 빈 history로 1회 재생성, 최초 및 복구 응답 보존 |
| History | 동일 run·role의 이전 종료 작업 최대 5건, 실패 포함, 실제 입력 snapshot 고정 |
| 지표 | 최초 응답 기준 최근 5회 M1~M4, V/C/U, coverage, warmup, null 및 disabled 구분 |
| 사후 평가 | 종료된 Base/L1 출력만 offline 평가, 생성·도구·복구 실행 없음 |
| 기록 | 실제 API payload, 모든 Judge 시도, 인용 검증, 도구 결과, 노드 로그 및 상세 조회 |
| 웹 | Overview 개요·목적·아키텍처 그래프, 설정 패널, 세로 3개 역할 출력과 우측 평가, 최초·복구 전환, run 및 call 필터 |
| 정책 | YAML/JSON 가져오기·편집·검증·파일 저장, 실행별 원문·설정·버전·해시 고정 |
| 안정성 | 생성 멱등성, DB 작업 큐·lease·heartbeat, 단일 worker 잠금, 고유키 및 저장 결과 재사용 |

원본 HumanEval의 **30문제(`HumanEval/0`~`HumanEval/29`)**를 포함하며, 선택 목록은 문제 번호순으로 표시합니다. 각 문제에 원본 정답 후보와 `return None` 오답 후보를 준비해 총 60개 후보를 제공합니다. 후보 라벨은 연구자 UI에만 표시하며 Agent·Judge 입력에는 전달하지 않습니다. 데이터 버전·SHA-256은 [provenance.json](data/provenance.json), 라이선스는 [HUMANEVAL_LICENSE](data/HUMANEVAL_LICENSE)에 있습니다.

기존 3문제 환경도 `./start.sh`를 다시 실행하면 이미지를 빌드하고 migration·seed 단계에서 새 문제를 추가합니다. 문제 원본은 `app.tasks`, 후보 코드는 `app.candidates`에 저장됩니다. 이미 등록된 문제는 건너뛰므로 재실행해도 중복되지 않으며 기존 실행 기록을 유지합니다.

## Mock과 실제 실행

### 정답 후보 30문제 일괄 실험

상단 **일괄 실험**에서 HumanEval/0~29의 정답 후보를 번호순으로 실행할 수 있습니다. 기본 모드는 **L1**, 모델은 **Mock**, 문제당 1 Round입니다. 실제 실험에서는 Agent·Judge 모델을 직접 선택합니다.

1. 모드, 문제당 Round, 모델, Run당 요청 한도와 공통 정책·요청을 설정합니다.
2. **30문제 일괄 실행 시작**을 누르면 30개의 독립 Run을 한 번에 접수합니다. L1은 기본적으로 각 Run의 생성 후 사후 평가까지 끝내고 다음 문제로 넘어갑니다.
3. 진행 화면에서 문제별 생성·평가 상태와 오류를 확인하고 **기록 열기**로 기존 상세 화면을 봅니다. `?batch=ID` 주소나 일괄 실행 기록에서 다시 열 수 있습니다.
4. **같은 구성으로 다시 실행**에서 L1+L2 또는 FULL을 선택하면, 문제·후보·모델·정책·요청·Round·예산을 유지한 새 일괄 실험이 만들어집니다. 기존 기록은 보존합니다.

실행 순서는 서버 DB에 저장되므로 브라우저를 닫아도 계속됩니다. 기존 단일 worker가 처리하며, 한 문제에서 오류가 발생하면 기록한 뒤 다음 문제로 진행합니다. **남은 실행 취소**는 대기 작업을 취소하고 진행 중인 모델·도구 요청이 반환된 다음 중지합니다. 취소한 일괄 실행은 새 실행으로 다시 시작합니다. 동일 접수 요청의 재전송은 같은 idempotency key를 사용하는 경우 중복 Run을 만들지 않습니다.

각 문제는 별도 이력으로 시작합니다. 이 기능은 정답 30문제의 순차 실행이며, 하나의 이력을 공유하면서 5번째마다 오답으로 바꾸는 혼합 시퀀스는 지원하지 않습니다. 정답 후보가 누락되거나 중복된 문제가 있으면 일부만 접수하지 않고 전체 요청을 거부합니다. 모드 변경 재실행 시 원본 문제·후보 해시가 바뀌었어도 요청을 거부합니다.

1 Round 기준으로 L1은 생성 120회와 선택적인 사후 평가 90회, L1+L2/FULL은 기본 210회 요청입니다. 복구·재시도는 추가됩니다. UI의 요청 한도는 **각 Run**에 적용되며, L1 사후 평가는 같은 크기의 별도 한도를 사용합니다. 기본 한도는 Run당 1,000회입니다.

| API | 기능 |
|---|---|
| `POST /api/batches` | 정답 30문제 접수. `mode`, `rounds`, `agent_model`, `judge_model`, `prompts_by_role`, `budget`, `policy_raw`, `evaluate_l1` 사용 |
| `GET /api/batches` | 일괄 실행 목록·진행 상태, offset/limit 지원 |
| `GET /api/batches/{id}` | 고정 manifest와 문제별 Run·사후 평가 상태 |
| `POST /api/batches/{id}/rerun` | `{"mode":"L1+L2"}` 등으로 모드만 바꿔 새 일괄 실행 접수 |
| `POST /api/batches/{id}/cancel` | 진행 중·대기 작업 취소 |

생성·재실행 API는 `Idempotency-Key`가 필요하며 기존과 같은 인증을 사용합니다. `0002` migration이 `app.batches`를 추가합니다. 기존 환경은 `./start.sh`로 새 코드·화면과 migration을 반영하세요. 이 기능 추가만으로 실제 모델 실험을 자동 실행하지는 않습니다.

### Mock 모델

`mock`은 그래프와 UI 검증용 고정 응답입니다. 실제 모델의 코드 검토 능력이나 Guardrail 효과를 측정하지 않습니다. 사용자 요청에 다음 문자열을 넣으면 복구 동작을 확인할 수 있습니다.

- `[mock:recover]`: designer/reviewer의 최초 응답에 위반을 만들고 복구 응답은 준수합니다.
- `[mock:violate]`: 최초 및 복구 응답에 위반을 만들어 미해결 상태를 확인합니다.

**Mock 모델과 sandbox 설정은 독립적입니다.** 기본 `SANDBOX_MODE=docker`에서는 mock runner도 실제 원본 테스트 실행을 요청합니다. 코드 실행 없이 UI·DB·그래프만 확인하려면 `.env`에서 `SANDBOX_MODE=mock`으로 바꾸고 `docker compose up -d`로 반영하세요. 이 경우 도구 결과는 `not_executed`, `mock: true`로 저장됩니다. 실제 pytest 결과처럼 표시하지 않습니다.

### 실제 모델

초기 adapter는 OpenAI Responses API를 사용합니다. `OPENAI_API_KEY`가 이미 셸 환경변수로 export되어 있다면 `.env`에는 허용할 모델 이름만 설정하세요. 예를 들어:

```dotenv
OPENAI_MODELS=gpt-5.4-nano
```

키가 설정된 터미널에서 API·worker를 재생성합니다. Docker Compose는 셸의 `OPENAI_API_KEY`를 worker에 전달합니다. 셸 환경변수는 `.env`보다 우선하며, `.env`의 `OPENAI_API_KEY=`가 비어 있어도 셸의 키를 사용합니다. 셸에 키가 없다면 로컬 `.env`에 설정할 수도 있습니다.

```sh
docker compose up -d --force-recreate api worker
```

화면을 새로고침하고 **Agent 모델**과 **Judge 모델**에서 사용할 모델을 각각 선택한 뒤 **새 실행 시작**을 누르세요. 기존 run의 모델 설정은 바뀌지 않습니다. 모델 이름 여러 개는 쉼표로 구분합니다.

모델은 해당 계정에서 Responses API를 지원해야 하며, runner 모델은 function calling, Judge 모델은 JSON 출력을 지원해야 합니다. 특정 모델을 자동 선택하거나 유료 검증 호출을 실행하지 않습니다. API 키는 worker에만 전달하고 업무 DB에는 저장하지 않습니다. GPT-5.4 nano의 지원 기능과 상태는 [공식 모델 문서](https://developers.openai.com/api/docs/models/gpt-5.4-nano)에서 확인할 수 있습니다.

Judge는 `text.format.type=json_object`를 사용하며, `input`의 developer 메시지에도 JSON 출력 지시를 넣습니다. `instructions`에만 JSON 지시가 있으면 공급자가 입력 검증 단계에서 HTTP 400을 반환할 수 있습니다. 이 오류가 발생했던 기존 환경은 `./start.sh`로 재빌드한 뒤 새 실행을 시작하세요. 기존 오류 기록은 보존합니다. 공급자 오류에는 상세 메시지와 문제 필드(`param`)를 함께 표시하며, 원본 응답은 호출 상세의 `response_payload`에서 확인할 수 있습니다.

### 예산

Run 설정의 `max_requests`는 Agent·Judge·전송 재시도·복구를 포함한 온라인 요청 수 상한입니다. Offline 평가는 별도의 동일 상한을 적용합니다. 각 요청에 입력 byte 상한과 출력 token 상한도 적용하며, 초과 시 입력을 임의로 잘라내지 않고 오류를 저장합니다. 공급자가 반환한 사용량은 요청별로 저장합니다.

각 역할의 위반율·Decision coverage 아래에서 현재 Round의 Agent 입력·출력·합계 토큰을 확인할 수 있습니다. 최초·도구 후속·복구·사용량이 보고된 재시도를 합산하며 Judge 평가 요청은 제외합니다. 그 아래 **LLM Judge 토큰 사용량**은 선택한 Online/Offline 단계의 최초·복구 평가와 재시도를 합산합니다. 최초/복구 탭을 바꾸어도 각 합계는 같으며, Online/Offline 탭을 바꾸면 Judge 사용량만 해당 평가 단계에 맞춰 바뀝니다. Mock은 0, 사용량이 없는 요청은 `—` 또는 일부 미집계 안내로 구분하고, 비활성 Judge에는 `평가 비활성`을 표시합니다. 기존 실행 기록도 저장된 사용량을 표시합니다. 금액 환산 UI는 포함하지 않으므로 실제 공급자의 과금 상한 설정도 함께 사용하세요.

Round는 세 Agent를 순서대로 한 번씩 실행하는 반복 단위입니다. Online은 응답 생성 과정의 평가로, L1+L2·FULL에서 Judge를 호출합니다. Offline은 Base·L1 실행 종료 후 저장된 출력을 평가하는 기능으로, Agent 생성이나 테스트 도구 실행을 반복하지 않고 Judge API만 호출합니다.

실행 기록 대시보드의 **모델 유형**에서 전체·Mock·Live를 선택할 수 있습니다. 저장된 모델 설정을 기준으로 Agent·Judge가 모두 `mock`이면 Mock, 하나 이상 실제 API 모델이면 Live입니다. Live 표시는 API 호출 성공 여부를 뜻하지 않으며, Base/L1에서 아직 사용하지 않은 Judge 설정도 포함합니다. Sandbox 설정과는 독립적입니다. 기존 기록에도 적용되며 모드·상태·날짜 필터와 함께 사용할 수 있습니다. 목록에는 유형과 Agent·Judge 모델을 함께 표시합니다. `GET /api/runs?model_type=mock` 또는 `model_type=live`로 조회하며, 필터를 적용한 전체 건수와 페이지를 반환합니다.

## 테스트와 검증 상태

일반 테스트는 SQLite와 메모리 checkpointer를 **테스트 harness에서만** 사용합니다. 운영 경로는 PostgreSQL과 PostgresSaver입니다. SQLite 테스트 성공을 실제 PostgreSQL 통합 검증으로 간주하지 않습니다.

```sh
# 모델 비용 없이 로직·API·worker 테스트
make test

# Node.js 22: 토큰 집계 테스트
npm --prefix frontend test

# 실제 PostgreSQL migration 및 재개 검사
# 임시 guardrail_test_* DB를 생성·검증·삭제합니다. 업무 DB는 삭제하지 않습니다.
docker compose run --rm --no-deps worker python /workspace/scripts/verify_postgres.py

# 실제 sandbox: 원본 정답/오답 테스트와 동일 operation 재사용 확인
docker compose run --rm --no-deps worker python /workspace/scripts/verify_sandbox.py
```

이번 작성 환경에서 확인한 결과:

| 검사 | 결과 |
|---|---|
| Python 로직·API·LangGraph·worker 자동 테스트 | 24개 통과 |
| 실제 PostgreSQL 테스트 | 실행 환경이 없어 1개 skip |
| TypeScript 타입 검사 및 Vite production build | 통과 |
| Python 문법 컴파일 및 Compose 설정 검사 | 통과 |
| 실제 PostgreSQL·PostgresSaver 연결 | 미검증: 공유 메모리 생성 권한 제한 |
| 실제 Docker sandbox·이미지 빌드 | 미검증: Docker 소켓 접근 제한 |
| 브라우저 화면 및 실제 서버 구동 | 미검증: 로컬 포트 listen 권한 제한 |
| 실제 공급자 호출 | 미실행 |

로직 검증에는 네 모드의 실제 payload 차이, lexical 통과/실패 모두 Judge 호출, 잘못된 인용 재시도, history 0/3/최근 5건과 실패 포함, FULL 복구 1회, runner 도구 재사용, offline의 생성 비변경, 예산 초과, 취소·재개, 업무 결과 저장 후 장애 재개를 포함합니다.

## 로컬 개발

Python 3.12 및 Node.js 22를 기준으로 구성했습니다. PostgreSQL은 별도로 실행해야 합니다. 아래 명령은 저장소 루트에서 실행합니다.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd frontend
npm ci
npm run build
cd ..
```

서버 프로세스에 `DATABASE_URL`, `APP_USERNAME`, `APP_PASSWORD`, `SANDBOX_MODE`를 환경변수로 설정합니다. Docker 외의 로컬 실행은 `.env`를 자동으로 읽지 않습니다.

```sh
export PYTHONPATH="$PWD/backend"
# DATABASE_URL은 postgresql+psycopg://... 형식
.venv/bin/alembic -c backend/alembic.ini upgrade head
.venv/bin/python -m app.seed
.venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000
# 별도 터미널, 동일 환경변수
.venv/bin/python -m app.worker
# UI 개발 서버는 frontend/에서 npm run dev
```

## 구현 구조와 운영 범위

```text
backend/app/
  api.py          인증, REST API, 정적 UI 제공
  contracts.py    설정·역할·평가 schema, 정책 parser
  services.py     실행 계획, 역할 DTO, history snapshot
  graphs.py       Agent graph와 별도 offline graph
  runtime.py      모델 전송·예산·도구 멱등성
  providers.py    mock 및 OpenAI adapter
  evaluation.py   lexical·인용·실행 근거·지표 계산
  worker.py       PostgreSQL queue, lease, checkpoint 재개
  db.py           12개 업무 테이블
  batches.py      정답 30문제 manifest, 순차 접수, 진행 집계·취소
sandbox/          executor와 격리된 pytest wrapper
frontend/src/     실행 화면과 대시보드
data/             원본 30문제(0~29), 출처 해시, 라이선스
policies/         기본 역할 정책
```

완료된 생성 실패는 이력에 남는 실험 결과입니다. **재개**는 중단된 작업과 아직 실행하지 않은 작업을 이어가며, 완료된 실패를 새 관측으로 재생성하지 않습니다. FULL 미해결도 종료된 결과입니다. 다시 실험하려면 새 run을 만드세요.

모델 API 응답 수신과 DB 저장 사이의 장애에서는 공급자 중복 호출 가능성이 있습니다. 도구는 실행 기록이 불명확할 때 자동으로 새 컨테이너를 실행하지 않습니다. Executor journal과 컨테이너 상태를 먼저 조회하며, 둘이 불일치하면 운영 오류를 유지합니다.

Sandbox는 네트워크 차단, 읽기 전용 root, 비특권 사용자, capability 제거, CPU·메모리·PID·시간 제한을 구성합니다. Docker socket은 executor에만 연결합니다. 실행 코드의 로그 파일에는 1 MiB 제한이 적용되며, 한도 초과는 실행 오류로 남습니다. 격리 설정의 실제 동작 확인은 위 live 검증과 배포 환경 점검이 필요합니다.

구현 시 참고한 공식 자료: [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Postgres checkpointer](https://pypi.org/project/langgraph-checkpoint-postgres/3.1.2/), [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [Docker 컨테이너 제한](https://docs.docker.com/reference/cli/docker/container/run/).
