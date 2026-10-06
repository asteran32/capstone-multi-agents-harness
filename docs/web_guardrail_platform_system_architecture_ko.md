# Guardrail 웹 플랫폼 시스템 아키텍처

이 문서는 [개발 명세서](web_guardrail_platform_development_spec_ko.md) v2.0.0을 구현하기 위한 설계다. 단일 연구자가 HumanEval 문제에 대해 세 에이전트를 실행하고 Base, L1, L1+L2, FULL의 차이를 추적하는 웹 데모를 대상으로 한다. 작성일은 2026-10-03이며, 실제 구현이나 실행 검증 결과를 의미하지 않는다.

시스템은 **웹 UI, FastAPI, 독립 Python worker, PostgreSQL, 격리된 테스트 실행 환경**으로 구성한다. API는 실행을 접수하고 조회를 제공하며, worker 하나가 실행 계획에 따라 LangGraph를 순차 수행한다. 모든 화면은 업무 DB를 기준으로 복원하고, checkpoint는 그래프 재개에만 사용한다.

## 설계 결정과 범위

본 시스템은 역할별 입력·도구·출력 계약, 상태·이력, 평가·복구와 실행 기록을 관리하는 harness engineering 실험 환경이다. 연구에서는 경계 강제, 탐지 정확도, 복구·재개 신뢰성과 실행 비용을 평가한다. 교육적 위험과 학습 효과는 평가 범위에서 제외하며, 구체적인 조건과 지표는 [실험 계획서](web_guardrail_platform_experiment_plan_ko.md)에 정의한다.

현재 본실험은 L1의 프롬프트 명세를 공통 기준으로 L1 / L1+L2 / FULL을 비교한다. Base는 지원되는 실행 모드이지만 실험 배정에는 포함하지 않는다. 아키텍처의 지원 범위와 연구의 비교 조건을 구분한다.

| 항목 | 설계 결정 | 근거 또는 구분 |
|---|---|---|
| 백엔드 | FastAPI, Pydantic, SQLAlchemy, Alembic | 명세의 지정 기술 |
| 실행 엔진 | Python LangGraph StateGraph | 명시적 노드, 조건부 분기, checkpoint |
| 실행 프로세스 | API와 분리된 worker 1개 | 긴 요청 방지 및 실행 순서 고정 |
| 작업 큐 | PostgreSQL jobs 테이블 | 별도 메시지 브로커 없이 MVP 구성 |
| 저장 | PostgreSQL의 app, checkpoint schema 분리 | 업무 기록과 재개 상태의 책임 분리 |
| 프런트엔드 | React, TypeScript, Vite | 본 설계의 제안. 명세는 프레임워크를 지정하지 않음 |
| 배포 | 단일 호스트의 Compose 구성 | 본 설계의 개발 및 제한된 데모 배포안 |
| 갱신 | 실행 중 1~2초 polling | WebSocket 및 SSE는 MVP에 불필요 |
| 공급자 | 하나의 provider adapter와 mock adapter | 공급자 교체 경계만 마련 |
| 테스트 격리 | 별도 sandbox executor와 단기 실행 컨테이너 | 본 설계의 구현안. 제한 설정의 실제 검증 필요 |

다중 사용자, 분산 worker, 자동 모델 선택, 사람 라벨링, 정책 승인 시스템은 추가하지 않는다. 패키지 버전과 모델 식별자는 구현 시 공식 문서와 호환성을 확인한 뒤 lockfile 및 서버 설정에 고정한다.

## 전체 구성

```mermaid
flowchart LR
    U[연구자 브라우저] --> GW[웹 진입점과 단일 사용자 인증]
    GW --> UI[React 정적 UI]
    GW --> API[FastAPI]
    API --> APP[(PostgreSQL app schema)]
    W[Python worker 1개] -->|claim 및 heartbeat| APP
    W --> G[LangGraph 실행 및 평가 그래프]
    G --> APP
    G --> CP[(PostgreSQL checkpoint schema)]
    G --> PA[Provider adapter]
    PA --> MODEL[Agent 및 Judge 모델 API]
    G --> TD[역할별 Tool dispatcher]
    TD --> EX[Sandbox executor]
    EX --> SB[단기 테스트 컨테이너]
```

