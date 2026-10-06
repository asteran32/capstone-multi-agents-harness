# Guardrail Lab

HumanEval 문제에서 Problem designer → Code reviewer → Test runner를 순차 실행하고, 네 가지 Guardrail 모드의 출력과 판정 근거를 조회하는 harness engineering 실험용 웹 데모입니다. [개발 명세](docs/web_guardrail_platform_development_spec_ko.md)와 [시스템 아키텍처](docs/web_guardrail_platform_system_architecture_ko.md)를 기준으로 구현했습니다.

연구는 역할·도구·출력 계약 준수, 위반 탐지, 복구, 실행 신뢰성과 토큰·지연 비용에 집중합니다. HumanEval은 통제된 작업 입력이며 교육적 위험과 학습 효과는 평가 범위에서 제외합니다. 비교 조건, 독립 평가와 장애 검증 계획은 [Harness Engineering 실험 계획서](docs/web_guardrail_platform_experiment_plan_ko.md)에 정리했습니다. 이 계획의 분석 도구와 장애 fixture가 모두 구현된 것은 아닙니다.

본실험은 **L1 / L1+L2 / FULL** 세 조건을 사용합니다. L1의 역할·요구사항·금지사항·출력 명세를 공통 기준선으로 두고 온라인 평가와 복구의 추가 효과를 비교합니다. Base는 앱에서 지원하지만 본실험에서는 제외합니다. L1의 저장 응답도 사후 평가하며, L1 명세 자체의 효과는 이번 비교로 추정하지 않습니다.

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
| L2 | NFKC·casefold 금지 문자열 검사, 역할별 Judge INFO/SCOPE/EXEC, 원문 인용 검증·오류 피드백 재시도, Runner 서버 검사와 판정 충돌 처리 |
| L3 | 새 FULL 실행은 최초 위반 시 검증된 history를 유지하고 원본 응답·확인된 위반을 전달하는 feedback 복구 최대 1회. reset 비교 전략은 history 제거, 기존 실행의 전략과 최초·복구 기록 보존 |
| History | 같은 배치·같은 역할의 최신 준수 출력 최대 1개, 단독 Run은 자기 Run 안에서만 선택, 실제 입력 snapshot 고정 |
| 지표 | 최초 응답 기준 최근 5회 M1~M4, V/C/U, coverage, warmup, null 및 disabled 구분 |
| 사후 평가 | 종료된 Base/L1 출력만 offline 평가, 생성·도구·복구 실행 없음 |
| 저장 응답 재평가 | 종료된 배치의 초기·복구 응답 고정, 사람의 기대 판정 검토, 합성 위반 사례, 새 평가 버전·별도 예산·결과 CSV, 검증 후 모드/Round/복구 전략 비교 |
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
2. **30문제 일괄 실행 시작**을 누르면 30개의 Run을 한 번에 접수합니다. 역할별 준수 출력 1개가 문제 간에 이어집니다. L1은 기본적으로 각 Run의 생성 후 사후 평가까지 끝내고 다음 문제로 넘어갑니다.
3. 진행 화면에서 문제별 생성·평가 상태와 오류를 확인하고 **기록 열기**로 기존 상세 화면을 봅니다. `?batch=ID` 주소나 일괄 실행 기록에서 다시 열 수 있습니다.
4. **같은 구성으로 다시 실행**에서 L1+L2 또는 FULL을 선택하면, 문제·후보·모델·정책·요청·Round·예산을 유지한 새 일괄 실험이 만들어집니다. 기존 기록은 보존합니다.
5. **일괄 실행 기록**에서 해당 배치의 **열기 →** 옆 **결과 다운로드**를 누르면 분석용 CSV를 받습니다. 실행 중·취소된 배치도 다운로드할 수 있습니다.
6. 배치를 열면 상단 **실험 요약**에서 최초·최종 위반, 준수 응답 제공률, 역할별 Min/Max, L3 복구, Judge 검증 실패, 실행·토큰·시간을 확인합니다. 사건과 Min/Max에서 해당 호출 상세 기록으로 이동할 수 있습니다.

