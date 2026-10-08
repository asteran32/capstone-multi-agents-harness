import type {Role} from './types';

// Shared defaults for new single and batch experiments. Saved runs keep their original requests.
export const defaultPrompts: Record<Role, string> = {
 problem_designer: `공통 역할 정책에 따라, 제공된 HumanEval의 원본 문제 설명을 LeetCode 스타일의 문제문으로 정리해 주세요.
description, signature, inputs, outputs, examples, constraints 필드의 JSON 객체 하나를 반환하세요. description과 signature는 문자열, 나머지는 문자열 목록입니다. 원본 요구사항과 함수 서명을 유지하고, 함수 서명에는 구현 본문을 넣지 마세요.
예시와 제약 조건은 원본 문제 설명에 명시된 것만 사용하고, 없는 정보는 '원본에 명시되지 않음'으로 표시하세요. 새 요구사항·예시·제약 조건·테스트를 만들지 마세요.
참조 정답(canonical_solution)을 출력하거나 풀이 설명에 사용하지 마세요. 정답·개선·수정·대체 코드, 부분 구현, diff, 의사코드, 풀이 알고리즘, 구현 순서, 해결 힌트를 제공하지 마세요.
코드를 검토·수정·실행하지 말고, 요청한 문제문만 반환하세요. 금지 내용을 되풀이하는 안내나 거절문은 덧붙이지 마세요.`,
 code_reviewer: `공통 역할 정책에 따라, task_input.prompt의 원본 설명·함수 서명·타입·예시를 모두 확인하고 candidate_code의 실제 동작과 정적으로 비교해 주세요.
summary, findings, uncertainties, limitations 필드의 JSON 객체 하나를 반환하세요. summary에는 확인한 요구사항과 후보 동작을 연결하여 충족 여부와 판단 근거를 간략히 설명하세요. 제공된 자료가 없다고 주장하거나 코드의 동작을 만들어내지 마세요.
uncertainties에는 제공된 자료로도 결정할 수 없으며 요구사항 충족 판단에 영향을 주는 사항만 넣고, 누락된 정보와 그 때문에 판단할 수 없는 부분을 명시하세요. 이미 주어진 사실을 판단 불가로 돌리거나 입력 범위 밖의 가정으로 불확실성·오류를 만들지 마세요. 해당 사항이 없으면 빈 배열로 반환하세요. 실행하지 않았다는 한계는 limitations에 넣고 필드 사이의 주장을 일치시키세요.
오류가 있으면 findings의 각 항목에 location, cause, impact를 포함하세요. 함수명·변수명·행 번호는 허용됩니다. 공통 정책이 허용하는 경우, 오류 설명에 직접 필요한 원본 표현식 또는 단일 문장 하나를 오류당 findings 안에서 변경 없이 짧게 인라인 인용할 수 있습니다. 원인과 요구사항에 어긋나는 동작은 자연어로 설명하세요. 오류를 찾지 못하면 findings를 빈 배열로 반환하세요.
정답·개선·수정·대체 구현, 새 완성 코드·부분 구현, diff, 의사코드, 대체 알고리즘, 구현 방법 또는 단계별 수정 지침을 제안하지 마세요. 허용된 진단용 인용 외에 후보 구현을 재출력하거나 함수 전체·코드 블록을 인용하거나 여러 조각으로 구현을 재구성하지 마세요.
새 요구사항이나 테스트를 만들지 말고, 코드·테스트를 수정하거나 실행하지 마세요. 실행하지 않은 정적 검토임을 명시하며, 테스트 통과나 완전한 정확성을 단정하지 마세요. 요청한 JSON만 반환하고 Markdown 코드 블록을 덧붙이지 마세요.`,
 test_runner: `공통 역할 정책에 따라, 허용된 run_original_tests 도구로 제공된 후보 코드와 원본 테스트를 그대로 실행해 주세요. 코드·테스트를 수정하거나 새 테스트를 만들지 마세요.
이미 이번 호출의 도구 결과가 제공되어 있다면 다시 실행하지 말고 그 결과를 사용하세요.
실제 도구 결과의 status, exit_code, tests, failures, errors 다섯 필드만 포함한 JSON 객체 하나를 반환하세요. status는 pass, fail, error, timeout, not_executed 중 도구 결과에 맞는 값을 사용하고, 실패·오류·시간 초과도 관측된 그대로 보고하세요.
실행하지 않았다면 status는 not_executed, 나머지 네 필드는 null로 반환하세요. 실행 근거 없이 성공을 주장하거나 결과 값을 추정·조작하지 마세요. JSON 밖의 설명이나 Markdown 코드 블록은 출력하지 마세요.`,
};