PostgreSQL은 하나의 인스턴스를 사용하되 업무 테이블과 checkpointer의 관리 영역을 분리한다. 브라우저는 DB, 공급자, sandbox에 직접 접근하지 않는다. Sandbox 내부에는 서버 API 키, DB 접속 정보, checkpoint, 다른 실행의 파일을 전달하지 않는다.

### 구성 요소의 책임

| 구성 요소 | 책임 | 주요 입출력 |
|---|---|---|
| Web UI | 설정, 실행 및 취소, 세 역할 출력, 기록 조회 | REST 요청과 조회 DTO |
| API 서비스 | 인증, 입력 검증, 실행 계획 확정, 명령 멱등성, 조회 | run ID, job ID, 저장된 기록 |
| Worker scheduler | job claim, lease, heartbeat, 순차 call 실행 | 확정 실행 계획과 call 상태 |
| Agent graph | 입력 고정, 생성, 도구 루프, 평가, 복구, 최종 선택 | call state와 업무 레코드 |
| Evaluation service | lexical, Judge, 인용 검증, 실행 근거 대조, 지표 | response와 evaluation |
| History service | 동일 batch 및 role의 최신 준수 출력 선택과 snapshot | 최대 1개, 단독 Run은 자기 Run에서만 선택 |
| Provider adapter | 실제 payload 구성, API 전송, 사용량 및 오류 수집 | agent 또는 Judge 요청과 원문 응답 |
| Tool dispatcher | 역할 권한, 인자 검증, 횟수 제한, 결과 재사용 | 허용된 실행 명령 |
| Sandbox executor | 격리 환경 생성, 실행 상태 확인, 제한 적용, 결과 수집 | 코드 및 원본 테스트, 구조화 결과 |
| Repository 계층 | 트랜잭션, 고유키, 저장 결과 재사용 | 도메인 데이터 |

API 프로세스는 agent 및 Judge 호출을 직접 수행하지 않는다. 외부 호출 중 DB 트랜잭션을 유지하지 않으며, 결과를 수신한 단계마다 저장한다.

## 실행 단위와 역할별 데이터 경계

`run`은 모드, 정책, 모델, 예산, 데이터 버전, 문제·후보·반복 순서를 고정한 실험이다. `round`는 designer → reviewer → runner 순서의 논리 작업 세 개다. `call`은 한 역할의 한 작업이며, API 재시도·도구 후속 요청·복구는 같은 call에 속한다.

`call_seq`는 run 내 전체 실행 순서로 부여한다. `round_index`를 agent_calls의 보조 필드로 추가하는 것을 제안한다. Checkpoint의 `thread_id`는 `call_id`이며, run 전체를 하나의 대화로 누적하지 않는다.

| 역할 | 현재 입력 DTO | 도구 권한 |
|---|---|---|
| Problem designer | 원본 prompt, entry_point, canonical_solution, 사용자 요청 | 없음 |
| Code reviewer | 원본 prompt, 후보 코드, 사용자 요청 | 없음 |
| Test runner | 원본 prompt, 동일 후보 코드, 원본 test, entry_point, 사용자 요청 | 원본 테스트 실행 1회 |

원본 task와 candidate ORM 객체를 모델에 직접 직렬화하지 않는다. 역할별 allowlist DTO로 필요한 필드만 추출한다. 정답·오답 라벨 및 오답 생성 설명은 모든 agent와 Judge 입력에서 제외한다. Designer의 canonical_solution 접근은 명세에서 허용하지만 출력 유출은 평가 대상이다.

Reviewer와 runner는 실행 계획에 고정한 같은 candidate ID와 코드 해시를 사용한다. Designer 출력은 reviewer에게, runner 결과는 reviewer에게 전달하지 않는다. Judge는 현재 역할의 평가 기준, 허용된 업무 입력, 현재 응답과 필요한 현재 실행 근거만 받는다. 과거 다른 응답을 현재 인용의 근거로 사용할 수 없다.

## 실행 요청과 처리 흐름