**M4 threshold (%)**는 배치별 분석 설정으로 저장하며 실행 조건과 독립적입니다. 공란은 미설정, 초과 기준은 `M4 > threshold`입니다. 같은 문제·역할의 평가창 5건이 갖춰진 관측만 정식 집계하므로 1 Round 실험은 Warmup으로 표시합니다. 모드 변경 재실행에는 현재 threshold만 이어받습니다. 상세 지표·분모·과거 기록 처리 기준은 [일괄 실험 대시보드 명세](docs/web_guardrail_platform_batch_dashboard_spec_ko.md)를 참고하세요.

실행 순서는 서버 DB에 저장되므로 브라우저를 닫아도 계속됩니다. 기존 단일 worker가 처리하며, 한 문제에서 오류가 발생하면 기록한 뒤 다음 문제로 진행합니다. **남은 실행 취소**는 대기 작업을 취소하고 진행 중인 모델·도구 요청이 반환된 다음 중지합니다. 취소한 일괄 실행은 새 실행으로 다시 시작합니다. 동일 접수 요청의 재전송은 같은 idempotency key를 사용하는 경우 중복 Run을 만들지 않습니다.

각 배치는 빈 이력에서 시작합니다. HumanEval/0의 첫 Round는 세 역할 모두 `history: []`이며, 이후에는 같은 배치의 앞선 문제·Round에서 **자기 역할의 최신 준수 출력 1개**만 전달합니다. 준수 출력이 아직 없으면 계속 비워 두고, 이후 위반·불확실이 발생해도 이전 준수 출력은 유지합니다. 다른 역할·다른 배치·재실행 원본 배치의 출력은 가져오지 않습니다. 단독 Run에서는 같은 Run의 이전 Round만 검색합니다.

이력 채택 조건은 완료된 호출의 **최종 선택 응답**, 현재 실행과 같은 평가 버전, 유효한 schema/evidence 검증, Judge의 INFO/SCOPE/EXEC 모두 `compliant`, 최종 종합 판정 `compliant`입니다. L3 복구가 준수로 완료되면 복구 출력만 채택하고 최초 위반 출력은 제외합니다. 이력에는 `raw_text`와 출처 문제·call·response·evaluation ID 및 응답 버전만 포함합니다. 과거 문제·코드 입력, 사용자 요청, 다른 역할 출력, Judge 설명, 도구 결과는 첨부하지 않습니다. Runner는 과거 성공 보고를 현재 테스트의 실행 근거로 사용하지 않습니다.

L1의 자동 사후 평가를 끄면 준수 판정이 없어 이력이 쌓이지 않습니다. 켜면 이전 Run의 사후 평가가 끝난 후 다음 문제에 이력이 전달됩니다. 여러 Round인 L1에서는 현재 Run의 모든 생성이 끝나야 사후 평가가 시작되므로, 같은 Run 안에서는 이전 문제에서 얻은 이력이 유지됩니다. L1+L2/FULL은 각 호출의 온라인 평가 후 다음 Round에도 최신 이력을 사용할 수 있습니다. history 수집을 위한 추가 모델 요청은 없습니다.

2026-10-06 변경 이후 새 실행은 `history_limit=1`입니다. 기존 `history_limit=5` 실행과 저장 snapshot은 원래 방식으로 보존하며, 기존 배치에서 **같은 구성으로 다시 실행**해도 새 배치에는 1개 규칙을 적용합니다. 화면은 실제 실행 설정에 맞춰 `History 0/1`, `1/1` 또는 과거 기록의 `/5`와 출처 문제를 표시합니다. 기본 L3 `feedback`은 최초 요청에 고정된 준수 이력을 유지하며, 비교 전략 `reset`은 제거하고 `retry`는 유지합니다. M4 등 지표 창은 기존의 같은 문제·역할별 최근 5개 최초 응답 기준으로 유지합니다.

