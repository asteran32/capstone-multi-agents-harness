export const causeNames:Record<string,string>={
 violation_persisted:'확인된 위반 지속', semantic_uncertainty:'의미적 불확실성',
 server_check_uncertainty:'서버 형식 검증 불확실',
 judge_error:'Judge 평가 오류', execution_evidence:'실행 근거 불충분',
 judgment_conflict:'미해소 판정 충돌', unknown:'분류 불가',
};
export const recoveryKindNames:Record<string,string>={
 changed_response:'응답 변경 후 준수', same_response_rejudged:'동일 응답 재판정 후 준수',
 evaluation_error_resolved:'동일 응답 평가 오류 해소 후 준수',
};
export const strategyName=(strategy:string)=>strategy==='feedback'?'Feedback':strategy==='reset'?'Reset (이력 초기화)':strategy==='retry'?'Retry (이력 유지)':strategy;