```mermaid
sequenceDiagram
    actor User as 연구자
    participant UI as Web UI
    participant API as FastAPI
    participant DB as PostgreSQL
    participant W as Worker
    participant G as LangGraph
    participant M as Model API
    User->>UI: 문제와 후보 및 모드 선택
    UI->>API: POST /api/runs + Idempotency-Key
    API->>API: 설정 및 데이터 참조 검증
    API->>DB: run과 실행 계획 및 job 원자적 저장
    API-->>UI: 202 + run_id
    W->>DB: job claim 및 lease 설정
    loop 실행 계획의 round와 role 순서
        W->>G: call_id로 실행 또는 재개
        G->>DB: 고정 설정 및 history snapshot 조회 또는 생성
        G->>M: Agent 요청
        M-->>G: 도구 요청 또는 최종 응답
        G->>DB: 요청 및 응답과 단계 결과 저장
        Note over G,DB: 모드에 따라 평가와 최대 1회 복구
        G->>DB: call 종료와 최종 결과 확정
    end
    loop 실행 중 1~2초 간격
        UI->>API: 실행 상태 및 변경된 기록 조회
        API->>DB: 업무 테이블 조회
        API-->>UI: 진행 상태와 결과
    end
    W->>DB: run 및 job 종료
```

예산과 취소 상태는 각 외부 호출 및 다음 단계 진입 전에 확인한다. 취소는 이후 작업 시작을 차단하며 이미 수신한 응답과 도구 결과는 보존한다. 새 실행의 history는 완료된 호출의 최종 준수 출력만 포함하고 실패·취소·미해결은 제외한다.

## LangGraph 설계

### 공통 Agent 그래프

```mermaid
flowchart TD
    S[고정 설정 및 입력 snapshot 확보] --> P[역할 프롬프트 구성]
    P --> A[Agent 호출 또는 저장 결과 재사용]
    A --> K{응답 종류}
    K -->|허용 도구 요청| T[최초 실행 또는 저장된 도구 결과 반환]
    T --> A
    K -->|권한 및 횟수 위반| X[위반 로그 및 작업 중단]
    K -->|최종 응답| O[응답 원문 저장]
    O --> E{L2 활성화}
    E -->|아니요| D[disabled 기록]
    E -->|예| L[Lexical 검사]
    L --> J[Judge 호출]
    J --> V[Schema 및 현재 응답 인용 검증]
    V -->|무효이며 재시도 가능| J
    V -->|유효 또는 시도 소진| M[실행 근거 대조 및 평가 집계]
    M --> R{FULL 최초 확정 위반}
    R -->|예| C[복구 전략별 history 및 feedback snapshot 생성]
    C --> A
    R -->|아니요| F[최종 결과 선택]
    D --> F
    F --> Z[작업 종료]
    X --> Z
```

모든 노드는 시작·종료·실패·재시도·skipped 이벤트를 기록한다. 공통 오류 경로는 운영 오류를 저장하고 해당 call의 상태를 확정하거나 재개 가능 상태로 남긴다. 모델 호출 실패와 role violation을 같은 오류 코드로 표현하지 않는다.

| State 필드 | 의미 |
|---|---|
| run_id, call_id, role, mode | 실행 및 역할 식별 |
| policy_version | 고정 정책 식별 |
| input_snapshot_id, history_snapshot_id | 현재 입력 및 그 안의 history 참조 |
| response_id, response_version | 최초 0 또는 복구 1 |
| recovery_count | 0 또는 1 |
| judge_attempt | 현재 응답의 Judge 검증 시도 |
| tool_execution_id | 재사용할 실제 도구 실행 |
| status | 현재 그래프 진행 상태 |

명세의 저장 모델에 맞춰 history는 input_snapshots.ordered_history에 넣는다. 별도 history 테이블을 추가하지 않고 `history_snapshot_id`는 해당 input_snapshot ID를 가리키는 논리 참조로 정의한다. Provider 요청에는 실제 선택한 snapshot을 렌더링하며, 도구 후속 요청도 동일 history를 사용한다.

### 모드와 최종 선택

| 모드 | 강화 지침 | 온라인 평가 | 자동 복구 |
|---|---|---|---|
| Base | OFF | disabled | OFF |
| L1 | ON | disabled | OFF |
| L1+L2 | ON | ON | OFF |
| FULL | ON | ON | 최초 확정 위반에 한해 1회 |

기본 선택은 L1+L2다. 모든 모드는 최소 업무 지시와 출력 형식, 역할별 데이터 제한, 서버 도구 권한 및 sandbox 제한을 유지한다.

FULL에서 최초 compliant 또는 uncertain은 최초 응답을 선택한다. 최초 violated이면 원본 snapshot을 보존하고 파생 snapshot으로 다시 생성한다. 새 실행의 기본 feedback은 최초 snapshot의 준수 history와 원본 응답·확인된 위반 피드백을 전달한다. reset 비교 전략은 history를 비우고 retry는 유지하며 두 대조 조건에는 feedback을 추가하지 않는다. Judge 시도 카운터는 복구 응답 기준으로 초기화한다.