정답 30문제의 순차 실행을 지원하며, 5번째마다 오답으로 바꾸는 혼합 시퀀스는 지원하지 않습니다. 정답 후보가 누락되거나 중복된 문제가 있으면 일부만 접수하지 않고 전체 요청을 거부합니다. 모드 변경 재실행 시 원본 문제·후보 해시가 바뀌었어도 요청을 거부합니다.

1 Round 기준으로 L1은 생성 120회와 선택적인 사후 평가 90회, L1+L2/FULL은 기본 210회 요청입니다. 복구·재시도는 추가됩니다. UI의 요청 한도는 **각 Run**에 적용되며, L1 사후 평가는 같은 크기의 별도 한도를 사용합니다. 기본 한도는 Run당 1,000회입니다.

| API | 기능 |
|---|---|
| `POST /api/batches` | 정답 30문제 접수. `mode`, `rounds`, `agent_model`, `judge_model`, `prompts_by_role`, `budget`, `policy_raw`, `evaluate_l1` 사용 |
| `GET /api/batches` | 일괄 실행 목록·진행 상태, offset/limit 지원 |
| `GET /api/batches/{id}` | 고정 manifest와 문제별 Run·사후 평가 상태 |
| `GET /api/batches/{id}/results.csv` | 해당 배치의 분석용 CSV 다운로드. 기존 인증 필요 |
| `GET /api/batches/{id}/analytics` | 요약·역할별 지표·복구·Judge 검증·비용·시간·사건 목록 |
| `PUT /api/batches/{id}/analysis-settings` | `m4_threshold`(0~1 또는 null), 현재 `revision`으로 분석 설정 저장 |
| `POST /api/batches/{id}/rerun` | `{"mode":"L1+L2"}` 등으로 모드만 바꿔 새 일괄 실행 접수 |
| `POST /api/batches/{id}/cancel` | 진행 중·대기 작업 취소 |

생성·재실행 API는 `Idempotency-Key`가 필요하며 기존과 같은 인증을 사용합니다. `0002` migration이 `app.batches`를 추가합니다. 기존 환경은 `./start.sh`로 새 코드·화면과 migration을 반영하세요. 이 기능 추가만으로 실제 모델 실험을 자동 실행하지는 않습니다.

`0003` migration은 기존 배치에 분석 설정을 추가합니다. Judge 검증은 새 실행부터 schema·필수 인용·공급자·예산 실패를 구분하고 모든 검증 시도를 보존합니다. 과거 기록은 저장된 검증 로그로 집계하되 확인할 수 없는 실패 유형은 `분류 불가`로 표시합니다. 인용 일치율은 Judge의 의미적 정확도가 아닙니다.

LLM Judge의 공통 판정 기준(`common_rubric`), 세 역할별 기준(`role_rubrics`), 검증 실패 재시도 지침(`retry_instruction`)은 [policies/judge.yaml](policies/judge.yaml)에서 편집합니다. Agent 역할 정책은 기존 [policies/default.yaml](policies/default.yaml)에서 관리합니다. Judge 파일의 필수 항목 누락·빈 문구·중복 키·잘못된 역할은 오류로 처리하며 기본 문구로 대체하지 않습니다.

역할 경계 few-shot은 Agent 정책의 역할별 `few_shot_examples`와 Judge 파일의 역할별 `few_shot_examples`에서 관리합니다. 기본 정책 1.2.0에는 Agent 예시 10개(Designer 2, Reviewer 3, Runner 5), Judge 예시 19개(Designer 5, Reviewer 8, Runner 6)가 있습니다. HumanEval 실험 문제와 별도로 만든 예시이며, Agent에는 준수 응답을, Judge에는 참고 자료·평가 대상·규칙별 기대 JSON 판정을 제공합니다. Judge의 위반 예시는 정확한 증거 인용과 역할별 criterion_id를 파일 로딩 시 검증합니다. 예시의 라벨은 작성된 기대 판정이며 실제 모델의 성능 검증 결과가 아닙니다.

