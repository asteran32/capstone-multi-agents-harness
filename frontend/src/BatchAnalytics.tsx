import {useEffect, useRef, useState, type ReactNode} from 'react';
import {MetricHelp} from './MetricHelp';
import {api} from './api';
import {Dict, names, Role, roles} from './types';
import './analytics.css';
import {causeNames,recoveryKindNames,strategyName} from './assessmentLabels';

const pct=(value:number|null|undefined)=>value==null?'—':`${(value*100).toLocaleString('ko-KR',{maximumFractionDigits:1})}%`;
const number=(value:number|null|undefined)=>value==null?'—':value.toLocaleString('ko-KR',{maximumFractionDigits:1});
const fraction=(value:Dict)=>`${number(value.numerator)} / ${number(value.denominator)}`;
const categories:Record<string,string>={violation:'최초 위반',threshold:'M4 초과',recovery:'L3 복구',judge_validation:'Judge 검증 실패',evaluation_error:'최종 평가 오류',call_error:'호출 오류',run_error:'Run 오류',job_error:'작업 오류',tool_mismatch:'실행 보고 불일치',format_error:'출력 형식 오류',server_violation:'Runner 실행 계약 위반',judgment_conflict:'서버–Judge 판정 충돌',unresolved:'미해결',uncertainty:'불확실 판정'};
const outcomes:Record<string,string>={success:'복구 성공',unresolved:'미해결',failed:'실패',running:'진행 중'};
const failureNames:Record<string,string>={schema:'Schema',evidence_missing:'필수 인용 누락',authority_conflict:'판정 권한 충돌',unexpected_evidence:'비위반 판정의 불필요한 인용',evidence_mismatch:'인용 원문 불일치',provider:'API / 전송',budget:'예산',unknown:'분류 불가'};
const stateNames:Record<string,string>={completed:'완료',failed:'실패',blocked:'차단',unresolved:'미해결',cancelled:'취소',queued:'대기',running:'실행 중',cancel_requested:'취소 중',pending:'평가 중',error:'평가 오류',not_evaluated:'미평가',not_generated:'미생성',disabled:'비활성'};
const costNames:Record<string,string>={agent:'Agent',judge_online:'Judge · Online',judge_offline:'Judge · Offline',recovery:'복구 (부분집합)',total:'전체 요청'};
const timingNames:Record<string,string>={call_ms:'역할 호출 · Online 포함',queue_ms:'Run 접수 → 첫 호출',agent_ms:'Agent 요청',judge_online_ms:'Judge · Online',judge_offline_ms:'Judge · Offline',recovery_ms:'복구 요청 (부분집합)'};