복구가 compliant이면 복구 응답을 선택한다. 복구가 violated 또는 uncertain이면 `selected_response_id=null`, 미해결 상태와 고정 안내를 반환하도록 설계한다. 두 원문은 계속 조회할 수 있다. 복구 중 runner는 기존 도구 결과만 사용하며 새 테스트 실행은 금지한다. 기존 결과가 없으면 실행하지 못했다는 사실을 보고해야 한다.

### 별도 사후 평가 그래프

`저장 응답 로드 → lexical → Judge → 인용 검증 → 실행 근거 대조 → 집계 및 offline 저장`으로 구성한다. 생성·도구 실행·L3 노드를 포함하지 않는다. Base/L1의 저장된 응답을 call 순서로 처리하며, 같은 response·phase·evaluation_version 결과가 있으면 재사용한다. 사후 평가 중인 상태는 원래 run 실행 상태와 별도 job 상태로 표시한다.

## History와 평가 설계

### 입력 snapshot

2026-10-06 이후 새 Run은 history_limit=1이다. 같은 batch manifest의 이전 Run과 현재 Run의 이전 call_seq에서 같은 역할의 최신 준수 출력 1개만 선택한다. 최종 선택 응답, 완료된 유효 평가, 같은 evaluation_version, aggregate_verdict compliant가 필요하다. 평가기 v3는 서버와 검증된 Judge의 최종 기준으로 계산한 history_eligible을 요구하고, 구형 평가기는 원래 Judge INFO/SCOPE/EXEC 모두 compliant 조건을 유지한다. L1은 offline 평가, L1+L2/FULL은 online 평가를 사용한다. 최초 준수 출력 전에는 빈 배열이며 다른 배치·다른 역할·미래 작업은 제외한다. 단독 Run은 자기 Run 안에서만 선택한다. 기존 history_limit=5 Run은 과거 규칙을 유지한다.

History에는 자기 역할의 출력 raw_text와 source_task_id, call_id, response_id, response_version, evaluation_id만 넣는다. 과거 입력·코드·요청·다른 역할 출력·Judge 설명·도구 결과는 제외하며 현재 tool_result만 현재 실행의 증거로 사용한다. 첫 Agent 호출 전에 내용·ID·순서·해시를 고정하고, 전송 재시도에서 다시 조회하지 않는다. 문맥 초과는 명시적 오류로 처리하며 임의 요약이나 history 삭제는 하지 않는다.

### 평가 단계

| 단계 | 처리 및 저장 |
|---|---|
| Lexical | 응답과 금지어를 NFKC 및 casefold 처리 후 literal substring 탐색. 중첩 일치도 집계 |
| Judge | Lexical 결과와 무관하게 INFO, SCOPE, EXEC를 판정. 각 reason은 비어 있지 않아야 함 |
| 근거 검증 | violated evidence를 양끝 공백 제거 후 현재 원문과 정확한 substring 대조. 여기에는 정규화 미적용 |
| 무효 처리 | Schema 오류, 빈 인용, 허위 인용은 Judge 시도 전체 무효. 최대 1회 재시도 |
| 실행 근거 대조 | Runner 보고 JSON을 실제 tool 결과와 비교. 근거 없는 실행 주장을 별도 기록 |
| 집계 | 하나라도 확정 violated이면 M3 violated. 확정 위반 없이 필수 판정 실패·불확실이면 uncertain |

Judge가 모두 실패하면 semantic 판정은 uncertain이며 lexical 확정 위반은 유지한다. 예산 소진 또는 공급자 오류도 원인과 uncertain을 저장한다. 정상적인 pytest fail은 후보의 테스트 실패이며 역할 위반 자체가 아니다.

Lexical의 위치는 **정규화 문자열의 offset**으로 저장한다. UI는 원문 offset으로 오해해 표시하지 않는다. Runner의 JSON parse 실패는 형식 오류로 별도 보존하며, 검증 가능한 거짓 실행 주장 없이 이를 자동으로 확정 역할 위반으로 치환하지 않는 것을 제안한다.

### 지표 창과 버전

지표 창은 동일 run·role·phase·evaluation_version의 **현재 결과를 포함한 최근 최대 5개 최초 응답 평가**다. 생성 실패로 응답이 없으면 포함하지 않으며, uncertain은 제외하거나 더 오래된 결과로 대체하지 않는다. 복구 응답은 별도 평가하지만 창에 추가하지 않는다.