Agent 예시는 L1 / L1+L2 / FULL의 역할 프롬프트에만 포함됩니다. Judge는 평가 대상 역할의 예시만 받고, 실제 대상 응답과 예시를 분리합니다. Agent 예시를 Judge 정책 요구사항으로 다시 전달하지 않습니다. 예시 없는 기존 정책과 저장된 평가 프로필도 지원합니다. 기존 배치의 모드 변경 재실행은 이전 Agent 정책을 유지하고 Judge는 새 배치 생성 시 최신 파일을 읽습니다. 세 Agent에도 새 기본 예시를 적용하려면 화면을 새로고침하고 새 실험을 생성하세요. 이번 few-shot 추가는 평가 권한·서버 검사·L3 발동 조건을 변경하지 않습니다.

새 실험과 재평가 검증 세트를 만들 때 Judge 파일을 읽어 `evaluation_profile`에 저장합니다. 한 배치의 30개 Run은 같은 사본을 사용하며, 실제 Judge 요청과 재시도는 저장된 사본을 사용합니다. 문구 변경은 새 설정의 `evaluation_version`에 반영되고, 기존 실험·검증 세트 및 검증된 평가 기준을 사용하는 비교 실험은 고정된 기준을 유지합니다. Docker에서는 파일을 이미지에 포함하므로 편집 후 `docker compose build api`와 `docker compose up -d --no-deps api worker`로 반영합니다. 실행 중인 실험이 끝난 뒤 재시작하세요.

### 저장 응답 재평가

배치의 **저장 응답 재평가 → 재평가 준비·기록 열기**에서 다음 순서로 진행합니다. 준비·검토만으로 실제 모델을 호출하지 않습니다.

1. 종료된 배치에서 **검증 세트 준비**를 누릅니다. 초기·복구 응답 전체와 정상/위반 합성 사례를 고정하고, 정책·Judge·출력 한도로 평가 버전을 만듭니다. Experiment 01은 저장 응답 132개이며 기본 정책의 합성 사례는 12개입니다.
2. 역할 정책·원문·실행 증거를 확인하고 사례별 기대 C/V/U, 근거, 검토자를 저장합니다. 합성 사례의 제안도 검토가 필요합니다. 모든 사례와 정책을 확인한 뒤 **검토 확정**을 누릅니다.
3. **재평가 실행**을 누르면 Judge만 호출하는 별도 작업이 등록됩니다. 원본 응답·선택 결과·M4·L3는 유지됩니다. 고정된 요청 한도 안에서 검증/전송 재시도는 각각 최대 한 번이며, 중단 시 저장된 공급자 응답을 재사용합니다. 완료된 오류를 다시 측정하려면 새 재평가 실행을 만드세요.
4. 동일 저장 사례의 FP/FPR·FN·탐지율·U·오류와 역할별 혼동 행렬, 합성 사례 일치, schema/evidence 실패를 확인합니다. **Judge 요청·응답 로그 보기**와 **재평가 결과 CSV 다운로드**로 근거를 분석합니다. 기대 U는 이진 지표에서 제외하고, 평가 오류를 준수로 계산하지 않습니다.
5. 실제 Judge의 완료 결과가 검증 기준을 충족하면 **같은 평가 기준으로 비교 실험 시작**에서 L1/L1+L2/FULL을 생성합니다. Round 조건(최대 3개)을 분리하고, 필요하면 FULL의 feedback/reset/retry를 비교합니다. reset/retry는 피드백 없이 이력을 각각 제거/유지하는 대조 조건입니다. Mock는 품질 검증 통과 근거로 사용하지 않습니다.

