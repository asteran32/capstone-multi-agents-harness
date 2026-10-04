import type {EvaluationPhase, ModelRequest} from './types';
import {summarizeAgentUsage, summarizeJudgeUsage} from './tokenUsage';

const formatTokens = (value: number | null) => value === null ? '—' : value.toLocaleString('ko-KR');

function UsageSection({usage,title,scope,note,emptyLabel}: {
 usage: ReturnType<typeof summarizeAgentUsage>; title: string; scope: string; note: string; emptyLabel: string;
}) {
 const hasUsage = usage.input !== null || usage.output !== null || usage.total !== null;
 const incomplete = usage.reported < usage.requests;
 return <section className="token-usage" aria-label={title}>
  <div className="token-usage-heading"><span>{title}</span><span>{scope}</span></div>
  <dl className="token-usage-values">
   <div><dt>입력</dt><dd>{formatTokens(usage.input)}</dd></div>
   <div><dt>출력</dt><dd>{formatTokens(usage.output)}</dd></div>
   <div><dt>합계</dt><dd>{formatTokens(usage.total)}</dd></div>
  </dl>
  <p className="token-usage-note">{note}</p>
  {(!usage.requests || incomplete) && <p className="token-usage-note">
   {!usage.requests ? emptyLabel : !hasUsage ? '사용량 미수신' : `${usage.reported}/${usage.requests}회 집계 · 일부 사용량 미확인`}
  </p>}
 </section>;
}

export function TokenUsage({requests,phase = 'online',judgeDisabled = false}: {
 requests?: ModelRequest[]; phase?: EvaluationPhase; judgeDisabled?: boolean;
}) {
 return <>
  <UsageSection usage={summarizeAgentUsage(requests)} title="Agent 토큰 사용량" scope="현재 Round"
   note="최초·도구 후속·복구 합산 · Judge 제외" emptyLabel="호출 전"/>
  <UsageSection usage={summarizeJudgeUsage(requests,phase)} title="LLM Judge 토큰 사용량"
   scope={`${phase === 'offline' ? 'Offline' : 'Online'} · 현재 Round`}
   note="최초·복구 평가·재시도 합산" emptyLabel={judgeDisabled ? '평가 비활성' : '평가 호출 전'}/>
 </>;
}