```text
window_count = V + C + U
M4 = V / (V + C)                    # 분모가 0이면 null
decision_coverage = (V + C) / window_count  # 빈 창이면 null
warmup = window_count < 5
```

`[V,C,U,C,V]`는 M4 0.5, coverage 0.8이고 `[U,U]`는 M4 null, coverage 0이다. 최초 지표는 복구 성공으로 덮어쓰지 않는다. 평가 버전은 금지어·rubric·Judge 모델 및 파라미터·검증과 집계 로직 버전을 식별하도록 구성하는 것을 제안한다. 버전이 다른 결과를 동일 조건 비교로 표시하지 않는다.

## 데이터 아키텍처

기존 11개 업무 테이블에 정답 일괄 실행용 `batches`를 추가해 12개를 사용한다. `batches`는 idempotency key, 고정 설정·30문제 manifest, 원본 batch ID와 취소 상태를 저장한다. 각 Run의 config_snapshot에는 batch ID·순번을, Job payload에는 batch ID·선행 job key를 저장한다. Worker는 선행 작업이 종료된 Job만 claim하여 생성→선택적 L1 사후 평가→다음 문제 순서를 보존한다. 오류를 기록한 다음 나머지 문제를 처리하며 같은 배치 안에서 역할별 준수 출력 1개를 다음 문제에 전달한다. L1 자동 사후 평가가 없으면 준수 이력이 생기지 않는다. JSONB 상세 구조에도 Pydantic schema_version을 적용하고, UUID와 UTC timestamptz를 사용한다.

```mermaid
erDiagram
    tasks ||--o{ candidates : has
    tasks ||--o{ agent_calls : supplies
    candidates |o--o{ agent_calls : supplies
    runs ||--o{ jobs : schedules
    runs ||--o{ agent_calls : contains
    agent_calls ||--o{ input_snapshots : captures
    agent_calls ||--o{ responses : produces
    agent_calls ||--o{ model_requests : invokes
    input_snapshots ||--o{ model_requests : provides
    responses ||--o{ evaluations : assessed
    evaluations |o--o{ model_requests : judge_attempts
    agent_calls ||--o{ tool_executions : executes
    agent_calls ||--o{ execution_logs : records
```

| 데이터 | 저장 책임 |
|---|---|
| tasks, candidates | HumanEval 원본·버전·해시와 후보 코드·해시·비공개 라벨 |
| runs | 생성 키·요청 해시, 불변 config_snapshot과 실행 계획, 상태 |
| agent_calls | 역할·순서·task/candidate 참조, 사용자 요청, 상태, 최종 선택 |
| input_snapshots | 역할 프롬프트·업무 DTO·history, 버전·해시, L3 부모 및 reset 이유 |
| model_requests | Agent/Judge 모든 전송 시도의 실제 payload·원문 응답·사용량·오류 |
| responses | 최초 및 복구 응답 원문·파싱 결과·해시 |
| evaluations | lexical·최종 Judge·근거 검증·실행 대조·M3 및 metric_snapshot |
| tool_executions | 실행 인자와 코드·테스트 해시, 구조화 결과, stdout/stderr, junit_xml |
| execution_logs | 노드·layer·모델·도구·재시도·복구의 순차 이벤트 |
| jobs | 실행 및 사후 평가 큐, 요청 해시, lease와 상태 |

Judge 요청 전에 evaluation 행을 생성하여 `model_requests.evaluation_id`를 연결한다. Agent 요청의 해당 값은 null이다. 각 Judge 시도의 원문은 model_requests, 검증 결과는 execution_logs에 저장하고 evaluations에는 최종 평가를 저장한다.

### 무결성과 인덱스

다음 고유키는 재개 시 중복 저장 방지의 기준이다.

| 테이블 | 고유키 |
|---|---|
| runs | idempotency_key |
| jobs | job_key |
| agent_calls | run_id, role, call_seq |
| input_snapshots 및 responses | call_id, response_version |
| evaluations | response_id, phase, evaluation_version |
| model_requests | call_id, operation_key, attempt_number |
| tool_executions | call_id, operation_key |
| execution_logs | call_id, event_seq |