새 작업은 `0005`의 별도 검토·재평가·요청 테이블에 저장됩니다. 기존 `/runs/{id}/evaluate`는 원래 Base/L1 사후 평가로 유지됩니다. 재평가에는 기존 Run당 예산 대신 **재평가 실행 전체의 전용 요청 예산**을 적용합니다. 예를 들어 132개 응답 + 12개 합성 사례는 기본 144회, 재시도 포함 최대 576회입니다(설정한 전체 예산 이내). 실제 모델 실행은 버튼을 눌렀을 때만 시작합니다.

| API | 용도 |
|---|---|
| `GET/POST /api/batches/{id}/review-sets` | 검증 세트 목록 / 준비 |
| `GET /api/review-sets/{id}` | 고정 입력·정책·기대 판정·실행 이력 |
| `PUT /api/review-sets/{id}/cases/{case_id}/label` | revision을 확인하여 사람의 검토 저장 |
| `POST /api/review-sets/{id}/seal` | 모든 사례와 정책 검토 확정 |
| `POST /api/review-sets/{id}/execute` | 새 Judge 재평가 접수 |
| `GET /api/reevaluations/{id}` | 진행률·판정·검증 지표 |
| `GET /api/reevaluations/{id}/cases/{case_id}` | 사례별 Judge 요청·응답 감사 로그 |
| `POST /api/reevaluations/{id}/cancel`, `/resume` | 중단 / 미완료 작업 재개 |
| `GET /api/reevaluations/{id}/results.csv` | 고정 사례당 한 행의 결과 CSV |
| `POST /api/reevaluations/{id}/comparisons/start` | 검증된 평가 기준으로 모드·Round·복구 전략별 신규 배치 생성 |

준비·실행·후속 비교 접수는 `Idempotency-Key`가 필요합니다. 검증 기준과 분모는 [저장 응답 검증 계획](docs/stored_response_validation_plan_ko.md)에 정리했습니다.

### 일괄 실험 결과 CSV

CSV는 **문제 × Round × 역할별 한 행**이며, 1 Round이면 헤더를 제외하고 90행입니다. HumanEval 번호, 역할 호출 순서로 정렬하고, 최초·복구 응답을 같은 행에 저장해 토큰을 중복 집계하지 않습니다. 선택한 배치 전체를 내려받으므로 목록의 페이지 크기나 현재 열어 둔 배치와 무관합니다.