const help={
 delivery:'예정된 전체 역할 호출 중 최종 응답이 선택되고 평가가 정상 완료되어 준수(C)로 판정된 비율입니다. 미실행·실패·미선택 호출도 분모에 포함합니다.',
 violation:'위반율은 V/(V+C)입니다. V=위반, C=준수, U=판정 불확실이며 U는 분모에서 제외합니다. 최초 응답과 최종 선택 응답을 비교하며, 선택되지 않은 응답은 최종 준수로 세지 않습니다.',
 coverage:'판정 가능한 응답의 비율 (V+C)/(V+C+U)입니다. V=위반, C=준수, U=판정 불확실입니다. 미생성·미평가 호출은 이 비율의 분모에서 제외하므로 평가 완료 수를 함께 확인하세요.',
 m4:'M4는 같은 문제·역할의 최근 5개 최초 응답에 대한 위반율 V/(V+C)입니다. 현재 응답을 포함하며, 미래 위반 확률이 아닙니다. 평가창 5건이 쌓인 경우만 정식 집계합니다.',
 threshold:'정식 M4가 설정값을 엄격히 초과(>)한 관측 수입니다. 같은 값은 초과가 아닙니다. 예: 20% 설정 시 M4=40%는 초과입니다. 미설정·Warmup은 이탈 0회를 뜻하지 않습니다.',
 thresholdSetting:'M4 이탈을 분석할 기준을 0~100%로 설정합니다. 공란 저장은 해제입니다. 설정은 배치에 보존되며, L3 복구 발동 조건에는 영향을 주지 않습니다.',
 warmup:'같은 문제·역할의 평가창이 아직 5건 미만인 초기 구간입니다. 문제당 1 Round 실험은 모두 Warmup이며 정식 M4·threshold 집계에서 제외합니다. 결측은 사용할 수 있는 창 값이 없는 경우입니다.',
 episodes:'초과는 기준을 넘은 관측 수, 이탈 시작은 연속 초과 구간이 시작된 횟수입니다. 같은 문제·역할의 인접 Round만 연결하며 정상값·결측·누락 Round는 구간을 끊습니다.',
 l3:'FULL에서 최초 응답이 violated로 판정되면 최대 1회 복구합니다. 새 실행은 과거 대화를 비우고 원본 응답과 확인된 위반 근거를 전달하는 Feedback 전략입니다. 기존 실행은 Reset을 유지합니다. 성공률은 성공/종료 시도이며 진행 중은 제외합니다.',
 judge:'LLM Judge의 실패 시도 수입니다. JSON 구조·필수 인용 검증 실패와 API·예산 오류를 포함합니다. 재시도로 성공해도 앞선 실패는 남으며, 최종 평가 오류와 구분합니다.',
 callErrors:'실패는 호출 오류, 차단은 정책·예산 제한 등으로 실행이 막힌 상태입니다. 미해결은 L3 복구 후에도 준수 응답을 선택하지 못한 경우입니다.',
 tokens:'공급자가 보고한 입력·출력 토큰의 합입니다. 미보고 사용량을 0으로 추정하지 않습니다. 사용량 보고 요청 수를 함께 확인하며 Mock이 보고한 0은 실제 0으로 표시합니다.',
 m1m3:'M1은 INFO 판정입니다. 새 평가의 역할별 표는 서버와의 충돌 처리를 거친 적용 판정이며 원래 Judge 판정은 상세 기록에 보존됩니다. M3는 금지어·적용 판정·서버 검사를 합쳐 V를 우선하고 다음으로 U를 적용합니다.',
 m2:'M2는 응답에서 정책의 금지어가 일치한 횟수입니다. Unicode·대소문자 정규화 후 중첩 일치도 셉니다. 특정 문자열 자체를 금지하므로 부정·안내 문맥도 등장하면 위반입니다.',
 rules:'INFO는 금지된 정보 제공, SCOPE는 맡은 역할 범위 이탈, EXEC는 도구 사용·실행 주장 관련 규칙의 판정입니다. V=위반, C=준수, U=판정 불확실입니다.',
 windowCoverage:'각 최근 평가창 안에서 확정 판정(V 또는 C)의 비율 (V+C)/(V+C+U)입니다. Warmup 창도 포함합니다. 배치 전체 coverage와는 집계 범위가 다릅니다.',
 distribution:'유효한 관측값의 최솟값·최댓값·산술평균입니다. n은 표본 수이며 표본이 없으면 —로 표시합니다. Min/Max 값을 누르면 해당 호출을 열며 동률이면 첫 위치를 보여 줍니다.',
 firstPass:'첫 Judge 검증 시도가 통과한 평가/첫 시도 기록이 있는 평가입니다. 첫 시도 기록이 없으면 통과로 추정하지 않고 분모에서 제외합니다.',
 schema:'JSON 파싱·필수 필드·타입 등 Judge 응답 구조 검사의 실패율입니다. 분모는 실제 schema 검사를 수행한 시도이며 API·예산 오류는 제외합니다.',
 evidence:'violated 판정에 필요한 인용이 없거나 현재 응답 원문과 일치하지 않은 비율입니다. 분모는 필수 인용 검사를 수행한 시도입니다. 인용 일치가 의미적 판정 정확도를 보장하지는 않습니다.',
 retry:'실패 후 재검증에 성공한 평가 수/재시도가 기록된 평가 수입니다. 공급자 전송 재시도와 별개이며 이전 실패 이력도 보존합니다.',
 finalError:'재시도까지 마친 후에도 오류로 끝난 평가/종료된 평가입니다. 진행 중 평가는 제외하며, 실패 시도 수와 다른 단위입니다.',
 report:'최종 선택된 Runner 응답의 실행 보고가 저장된 도구 결과와 일치한 비율입니다. 코드를 실제 실행했는지, 테스트가 통과했는지와는 별개입니다.',
 executed:'원본 테스트 도구가 실제 실행을 완료한 횟수/예정된 Runner 호출 수입니다. Mock과 not_executed는 실제 실행에 포함하지 않습니다.',
 transport:'API 전송 단계에서 재시도한 요청 수입니다. Judge 응답을 다시 검증하기 위한 재요청과 구분합니다.',
 recoveryCost:'복구에 사용한 Agent·Judge 요청만 모은 부분집합입니다. 이미 Agent/Judge와 전체 합계에 포함되어 있으므로 다시 더하지 않습니다.',
 phase:'Online Judge는 L1+L2·FULL의 응답 생성 과정에서 평가합니다. Offline Judge는 L1 실행 후 저장된 응답을 평가하며 Agent 생성·도구 실행을 반복하지 않습니다.',
 median:'경과시간을 정렬했을 때 가운데 값(50번째 백분위수)입니다. 표본 수가 짝수면 가운데 두 값의 평균을 사용합니다.',
 p95:'관측한 경과시간의 95번째 백분위수입니다. 정렬된 표본 사이를 선형 보간하며 느린 호출 구간을 파악하는 값입니다. 표본이 적으면 해석에 주의하세요.',
 timing:'저장된 시작·종료 시각으로 계산한 서버 기준 경과시간(ms)입니다. 역할 호출은 Online 평가·복구를 포함하고, Run 대기는 접수부터 첫 호출까지입니다. 끝 시각이 없으면 표본에서 제외합니다.',
};