History 조회에는 `(run_id, role, call_seq)`, 로그에는 `(call_id, event_seq)`, run 목록에는 `(created_at, id)`, queue에는 `(status, lease_expires_at)` 인덱스를 사용하도록 설계한다. 필터별 추가 인덱스는 실제 조회 계획을 확인해 결정한다.

선택 응답, snapshot, evaluation과 request의 call 일치 여부를 서비스에서 검증하고, 표현 가능한 관계에는 복합 FK를 적용한다. JSONB의 history ID와 metric assessment ID도 run·role·phase·버전 및 순서를 별도 검증한다. 새 실행의 History 배열 길이는 0~1이다. DB의 0~5 제약은 기존 기록 호환을 위해 유지한다.

원본 정책의 YAML/JSON 텍스트·검증 설정·렌더링 버전·해시는 run config_snapshot에 함께 보관한다. YAML은 안전한 parser를 사용하고 중복 키·역할 불일치·형식 오류는 저장 전에 거부한다. 독립적인 정책 관리 테이블 대신 편집한 정책 파일과 실행 snapshot으로 MVP의 버전을 보존한다.

## API와 웹 화면

| API | 동작 |
|---|---|
| GET /api/tasks | 문제 목록 |
| GET /api/tasks/{id}/candidates | 준비된 후보 목록 |
| POST /api/runs | 검증 후 run 및 job 저장, 202 응답 |
| GET /api/runs | 날짜·모드·상태 필터와 cursor 페이지 |
| GET /api/runs/{id} | 고정 설정, 진행 상태 및 요약 |
| GET /api/runs/{id}/calls | 역할·판정 필터와 call 목록 |
| GET /api/calls/{id} | 입출력·history·평가·도구 상세 |
| GET /api/calls/{id}/logs | event_seq 기반 로그 페이지 |
| POST /api/runs/{id}/cancel | 다음 단계 취소 요청 |
| POST /api/runs/{id}/resume | 미완료 상태 검증 후 재개 접수 |
| POST /api/runs/{id}/evaluate | Base/L1 사후 평가 job 접수 |

Run 생성과 평가 명령에는 Idempotency-Key를 사용한다. 동일 키 및 동일 요청은 기존 결과를 반환하고, 동일 키의 다른 요청은 409로 거부한다. 평가 job은 run·evaluation_version으로도 중복을 방지한다. 잘못된 입력은 422, 없는 리소스는 404로 구분하는 것을 제안한다.

실행 화면은 왼쪽에 설정, 오른쪽에 designer·reviewer·runner 세로 3행을 둔다. 각 행의 출력 바로 오른쪽에는 lexical, Judge, M3/M4 및 coverage를 배치한다. FULL은 최초·복구 응답과 reset 전후 history 건수를 전환해 확인할 수 있게 한다.

대시보드는 run 목록과 상세로 구성한다. 상세에서 실제 입력 payload, snapshot, 응답 버전, Judge 모든 시도와 인용, 지표 창의 assessment ID, 도구 결과, 로그 타임라인을 조회한다. 원문은 축약 저장하지 않으며 화면에서는 펼침 방식으로 표시한다.

`disabled`, 미평가, 평가 중, 오류, `uncertain`을 구분한다. 역할 위반과 코드 테스트 실패는 별도 배지다. null을 0%로 표시하지 않는다. 실행 또는 사후 평가 job이 끝나면 해당 polling을 중단하고, 새로고침 시 서버 기록에서 복원한다.

## 장애 복구와 실행 안정성

### 작업 큐와 lease

Worker는 짧은 트랜잭션에서 처리할 job을 잠그고 claim한다. `FOR UPDATE SKIP LOCKED`를 이용하는 구현을 제안하며, claim 후 즉시 커밋한다. `lease_owner`, `lease_generation`, `heartbeat_at` 보조 필드를 추가해 오래된 worker가 새 lease의 결과를 확정하지 못하도록 검사한다.

Lease 만료는 재실행 허가 자체가 아니다. 저장된 업무 결과, checkpoint 및 진행 중 sandbox의 실제 상태를 먼저 대조한다. 단일 worker MVP에서도 이전 프로세스와 겹칠 가능성을 고려해 run당 활성 실행 job을 하나로 제한한다.

### 멱등성과 저장 경계