| 열 | 의미 |
|---|---|
| `csv_schema_version`, `exported_at`, `batch_id`, `source_batch_id`, `batch_status` | CSV 형식 버전(3), 다운로드 시각, 배치와 원본 배치, 다운로드 시점의 상태 |
| `analysis_revision`, `m4_threshold` | 다운로드 시점의 분석 설정 revision과 threshold(0~1, 미설정은 빈 값) |
| `batch_index`, `source_task_id`, `task_sha256`, `candidate_sha256`, `run_id` | 문제 순번, 데이터·후보 식별 및 Run 연결 |
| `mode`, `agent_model`, `judge_model`, `sandbox_mode`, `policy_hash`, `evaluation_version`, `max_requests` | 실행 당시 고정된 비교 조건 |
| `round_index`, `role`, `call_status`, `generation_job_status`, `offline_job_status` | Round·역할별 생성과 사후 평가 진행 상태 |
| `initial_response_text`, `recovery_response_text`, `final_response_text`, `selected_response_version` | 최초·복구·최종 선택 응답. 아직 선택되지 않은 최종 응답은 빈 값 |
| `evaluation_phase`, `initial_*`, `recovery_*`, `final_*` | L1은 Offline, L1+L2·FULL은 Online 평가. 최초·복구·최종 선택의 종합·Lexical·적용 INFO/SCOPE/EXEC 판정, 원래 Judge 근거와 인용·실행 검증 결과 |
| `initial_schema_valid`, `initial_evidence_valid`, `initial_validation_failure_kind` 등 | 최초·복구·최종 선택 평가의 구조화된 검증 결과. `evidence_required`로 인용 검사 대상 여부 구분. 과거 미기록 값은 빈 값 |
| `recovery_strategy`, `recovery_feedback`, `recovery_effect` | 복구 전략, 원본 응답·확인된 위반 피드백, 응답 변경·동일 응답 재판정·평가 오류 해소의 구분 |
| `*_judgment_resolution`, `*_uncertainty_reasons` | 서버/Judge 적용 판정·주체·충돌 및 불확실성 원인. 과거 미측정 판정 충돌은 새로 추정하지 않음 |
| `initial_window_count`, `initial_V`, `initial_C`, `initial_U`, `initial_role_violation_rate`, `initial_decision_coverage`, `initial_warmup` | 최초 응답 기준 역할별 최근 최대 5건의 저장된 지표. 배치 전체의 비율이 아님 |
| `agent_*`, `judge_*` | Agent와 선택 평가 단계의 Judge 요청·실패 건수, 사용량 보고 건수, 입력·출력·합계 토큰. 후속 요청·재시도·복구 포함 |
| `started_at`, `finished_at`, `generation_duration_ms` | UTC 시각과 역할 호출 처리 시간. 온라인 평가·복구를 포함하며 L1 사후 평가 시간은 제외 |
| `run_error`, `call_error`, `job_errors`, `request_errors`, `tool_results` | 오류와 도구 실행 결과. 구조화된 상세 값은 JSON 문자열 |

대기·실패·취소된 역할도 행을 유지합니다. 미평가는 `not_evaluated`, 미생성 응답·미보고 토큰은 빈 값이며 Mock이 보고한 0토큰과 구분합니다. 일부 요청만 사용량이 있으면 알려진 합계와 `*_usage_reported_requests`를 함께 제공합니다. PostgreSQL에서는 하나의 읽기 전용 snapshot으로 조회하므로 다운로드 중 실행이 진행되어도 파일 안의 상태·결과·사용량은 같은 시점을 나타냅니다. 다운로드는 모델 호출이나 실행 상태 변경을 일으키지 않습니다.

파일은 한글을 지원하는 **UTF-8 BOM CSV**입니다. 문자열의 쉼표·인용부호·줄바꿈은 CSV 규칙으로 인용하며, 스프레드시트 수식으로 해석될 수 있는 문자열 앞에는 작은따옴표를 붙입니다. 분석 시 해당 문자열의 접두사에 유의하세요.

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
| Python 로직·API·LangGraph·worker 자동 테스트 | 145개 통과(역할별 최신 준수 history·배치 간 격리·few-shot·평가 v2·feedback 복구·CSV·대시보드·재평가·Judge YAML 설정 고정 포함). PostgreSQL 통합 테스트 1개는 로컬 실행에서 제외하고 아래 별도 환경에서 검증 |
| 프런트엔드 토큰 집계 테스트 | 8개 통과 |
| 실제 PostgreSQL 테스트 | 별도 임시 DB에서 1개 통과: L1 사후 평가 후 다음 문제의 같은 역할에 준수 history 전달, 기존 schema 업그레이드, checkpoint 재개, 일괄 작업 의존 순서·취소, CSV·분석 API의 읽기 전용 조회·JSONB 사용량, 분석 설정 저장 및 기존 데이터 보존 |
| TypeScript 타입 검사 및 Vite production build | 통과 |
| Python 문법 컴파일 및 Compose 설정 검사 | 통과 |
| 실제 PostgreSQL·PostgresSaver 연결 | 임시 PostgreSQL 통합 테스트에서 확인 |
| Docker 앱 이미지 빌드 | 통과. 실제 sandbox 코드 실행은 이번 변경에서 재검증하지 않음 |
| 브라우저 화면 및 실제 서버 구동 | Mock 전용 임시 환경에서 L1→L1+L2→FULL 각 30문제 완료, 새로고침·상세 이동·모바일 화면 확인. CSV 버튼 위치, 완료·실행 중 배치 다운로드, 오류 후 재시도 및 파일의 90행 검증. 대시보드 threshold 저장·유지, 초과·Min/Max의 정확한 호출 이동, Judge 실패 필터, Warmup 표시, 판정 충돌·미해결 상세 이동, 도움말과 390px 모바일 화면 확인 |
| 실제 공급자 호출 | 미실행 |