function SummaryMetric({label,description,children,onClick}:{label:string;description:string;children:ReactNode;onClick?:()=>void}) {
 return <div><MetricHelp label={label} description={description}/>{onClick?<button type="button" className="analytics-summary-action" aria-label={`${label} 기록 보기`} onClick={onClick}>{children}</button>:<div className="analytics-summary-value">{children}</div>}</div>;
}

function Location({value,open}:{value:Dict|null;open:(value:Dict)=>void}) {
 return value?<button type="button" className="text-button metric-location" onClick={()=>open(value)}>{value.source_task_id} · R{value.round_index} · {names[value.role as Role]}</button>:null;
}

function Distribution({value,percent=false,open}:{value:Dict;percent?:boolean;open:(value:Dict)=>void}) {
 const format=percent?pct:number;
 return <div className="metric-distribution"><span>Min <button type="button" disabled={!value.min_at} onClick={()=>value.min_at&&open(value.min_at)} title="최솟값 호출 기록">{format(value.min)}</button> / Max <button type="button" disabled={!value.max_at} onClick={()=>value.max_at&&open(value.max_at)} title="최댓값 호출 기록">{format(value.max)}</button></span><small>평균 {format(value.mean)} · n={value.count}</small></div>;
}

export function BatchAnalytics({batchId,onInspect}:{batchId:string;onInspect:(runId:string,callId:string)=>void}) {
 const [data,setData]=useState<Dict|null>(null),[error,setError]=useState(''),[reload,setReload]=useState(0);
 const [threshold,setThreshold]=useState(''),[revision,setRevision]=useState(0),[dirty,setDirty]=useState(false),[saving,setSaving]=useState(false);
 const [kind,setKind]=useState(''),[role,setRole]=useState(''),[query,setQuery]=useState(''),[offset,setOffset]=useState(0);
 const eventsRef=useRef<HTMLElement>(null);
 useEffect(()=>{
  let active=true,timer:ReturnType<typeof setTimeout>;
  const controller=new AbortController();
  async function tick(){
   try {
    const result=await api<Dict>(`/batches/${batchId}/analytics`,{signal:controller.signal});
    if(!active)return;
    setData(result);setError('');
    if(!['completed','completed_with_errors','cancelled'].includes(result.status))timer=setTimeout(tick,3000);
   }catch(e){if(active){setError(String(e));timer=setTimeout(tick,5000)}}
  }
  tick();return()=>{active=false;controller.abort();clearTimeout(timer)};
 },[batchId,reload]);
 useEffect(()=>{
  if(data&&!dirty){setThreshold(data.analysis_settings.m4_threshold==null?'':String(Number((data.analysis_settings.m4_threshold*100).toFixed(8))));setRevision(data.analysis_settings.revision)}
 },[data?.analysis_settings.revision,dirty]);
 const open=(value:Dict)=>onInspect(value.run_id,value.call_id);
 const showEvents=(value:string)=>{setKind(value);setOffset(0);eventsRef.current?.scrollIntoView({behavior:'smooth',block:'start'})};
 async function save(e:React.FormEvent){
  e.preventDefault();if(!data)return;
  setSaving(true);setError('');
  try {
   const settings=await api<Dict>(`/batches/${batchId}/analysis-settings`,{method:'PUT',body:JSON.stringify({m4_threshold:threshold.trim()===''?null:Number(threshold)/100,revision})});
   setData({...data,analysis_settings:settings});setDirty(false);setReload(x=>x+1);
  }catch(e){setError(String(e))}finally{setSaving(false)}
 }
 const filtered=(data?.events||[]).filter((event:Dict)=>(!kind||event.kind===kind)&&(!role||event.role===role)&&(!query||event.source_task_id.toLowerCase().includes(query.trim().toLowerCase())));
 useEffect(()=>{setOffset(old=>Math.min(old,Math.max(0,Math.floor((filtered.length-1)/15)*15)))},[filtered.length]);
 if(!data)return <section className="batch-analytics" aria-label="실험 요약"><p role="status">실험 요약을 불러오는 중…</p>{error&&<p className="error" role="alert">{error}</p>}</section>;
 const p=data.progress,t=data.threshold,j=data.judge,r=data.recovery;
 const failedAttempts=Object.values(j.failures).reduce<number>((total,n)=>total+Number(n),0);
 const thresholdText=t.value==null?'미설정':!t.eligible?'집계 대상 없음':`${t.exceedances}건`;
 return <section className="batch-analytics" aria-label="실험 요약">
  <div className="analytics-heading"><div><span className="eyebrow">BATCH ANALYSIS</span><h2>실험 요약</h2><p className="muted">{data.phase==='offline'?'Offline · L1 사후 평가':'Online · 실행 중 평가'} · 자동 판정 기준 · {['completed','completed_with_errors','cancelled'].includes(data.status)?'저장 결과':'실행 중 · 중간 결과'}</p></div><button type="button" onClick={()=>{setDirty(false);setReload(x=>x+1)}}>요약 새로고침</button></div>
  {error&&<p className="error" role="alert">{error}</p>}
  <div className="analytics-totals">
   <SummaryMetric label="준수 응답 제공률" description={help.delivery}><strong>{pct(data.compliant_delivery.rate)}</strong><small>{fraction(data.compliant_delivery)} 예정 호출</small></SummaryMetric>
   <SummaryMetric label="최초 → 최종 위반율" description={help.violation} onClick={()=>showEvents('violation')}><strong>{pct(data.initial.violation.rate)} → {pct(data.final.violation.rate)}</strong><small>V/(V+C) · {fraction(data.initial.violation)} → {fraction(data.final.violation)}</small></SummaryMetric>
   <SummaryMetric label="최초 판정 coverage" description={help.coverage}><strong>{pct(data.initial.coverage.rate)}</strong><small>{fraction(data.initial.coverage)} · 정상 평가 {fraction(p.evaluation_completion)}</small></SummaryMetric>
   <SummaryMetric label="M4 threshold 초과 관측" description="M4는 같은 문제·역할의 최근 5개 최초 응답에 대한 위반율 V/(V+C)입니다. 이 중 설정 threshold를 초과(>)한 관측 수이며, Warmup·결측은 제외합니다." onClick={()=>showEvents('threshold')}><strong>{thresholdText}</strong><small>대상 {t.eligible} · Warmup {t.warmup} · 결측 {t.missing}</small></SummaryMetric>
   <SummaryMetric label={`L3 ${r.enabled?'활성':'비활성'} · 실제 발동`} description={help.l3} onClick={()=>showEvents('recovery')}><strong>{r.triggered}회</strong><small>성공 {fraction(r.success_rate)} 종료 시도 · {pct(r.success_rate.rate)}</small></SummaryMetric>
   <SummaryMetric label="Judge 실패 시도" description={help.judge} onClick={()=>showEvents('judge_validation')}><strong>{failedAttempts} / {j.attempts||0}</strong><small>검증·API·예산 포함 · 최종 오류 {j.final_errors||0}</small></SummaryMetric>
   <SummaryMetric label="호출 실패·차단 / 미해결" description={help.callErrors} onClick={()=>showEvents('call_error')}><strong>{(p.states.failed||0)+(p.states.blocked||0)} / {p.states.unresolved||0}</strong><small>종료 후 응답 미선택 {p.terminal_unselected} · 취소 {p.states.cancelled||0}</small></SummaryMetric>
   <SummaryMetric label="미해결률" description="복구 후 준수 응답을 선택하지 못한 호출 / 전체 예정 호출입니다. 평가 오류와 의미적 불확실성을 원인별로 구분합니다." onClick={()=>showEvents('unresolved')}><strong>{pct(data.unresolved.rate.rate)}</strong><small>{fraction(data.unresolved.rate)} 예정 호출</small></SummaryMetric>
   <SummaryMetric label="Judge 최초 검증 통과율" description={help.firstPass} onClick={()=>showEvents('judge_validation')}><strong>{pct(j.first_pass.rate)}</strong><small>{fraction(j.first_pass)} 첫 시도 기록</small></SummaryMetric>
   <SummaryMetric label="서버–Judge 판정 충돌률" description="충돌이 있는 평가 / 충돌 검사를 수행한 전체 역할 평가입니다. 최초와 복구를 모두 포함합니다. 기존 버전에서 검사하지 않은 평가는 미측정으로 구분합니다. 충돌률은 사람이 확인한 오탐률과 다릅니다." onClick={()=>showEvents('judgment_conflict')}><strong>{pct(data.conflicts.rate.rate)}</strong><small>{fraction(data.conflicts.rate)} 검사 평가 · 미측정 {data.conflicts.unmeasured}</small></SummaryMetric>
   <SummaryMetric label="동일 응답 판정 변경률" description="최초·복구 응답이 동일하고 둘 다 평가가 정상 완료된 쌍 중 종합 판정이 바뀐 비율입니다. 응답 개선을 뜻하지 않으며 Judge 재시도 통과율과 다릅니다." onClick={()=>showEvents('recovery')}><strong>{pct(r.same_response_verdict_change.rate)}</strong><small>{fraction(r.same_response_verdict_change)} 동일 응답 유효 평가 쌍</small></SummaryMetric>
   <SummaryMetric label="전체 보고 토큰" description={help.tokens}><strong>{number(data.cost.total.total_tokens)}</strong><small>사용량 보고 {data.cost.total.usage_reported_requests} / {data.cost.total.request_count} 요청</small></SummaryMetric>
  </div>
  <div className="analytics-progress"><p><b>{p.planned} 예정 호출</b> · 생성 {p.generated} · 최종 선택 {p.selected}</p><p>{Object.entries(p.states).map(([key,n])=>`${stateNames[key]||key} ${n}`).join(' · ')}</p><p>최초 평가: {Object.entries(p.evaluation_states).map(([key,n])=>`${stateNames[key]||key} ${n}`).join(' · ')}</p></div>
  <div className="analytics-threshold">
   <form onSubmit={save}><div className="analytics-threshold-field"><MetricHelp label="M4 threshold (%)" description={help.thresholdSetting}/><input aria-label="M4 threshold (%)" type="number" min="0" max="100" step="any" placeholder="미설정" value={threshold} onChange={e=>{setThreshold(e.target.value);setDirty(true)}}/></div><button type="submit" disabled={saving||!dirty}>{saving?'저장 중…':'분석 설정 저장'}</button><span className="muted">Revision {data.analysis_settings.revision} · 공란 저장 시 해제</span></form>
   <p><MetricHelp label="초과·이탈 시작·연속 Round" description={help.episodes}/></p><p>초과 조건 M4 &gt; {pct(t.value)} · {t.value==null?'threshold를 설정하면 초과 관측과 이탈 사건을 집계합니다.':!t.eligible?'정식 집계 대상 없음: Warmup 또는 결측. 같은 문제·역할의 평가창 5건이 필요합니다.':`초과 ${t.exceedances}건 · 이탈 시작 ${t.episodes}회 · ${t.affected_problems}문제 · 최대 연속 ${t.longest_streak} Round`}</p>
   {t.first_at&&<p>최초 이탈 <Location value={t.first_at} open={open}/></p>}
   <p>복구 전략: {r.configured_strategies.map(strategyName).join(' · ')}</p><p className="muted">분석 설정은 L3 발동 조건에 영향을 주지 않습니다. L3는 FULL에서 최초 violated 판정일 때 최대 1회 동작합니다.</p>
  </div>
  <h3>역할별 지표</h3>
  <div className="table-scroll"><table className="analytics-role-table"><thead><tr><th>역할</th><th><MetricHelp label="M1 / M3 · 최초 판정" description={help.m1m3}/></th><th><MetricHelp label="최초 → 최종 위반율" description={help.violation}/></th><th><MetricHelp label="M2 · 금지어 일치 수" description={help.m2}/></th><th><MetricHelp label="M4 · 정식 창 위반율" description={help.m4}/></th><th><MetricHelp label="창 Decision coverage" description={help.windowCoverage}/></th></tr></thead><tbody>{data.roles.map((item:Dict)=><tr key={item.role}>
   <td><strong>{names[item.role as Role]}</strong><small>{item.planned} 예정 호출</small></td>
   <td><span>M3 · V {item.initial.V} / C {item.initial.C} / U {item.initial.U}</span><details><summary>M1(INFO) · SCOPE · EXEC</summary>{Object.entries(item.rules).map(([rule,counts])=><p key={rule}><MetricHelp label={rule} description={help.rules}/> · V {(counts as Dict).violated} / C {(counts as Dict).compliant} / U {(counts as Dict).uncertain}</p>)}</details></td>
   <td>{pct(item.initial.violation.rate)} → {pct(item.final.violation.rate)}<small>최종 V {item.final.V} / C {item.final.C} / U {item.final.U}</small><small>전체 최초 coverage {pct(item.initial.coverage.rate)}</small></td>
   <td><Distribution value={item.m2} open={open}/><small>일치 수 &gt; 0 · 문맥 예외 없음</small></td>
   <td><Distribution value={item.m4} percent open={open}/><small><MetricHelp label={`Warmup ${item.warmup} · 결측 ${item.missing_window}`} description={help.warmup}/></small></td>
   <td><Distribution value={item.coverage} percent open={open}/></td>
  </tr>)}</tbody></table></div>
  <p className="muted">Min·Max를 누르면 발생 호출을 엽니다. M4는 같은 문제·역할의 최근 5건이며 배치 전체 위반율과 다릅니다. M1·M3는 범주형 판정입니다.</p>
  <details className="analytics-detail" open><summary>미해결 및 불확실성 원인</summary><div className="analytics-detail-columns">
   <div><h3>미해결 {data.unresolved.rate.numerator} / {data.unresolved.rate.denominator} 예정 호출</h3><dl>{Object.entries(causeNames).map(([key,label])=><div key={key}><dt>{label}</dt><dd>{data.unresolved.primary_causes[key]||0}</dd></div>)}</dl><p className="muted">호출당 주원인 하나입니다. 확인된 위반 지속 → Judge 오류 → 미해소 충돌 → 실행 근거 → 의미적 불확실성 순으로 분류합니다.</p></div>
   <div><h3>U 판정 원인 · 미선택 응답 포함</h3><div className="table-scroll"><table><thead><tr><th>원인</th><th>최초 U</th><th>복구 U</th><th>호출별 최신 U</th></tr></thead><tbody>{Object.entries(causeNames).filter(([key])=>key!=='violation_persisted').map(([key,label])=><tr key={key}><td>{label}</td>{['initial','recovery','latest'].map(scope=><td key={scope}>{data.uncertainty[scope].causes[key]||0} / {data.uncertainty[scope].evaluations}</td>)}</tr>)}</tbody></table></div><p className="muted">각 열은 원인 해당 평가 / 해당 범위 U 평가입니다. 원인은 중복될 수 있습니다. 최신은 최종 선택 여부와 관계없이 마지막으로 생성된 응답입니다. 최종 선택 응답의 위반율은 미선택과 U를 분모에서 제외합니다.</p></div>
  </div></details>
  <details className="analytics-detail" open><summary>L3 복구와 Judge 검증</summary><div className="analytics-detail-columns">
   <div><h3><MetricHelp label="L3 복구 결과" description={help.l3}/></h3><dl>{(['success','unresolved','failed','running'] as const).map(key=><div key={key}><dt>{outcomes[key]}</dt><dd>{r[key]}</dd></div>)}</dl><p className="muted">성공 / 종료 시도 {fraction(r.success_rate)}. 미해결·실패도 분모에 포함하며 진행 중은 제외합니다.</p><dl>{Object.entries(recoveryKindNames).map(([key,label])=><div key={key}><dt>{label}</dt><dd>{r.success_kinds[key]||0} / {r.success} 성공</dd></div>)}</dl><p className="muted">응답 변경은 내용의 품질 개선을 보장하지 않습니다. 평가 오류 해소 {r.comparisons.error_resolved||0}회는 별도 중복 관측입니다.</p>{Object.entries(r.strategies).map(([strategy,value])=><p key={strategy}>{strategyName(strategy)} · 성공 / 종료 {fraction((value as Dict).success_rate)} · {pct((value as Dict).success_rate.rate)}</p>)}</div>
   <div><h3>Judge 검증 결과</h3><dl><div><dt><MetricHelp label="첫 시도 통과" description={help.firstPass}/></dt><dd>{pct(j.first_pass.rate)} <small>({fraction(j.first_pass)})</small></dd></div><div><dt><MetricHelp label="Schema 검사 실패" description={help.schema}/></dt><dd>{pct(j.schema_failure.rate)} <small>({fraction(j.schema_failure)})</small></dd></div><div><dt><MetricHelp label="필수 인용 검사 실패" description={help.evidence}/></dt><dd>{pct(j.evidence_failure.rate)} <small>({fraction(j.evidence_failure)})</small></dd></div><div><dt><MetricHelp label="재검증 후 성공 / 재시도 평가" description={help.retry}/></dt><dd>{j.retry_success||0} / {j.retried_evaluations||0}</dd></div><div><dt><MetricHelp label="최종 평가 오류" description={help.finalError}/></dt><dd>{pct(j.final_failure.rate)} <small>({fraction(j.final_failure)})</small></dd></div></dl>
    <p>{Object.entries(failureNames).map(([key,label])=>`${label} ${j.failures[key]||0}`).join(' · ')}</p><p className="muted">유형 분류 가능 {j.classified_attempts||0} / {j.attempts||0} 시도 · 첫 시도 기록 누락 {j.missing_first_attempts||0}. 인용 일치는 판정의 의미적 정확도를 보장하지 않습니다.</p>
   </div>
  </div></details>
  <details className="analytics-detail"><summary>실행 계약과 오류</summary><div className="analytics-detail-columns"><div><h3>원본 테스트와 보고</h3><dl><div><dt><MetricHelp label="실제 실행 / 예정 Runner" description={help.executed}/></dt><dd>{data.execution.tools.executed||0} / {data.execution.runner_planned}</dd></div><div><dt>통과 / 테스트 실패</dt><dd>{data.execution.tools.pass||0} / {data.execution.tools.fail||0}</dd></div><div><dt>실행 오류 / 시간 초과</dt><dd>{data.execution.tools.error||0} / {data.execution.tools.timeout||0}</dd></div><div><dt>미실행·Mock / 진행·미완료</dt><dd>{data.execution.tools.not_executed||0} / {data.execution.tools.running_or_incomplete||0}</dd></div><div><dt><MetricHelp label="선택 보고 일치율" description={help.report}/></dt><dd>{pct(data.execution.runner_report_match.rate)} <small>({fraction(data.execution.runner_report_match)})</small></dd></div><div><dt>미실행을 정확히 보고</dt><dd>{data.execution.matching_unexecuted}</dd></div></dl></div><div><h3>오류 기록</h3><dl><div><dt>기록된 출력 형식 오류</dt><dd>{data.execution.format_errors}</dd></div><div><dt>실패한 모델 요청 / Timeout</dt><dd>{data.execution.failed_requests} / {data.execution.timeout_requests}</dd></div>{Object.entries(data.execution.call_errors).map(([key,count])=><div key={key}><dt>{key==='ToolDenied'?'무단 도구 요청 차단':key==='BudgetExceeded'?'예산 제한':key}</dt><dd>{String(count)}</dd></div>)}</dl><p className="muted">형식 오류는 현재 검증기가 기록한 응답만 집계합니다. 코드 테스트 실패와 역할 위반은 별개입니다.</p></div></div></details>
  <details className="analytics-detail"><summary>요청·토큰·시간</summary><div className="table-scroll"><table><thead><tr><th>구분</th><th>요청 / 실패</th><th><MetricHelp label="전송 재시도" description={help.transport}/></th><th>입력</th><th>출력</th><th><MetricHelp label="합계 토큰" description={help.tokens}/></th><th><MetricHelp label="사용량 보고" description={help.tokens}/></th></tr></thead><tbody>{Object.entries(costNames).map(([key,label])=>{const item=data.cost[key];return <tr key={key}><td>{key==='recovery'?<MetricHelp label={label} description={help.recoveryCost}/>:key.startsWith('judge_')?<MetricHelp label={label} description={help.phase}/>:label}</td><td>{item.request_count} / {item.failed_requests}</td><td>{item.transport_retries}</td><td>{number(item.input_tokens)}</td><td>{number(item.output_tokens)}</td><td>{number(item.total_tokens)}</td><td>{item.usage_reported_requests} / {item.request_count}</td></tr>})}</tbody></table></div><p className="muted">복구는 Agent/Judge의 부분집합입니다. 미보고 사용량은 0으로 채우지 않으며, 알려진 토큰만 합산합니다.</p>
   <div className="table-scroll"><table><thead><tr><th><MetricHelp label="경과시간 · ms" description={help.timing}/></th><th>표본</th><th><MetricHelp label="중앙값" description={help.median}/></th><th><MetricHelp label="p95" description={help.p95}/></th><th><MetricHelp label="Min / Max / 평균" description={help.distribution}/></th></tr></thead><tbody>{Object.entries(timingNames).map(([key,label])=><tr key={key}><td>{label}</td><td>{data.timing[key].count}</td><td>{number(data.timing[key].median)}</td><td>{number(data.timing[key].p95)}</td><td><Distribution value={data.timing[key]} open={open}/></td></tr>)}</tbody></table></div><p className="muted">역할 호출 시간은 Online 평가·복구를 포함합니다. 요청 시간은 서버 로그 기준이며, 큐 대기·Offline을 별도로 표시합니다.</p>
  </details>
  <section className="analytics-events" ref={eventsRef} aria-label="분석 사건 목록"><div className="analytics-heading"><h3>이탈·복구·검증 실패 기록</h3><span>{filtered.length}건</span></div><div className="analytics-event-filters"><label>사건 유형<select aria-label="사건 유형" value={kind} onChange={e=>{setKind(e.target.value);setOffset(0)}}><option value="">전체</option>{Object.entries(categories).map(([key,label])=><option value={key} key={key}>{label} ({data.event_counts[key]||0})</option>)}</select></label><label>역할<select aria-label="역할" value={role} onChange={e=>{setRole(e.target.value);setOffset(0)}}><option value="">전체 역할</option>{roles.map(item=><option key={item} value={item}>{names[item]}</option>)}</select></label><label>문제 검색<input value={query} placeholder="HumanEval/0" onChange={e=>{setQuery(e.target.value);setOffset(0)}}/></label></div>
   <div className="table-scroll"><table><thead><tr><th>문제 / 역할 / Round</th><th>유형</th><th>결과·근거</th><th>기록</th></tr></thead><tbody>{filtered.slice(offset,offset+15).map((item:Dict)=><tr key={item.id}><td><strong>{item.source_task_id}</strong><small>{names[item.role as Role]} · R{item.round_index}</small><small>{item.phase} · {item.response_version?'복구':'최초'}</small></td><td>{categories[item.kind]}{item.failure_kind&&<small>{failureNames[item.failure_kind]} · 시도 {item.attempt}</small>}</td><td>{item.kind==='threshold'?`${pct(item.value)} > ${pct(item.threshold)} · ${item.episode_start?'새 이탈':'연속 이탈'}`:item.kind==='recovery'?`${strategyName(item.strategy)} · ${outcomes[item.outcome]} · ${item.initial_verdict||'—'} → ${item.recovery_verdict||'—'}`:item.kind==='unresolved'?causeNames[item.primary_cause]:item.kind==='uncertainty'?item.reasons.map((key:string)=>causeNames[key]||key).join(' · '):item.kind==='judgment_conflict'?`적용 판정 ${item.verdict}`:item.recovered?'재시도 후 검증 성공':null}<details><summary>상세 근거</summary><p>{item.detail}</p><pre>{JSON.stringify(item,null,2)}</pre></details></td><td><button type="button" className="text-button" onClick={()=>open(item)}>호출 기록 →</button></td></tr>)}</tbody></table></div>
   {!filtered.length&&<p className="muted">현재 조건에 해당하는 사건이 없습니다. 미평가·Warmup은 위반 없음으로 판정한 결과가 아닙니다.</p>}
   <div className="pagination"><button disabled={!offset} onClick={()=>setOffset(x=>Math.max(0,x-15))}>← 이전</button><span>{filtered.length?offset+1:0}–{Math.min(offset+15,filtered.length)} / {filtered.length}</span><button disabled={offset+15>=filtered.length} onClick={()=>setOffset(x=>x+15)}>다음 →</button></div>
  </section>
 </section>;
}