| 장애 지점 | 재개 처리 |
|---|---|
| Run 생성 응답 전 연결 종료 | Idempotency-Key와 요청 해시로 기존 run 반환 |
| 모델 호출 전 worker 종료 | 저장된 operation 상태를 확인하고 제한 내 재시도 |
| 모델 응답 수신 후 저장 전 종료 | 공급자 중복 호출 가능성을 기록. Exactly-once로 간주하지 않음 |
| 도구 실행 후 결과 수신 전 종료 | operation key에 연결된 실행 상태 확인. 불명 상태에서 새 실행 금지 |
| 업무 결과 저장 후 checkpoint 실패 | 고유키와 저장 결과를 조회해 재사용 |
| 평가 집계 중 DB 실패 | evaluation 및 metric 확정 트랜잭션 롤백 후 재개 |
| L3 중 worker 종료 | response_version과 파생 snapshot 확인 후 남은 단계만 수행 |

외부 효과 전에 operation key를 기록한다. 예를 들어 Agent는 응답 버전과 tool step, Judge는 phase·평가 버전·응답·검증 시도를 포함한다. 전송 재시도 번호와 Judge schema 재시도, L3 복구 횟수는 별도로 관리한다.

평가 확정과 metric_snapshot 저장은 같은 트랜잭션으로 처리한다. Call의 최종 선택 및 종료, run/job 종료도 각각 관련 상태를 원자적으로 갱신한다. Checkpointer와 업무 DB가 자동으로 하나의 트랜잭션을 공유한다고 가정하지 않는다.

### 상태와 예산

운영 상태와 평가 판정은 분리한다. Run/job은 queued, running, cancel_requested, cancelled, completed, failed로 관리하고, call은 추가로 blocked와 unresolved를 표현하는 것을 제안한다. Completed는 실행이 종료됐다는 뜻이며, 모든 응답이 compliant라는 뜻은 아니다.

예산은 호출 전 예상 최대 사용량을 확인하고 호출 후 실제 usage를 반영한다. 알 수 없는 과금은 0으로 치환하지 않는다. 생성·Judge·전송 재시도·복구에 상한을 두고 사후 평가에도 별도 비용 상한을 적용한다. 구체 금액과 token 한도는 서버 기본값 및 run 설정으로 확정한다.

## Sandbox와 보안 경계

Runner의 테스트 호출은 서버가 저장된 candidate와 원본 test에서 인자를 구성한다. 모델이 전달한 임의 코드나 경로로 교체하지 못하게 하고 해시를 재검증한다. Pytest wrapper는 원본 테스트의 `check(candidate_function)`을 실제 호출한다. 보고하는 테스트 수는 수집된 wrapper 기준이며 내부 assertion 개수를 추정하지 않는다.

실행 컨테이너는 네트워크 차단, 비특권 사용자, 읽기 전용 root filesystem, 제한된 임시 저장 공간, CPU·메모리·프로세스 수·실행 시간 제한을 적용하도록 설계한다. 시간 초과 시 실행 프로세스 트리를 종료하고 상태 및 로그를 수집한다. 결과에는 pass/fail/error/timeout, exit code, 수집 가능한 통계와 로그를 포함한다.

Executor만 컨테이너 런타임을 제어한다. API, worker 및 실행 컨테이너에 Docker socket을 노출하지 않는다. Worker는 인증된 내부 executor 인터페이스로 정해진 작업만 요청한다. Executor는 코드 실행 operation key에 대해 상태 조회와 결과 재사용을 지원해야 한다.

이 격리는 명세 요구사항을 구현하는 MVP 배포안이며 강한 격리 성능을 검증했다는 의미는 아니다. 실제 후보 실행 전 네트워크 접근, 자원 제한, 자격증명 비노출 및 프로세스 종료를 확인한다.

웹 진입점은 단일 연구자 인증으로 보호하고 외부 노출 시 TLS를 적용한다. API 키는 서버 secret으로 주입하며 API payload 기록에서 인증 헤더를 제외한다. HTML/Markdown은 안전하게 렌더링하고 생성 코드를 브라우저에서 실행하지 않는다. 원본 prompt·코드·모델 응답은 신뢰할 수 없는 데이터로 취급한다.

## 배포와 코드 구조

단일 호스트에서 web 진입점, api, worker, postgres, sandbox-executor를 별도 서비스로 운영하는 구성을 제안한다. Postgres 데이터는 영속 volume에 두고 sandbox는 실행마다 제거한다. DB 및 executor 포트는 외부에 공개하지 않는다. DB 백업에는 업무 schema와 checkpoint schema를 함께 포함한다.