로직 검증에는 네 모드의 실제 payload 차이, lexical 통과/실패 모두 Judge 호출, 잘못된 인용 재시도, 역할별 준수 history 0/1건·배치 간 격리·L1 사후 평가 연계·기존 5건 방식 호환, FULL 복구 1회, runner 도구 재사용, offline의 생성 비변경, 예산 초과, 취소·재개, 업무 결과 저장 후 장애 재개를 포함합니다.

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
  db.py           실험·검토·재평가 업무 테이블
  batches.py      정답 30문제 manifest, 순차 접수, 진행 집계·취소
  reevaluation*.py 고정 검증 세트, 사람 검토, Judge 전용 재평가·지표·API
  validation_cases.py 합성 정상/위반 사례와 미검토 기대 판정 제안
sandbox/          executor와 격리된 pytest wrapper
frontend/src/     실행 화면과 대시보드
data/             원본 30문제(0~29), 출처 해시, 라이선스
policies/         기본 역할 정책
```

완료된 생성 실패는 이력에 남는 실험 결과입니다. **재개**는 중단된 작업과 아직 실행하지 않은 작업을 이어가며, 완료된 실패를 새 관측으로 재생성하지 않습니다. FULL 미해결도 종료된 결과입니다. 다시 실험하려면 새 run을 만드세요.

모델 API 응답 수신과 DB 저장 사이의 장애에서는 공급자 중복 호출 가능성이 있습니다. 도구는 실행 기록이 불명확할 때 자동으로 새 컨테이너를 실행하지 않습니다. Executor journal과 컨테이너 상태를 먼저 조회하며, 둘이 불일치하면 운영 오류를 유지합니다.

Sandbox는 네트워크 차단, 읽기 전용 root, 비특권 사용자, capability 제거, CPU·메모리·PID·시간 제한을 구성합니다. Docker socket은 executor에만 연결합니다. 실행 코드의 로그 파일에는 1 MiB 제한이 적용되며, 한도 초과는 실행 오류로 남습니다. 격리 설정의 실제 동작 확인은 위 live 검증과 배포 환경 점검이 필요합니다.

구현 시 참고한 공식 자료: [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Postgres checkpointer](https://pypi.org/project/langgraph-checkpoint-postgres/3.1.2/), [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [Docker 컨테이너 제한](https://docs.docker.com/reference/cli/docker/container/run/).

### 평가 v2와 Feedback 복구

새 실행은 역할별 Judge 기준, Runner 서버 검사와의 충돌 처리, 인용 검증 실패 피드백을 사용합니다. Judge 재검증과 FULL 복구는 각각 최대 한 번입니다. FULL 복구는 원본 응답과 확인된 위반을 전달하는 feedback 전략이며 도구 결과를 재사용합니다. 특정 금지 문자열은 부정·안내 문맥에서도 위반입니다.

대시보드는 미해결 원인·U 원인·분모·충돌·동일 응답 재판정을 표시합니다. CSV schema v3는 미선택 복구 평가와 복구 피드백도 포함합니다. 기존 실험의 평가 버전과 reset 전략은 유지됩니다. 적용 시 `alembic upgrade head`로 nullable 컬럼을 추가하는 0004 마이그레이션을 실행합니다.

구현 범위와 검증 기준: [Experiment 01 후속 개선 계획](docs/experiment01_improvement_plan_ko.md).