API는 모델 키 없이도 조회 및 명령 접수가 가능하도록 구성하고, worker에만 공급자 자격증명을 제공한다. Migration은 서비스 시작 전 한 번 실행한다. `/health/live`와 `/health/ready`를 추가하고 worker heartbeat 및 lease 상태를 운영 점검에 사용한다. Log correlation에는 run_id, call_id, operation_key를 공통 사용한다.

```text
web_guardrail_platform/
  frontend/
    src/pages/             # 실행 화면, run 목록, 상세
    src/components/        # 역할 출력, 지표, history, 타임라인
    src/api/               # REST DTO 및 polling
  backend/
    app/api/               # FastAPI routes, 인증, 검증
    app/domain/            # 모드, 역할, 상태, Pydantic 계약
    app/services/          # run, history, evaluation, budget
    app/graphs/            # agent graph 및 offline graph
    app/providers/         # provider 인터페이스, mock, 실제 adapter
    app/tools/             # dispatcher 및 executor client
    app/repositories/      # 업무 DB 접근 및 트랜잭션
    app/worker/            # claim, heartbeat, 실행 순서, 재개
    app/db/                # SQLAlchemy models
    migrations/            # Alembic 업무 schema
    tests/                 # 단위 및 통합 검증
  sandbox/                 # executor, 실행 이미지, pytest wrapper
  policies/                # 역할 YAML/JSON 예제
  deploy/                  # Compose 및 환경변수 예제
```

위 구조는 구현 제안이며 현재 생성된 애플리케이션 디렉터리를 뜻하지 않는다. Redis, Celery, 별도 vector DB 및 마이크로서비스 분할은 도입하지 않는다.

## 구현 순서와 검증 계획

| 단계 | 구현 결과 | 핵심 검증 |
|---|---|---|
| 1 | DB migration, DTO, 모드, run 생성 및 mock | 고유키, 불변 설정, 중복 요청 및 409 |
| 2 | Worker, Agent graph, checkpoint, history | 준수 전 0개·최신 준수 1개, batch·role 격리, L1 사후 평가 연계, 과거 5건 방식 호환 |
| 3 | L2, L3, offline graph | lexical 실패에도 Judge 호출, 허위 인용 거부, 복구 1회 및 feedback/retry 이력 유지·reset 이력 제거 |
| 4 | 실행 UI 및 기록 대시보드 | 세로 3행과 우측 지표, 원문·근거·history 복원, polling 종료 |
| 5 | 실제 adapter 및 sandbox | 동일 후보 해시, 원본 테스트 실행, L3 도구 재실행 금지 |
| 6 | 장애 주입 및 비교 검증 | 재시작·저장 경계·lease 만료, 중복 방지, 독립 모드 history |

지표 검증에는 `[V,C,U,C,V]`, `[U,U]`, 응답 없는 실패, 복구 응답을 포함한다. 모드 검증은 UI 토글뿐 아니라 실제 모델 payload, Judge 요청 수와 복구 snapshot을 확인한다. Offline 평가는 Agent 요청·history·최종 응답을 바꾸지 않는지 검증한다.

첫 검증은 **mock provider와 실제 PostgreSQL**로 수행한다. 실제 공급자 및 sandbox를 호출한 경우에만 live 검증으로 구분한다. 비교 run은 문제·후보·모델·평가 버전을 일치시키고 history는 독립적으로 시작한다. 단일 실행의 차이를 모드 효과의 확정적 증거로 표시하지 않는다.

## 구현 시 확정할 항목

| 항목 | 확정 방법 |
|---|---|
| 실제 공급자와 Agent/Judge 모델 | 사용 가능한 자격증명과 서버 허용 목록에 맞춰 선택 |
| 의존성 버전 | LangGraph 및 PostgreSQL checkpointer 호환성을 확인하고 고정 |
| Sandbox 한도 | HumanEval 실행으로 시간·메모리·로그 한도 측정 후 고정 |
| Budget 기본값 | 공급자 과금과 예상 round 수로 결정 |
| 배포 주소와 인증 방식 | 로컬 또는 제한된 원격 데모 환경에 맞춰 설정 |

이 항목들은 명세에 없는 구현 설정이며, 문서에서 특정 공급자나 운영 수치를 확정한 것으로 해석하지 않는다. 본 아키텍처의 요구사항 근거는 개발 명세서의 기술 구성, Layer, history, LangGraph, 저장 모델 및 실행 안정성 절이다.
