import {useEffect, useRef, useState} from 'react';
import {api, downloadCsv} from './api';
import {Batch, Dict, names, Role} from './types';
import './reevaluation.css';

const verdicts:Record<string,string>={compliant:'C · 준수',violated:'V · 위반',uncertain:'U · 불확실',error:'평가 오류',pending:'미평가'};
const statuses:Record<string,string>={draft:'검토 중',ready:'검토 확정',queued:'대기',running:'실행 중',completed:'완료',failed:'실패',cancelled:'취소됨'};
const pct=(v:number|null)=>v===null?'—':`${(v*100).toFixed(1)}%`;
const pretty=(x:unknown)=>JSON.stringify(x,null,2);
const evaluationVerdict=(e:Dict|undefined)=>e?.status==='error'?'error':e?.status==='completed'?e.aggregate_verdict||'pending':'pending';
const resultVerdict=(r:Dict|undefined)=>!r||r.status==='skipped'?'pending':evaluationVerdict(r.status==='retained'?r.result:{...r.result,status:r.status});

function Scores({title,old,next}:{title:string;old:Dict;next:Dict}) {
 return <div className="reeval-scores"><h4>{title}</h4><table><thead><tr><th>지표 · 분모</th><th>기존</th><th>신규</th></tr></thead><tbody>
  <tr><th>오탐 / 기대 C</th><td>{old.fp}/{old.expected_c} · {pct(old.fpr)}</td><td>{next.fp}/{next.expected_c} · {pct(next.fpr)}</td></tr>
  <tr><th>위반 탐지 / 기대 V</th><td>{old.tp}/{old.expected_v} · {pct(old.recall)}</td><td>{next.tp}/{next.expected_v} · {pct(next.recall)}</td></tr>
  <tr><th>미탐(C로 판정) / 기대 V</th><td>{old.fn}/{old.expected_v}</td><td>{next.fn}/{next.expected_v}</td></tr>
  <tr><th>U / 전체 검토</th><td>{old.uncertain}/{old.total}</td><td>{next.uncertain}/{next.total}</td></tr>
  <tr><th>평가 오류 / 전체 검토</th><td>{old.errors}/{old.total}</td><td>{next.errors}/{next.total}</td></tr>
 </tbody></table></div>
}

export function ReevaluationPanel({batch,config,onBatch,onInspect,onError}:{batch:Batch;config:Dict;onBatch:(id:string)=>void;onInspect:(id:string,callId?:string)=>void;onError:(s:string)=>void}) {
 const [open,setOpen]=useState(false),[sets,setSets]=useState<Dict[]>([]),[setId,setSetId]=useState(''),[review,setReview]=useState<Dict|null>(null);
 const [judge,setJudge]=useState(batch.config_snapshot.judge_model),[policy,setPolicy]=useState(batch.config_snapshot.policy_raw),[budget,setBudget]=useState(1000),[tokens,setTokens]=useState(1500);
 const [busy,setBusy]=useState(false),[reload,setReload]=useState(0),[caseId,setCaseId]=useState(''),[filter,setFilter]=useState('all');
 const [taskFilter,setTaskFilter]=useState('all'),[roleFilter,setRoleFilter]=useState('all');
 const [reviewer,setReviewer]=useState(''),[expected,setExpected]=useState('compliant'),[reason,setReason]=useState(''),[confirmed,setConfirmed]=useState(false);
 const [executionId,setExecutionId]=useState(''),[execution,setExecution]=useState<Dict|null>(null),[audit,setAudit]=useState<Dict|null>(null);
 const [rounds,setRounds]=useState('1,5'),[reset,setReset]=useState(false);
 const locked=useRef(false),keys=useRef<Record<string,string>>({});
 useEffect(()=>{
  if(!open)return;let active=true;
  api<Dict[]>(`/batches/${batch.id}/review-sets`).then(rows=>{if(active)setSets(rows.filter(row=>row.mode!=='judge_replay'))}).catch(e=>{if(active)onError(String(e))});
  return()=>{active=false};
 },[open,batch.id,reload]);
 useEffect(()=>{
  setReview(null);setCaseId('');setExecutionId('');setExecution(null);setAudit(null);setConfirmed(false);setTaskFilter('all');setRoleFilter('all');setFilter('all');
  if(!setId)return;let active=true;
  api<Dict>(`/review-sets/${setId}`).then(data=>{if(active){setReview(data);setCaseId(data.cases.find((c:Dict)=>c.label.status!=='reviewed')?.id||data.cases[0]?.id||'');setExecutionId(data.executions[0]?.id||'')}}).catch(e=>{if(active)onError(String(e))});
  return()=>{active=false};
 },[setId]);
 useEffect(()=>{
  setExecution(null);setAudit(null);if(!executionId)return;let active=true,timer:ReturnType<typeof setTimeout>;
  async function poll(){try{const data=await api<Dict>(`/reevaluations/${executionId}`);if(!active)return;setExecution(data);if(['queued','running'].includes(data.status))timer=setTimeout(poll,2000)}catch(e){if(active){onError(String(e));timer=setTimeout(poll,5000)}}}
  poll();return()=>{active=false;clearTimeout(timer)};
 },[executionId,reload]);
 const selected=review?.cases.find((c:Dict)=>c.id===caseId);
 useEffect(()=>{setExpected(selected?.label.verdict||selected?.label.suggested_verdict||(selected?.data.baseline?.status==='completed'?selected.data.baseline.aggregate_verdict:null)||'compliant');setReason(selected?.label.reason||selected?.label.suggested_reason||'');setAudit(null)},[caseId,setId]);
 const results:Record<string,Dict>=Object.fromEntries((execution?.results||[]).map((r:Dict)=>[r.case_id,r]));
 const cases=(review?.cases||[]).filter((c:Dict)=>(taskFilter==='all'||c.data.source_task_id===taskFilter)&&(roleFilter==='all'||c.role===roleFilter)&&(filter==='all'||filter===c.kind||(filter==='reviewed'&&c.label.status==='reviewed')||(filter==='unreviewed'&&c.label.status!=='reviewed')||(filter==='error'&&resultVerdict(results[c.id])==='error')||(filter==='difference'&&c.label.status==='reviewed'&&resultVerdict(results[c.id])!==c.label.verdict)));
 const appliedVerdict=(c:Dict)=>results[c.id]?resultVerdict(results[c.id]):c.label.status==='reviewed'?'pending':evaluationVerdict(c.data.baseline);
 const originText=(c:Dict)=>{const status=results[c.id]?.status;return status==='retained'?'기존 Judge 유지':status==='skipped'?'기존 판정 없음 · 호출 제외':status?'재평가':c.label.status==='reviewed'?'재평가 예정':c.data.baseline?'기존 Judge 유지':'기존 판정 없음 · 호출 제외'};
 const tasks=[...new Set<string>((review?.cases||[]).map((c:Dict)=>c.data.source_task_id))];
 useEffect(()=>{if(review&&!cases.some((c:Dict)=>c.id===caseId))setCaseId(cases[0]?.id||'')},[review,taskFilter,roleFilter,filter,caseId,execution]);

 async function action(path:string,body:unknown,done:(data:Dict)=>void,method='POST'){
  if(locked.current)return;locked.current=true;setBusy(true);
  const fingerprint=path+pretty(body);keys.current[fingerprint] ||= crypto.randomUUID();
  try{const data=await api<Dict>(path,{method,headers:{'Idempotency-Key':keys.current[fingerprint]},body:JSON.stringify(body)});delete keys.current[fingerprint];done(data);setReload(x=>x+1)}
  catch(e){onError(String(e))}finally{locked.current=false;setBusy(false)}
 }
 async function saveLabel(){if(!review||!selected)return;await action(`/review-sets/${review.id}/cases/${selected.id}/label`,{revision:review.revision,verdict:expected,reason,reviewer},data=>{setReview(data);setConfirmed(false)},'PUT')}
 const active=execution&&['queued','running'].includes(execution.status);
 const conditionRounds=rounds.split(',').map(x=>Number(x.trim()));
 const validRounds=conditionRounds.length>=1&&conditionRounds.length<=3&&conditionRounds.every(x=>Number.isInteger(x)&&x>=1&&x<=20)&&new Set(conditionRounds).size===conditionRounds.length;
 const metrics=execution?.metrics;

 return <section className="reevaluation" aria-label="저장 응답 재평가">
  <div className="analytics-heading"><div><span className="eyebrow">EVALUATOR VALIDATION</span><h2>저장 응답 재평가</h2><p className="muted">응답과 실행 증거를 고정하고, 검토한 기대 판정으로 새 평가기를 검증합니다.</p></div><button aria-expanded={open} onClick={()=>setOpen(x=>!x)}>{open?'재평가 패널 닫기':'재평가 준비·기록 열기'}</button></div>
  {open&&<>
   <p className="reeval-note">① 검증 세트 준비 → ② 선택한 문제·역할의 기대 판정 저장 → ③ 선택 검토 확정 → ④ 선택 응답 재평가. 수정하지 않은 사례는 기존 Judge 판정을 유지합니다. 모든 문제를 검토할 필요는 없으며 준비와 검토에는 모델 호출이 없습니다.</p>
   <label>검증 세트<select aria-label="검증 세트" value={setId} onChange={e=>setSetId(e.target.value)}><option value="">새 검증 세트 준비</option>{sets.map(s=><option key={s.id} value={s.id}>{new Date(s.created_at).toLocaleString('ko-KR')} · {statuses[s.status]} · {s.reviewed}/{s.total} · {s.config_snapshot.judge_model}</option>)}</select></label>
   {!setId&&<form className="reeval-prepare" onSubmit={e=>{e.preventDefault();action(`/batches/${batch.id}/review-sets`,{judge_model:judge,policy_raw:policy,budget:{max_requests:budget,max_output_tokens:tokens}},data=>setSetId(data.id))}}>
    <div className="reeval-fields"><label>재평가 Judge<select aria-label="재평가 Judge" value={judge} onChange={e=>setJudge(e.target.value)}>{config.models.map((m:string)=><option key={m}>{m}</option>)}</select></label><label>재평가 전체 요청 한도<input type="number" required min={1} max={2000} value={budget} onChange={e=>setBudget(Number(e.target.value))}/></label><label>응답당 최대 출력 토큰<input type="number" required min={128} max={4096} value={tokens} onChange={e=>setTokens(Number(e.target.value))}/></label></div>
    <details><summary>역할 정책 확인·수정</summary><p>원본 정책을 기본으로 사용합니다. 정책을 바꾸면 기존 판정과의 차이를 평가기 개선만의 효과로 볼 수 없습니다.</p><label>검증 정책 YAML<textarea className="code-input" rows={16} value={policy} onChange={e=>setPolicy(e.target.value)} required/></label></details>
    <p className="muted">현재 저장된 초기·복구 응답 전체와 합성 정상·위반 사례를 고정합니다. 검증 세트 생성 후 정책·모델은 변경할 수 없습니다.</p>
    <button disabled={busy||!['completed','completed_with_errors','cancelled'].includes(batch.status)} className="primary">검증 세트 준비 · 호출 없음</button>
   </form>}
   {review&&<>
    <div className="reeval-meta"><strong>{statuses[review.status]} · 기대 판정 {review.reviewed}/{review.total} 선택 검토</strong><p>저장 응답 {review.stored}개 + 합성 사례 {review.synthetic}개 · Judge {review.config_snapshot.judge_model}</p><small>평가 버전 {review.config_snapshot.evaluation_version} · 세트 해시 {review.corpus_sha256}</small></div>
    <details><summary>고정된 역할 정책과 평가 기준</summary><pre>{review.config_snapshot.policy_raw}</pre><details><summary>평가 프로필</summary><pre>{pretty(review.config_snapshot.evaluation_profile)}</pre></details></details>
    {review.config_snapshot.policy_changed&&<p className="reeval-note">원본 정책이 변경되었습니다. 후속 비교 실험 승인 지표에는 사용할 수 없습니다.</p>}
    <p className="muted">변경할 문제와 역할을 선택해 해당 응답의 기대 판정만 저장하세요. 기대 판정을 저장한 {review.reviewed}개만 Judge를 다시 호출합니다. 수정하지 않은 사례는 기존 Judge 판정·근거·평가 버전을 유지하고, 기존 판정이 없으면 미평가로 남습니다.</p>
    <div className="reeval-fields"><label>문제 선택<select aria-label="문제 선택" value={taskFilter} onChange={e=>setTaskFilter(e.target.value)}><option value="all">전체 문제·합성 사례</option>{tasks.map(task=><option key={task} value={task}>{task}</option>)}</select></label><label>역할 선택<select aria-label="역할 선택" value={roleFilter} onChange={e=>setRoleFilter(e.target.value)}><option value="all">전체 역할</option>{Object.entries(names).map(([role,name])=><option key={role} value={role}>{name}</option>)}</select></label><label>사례 필터<select aria-label="사례 필터" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">전체</option><option value="reviewed">기대 판정 저장됨</option><option value="unreviewed">기존 Judge 유지 · 호출 제외</option><option value="stored">저장 응답</option><option value="synthetic">합성 사례</option><option value="difference">기대 판정과 다른 결과</option><option value="error">평가 오류</option></select></label></div>
    <div className="table-scroll reeval-case-list"><table><thead><tr><th>사례</th><th>역할 / 버전</th><th>기대 판정 / 유지 기준</th><th>기존 → 적용 판정</th><th>검토</th></tr></thead><tbody>{cases.map((c:Dict)=><tr key={c.id} aria-selected={c.id===caseId}><td><button className="text-button" onClick={()=>setCaseId(c.id)}>{c.data.source_task_id}</button><small>{c.kind==='stored'?'저장 응답':'합성 사례'}</small></td><td>{names[c.role as Role]}<small>{c.data.response_version===1?'복구':'초기'} {c.data.round_index?`· Round ${c.data.round_index}`:''}</small></td><td>{c.label.status==='reviewed'?verdicts[c.label.verdict]:<>{verdicts[evaluationVerdict(c.data.baseline)]}<small>기존 Judge · 사람 검토 아님</small></>}</td><td>{verdicts[evaluationVerdict(c.data.baseline)]} → {verdicts[appliedVerdict(c)]}<small>{originText(c)}</small></td><td>{c.label.status==='reviewed'?c.label.reviewer:'미검토'}</td></tr>)}</tbody></table></div>
    {!cases.length&&<p>선택 조건에 해당하는 사례가 없습니다.</p>}
    {selected&&<div className="reeval-case" aria-label="선택한 사례 검토"><h3>{selected.data.source_task_id} · {names[selected.role as Role]} · {selected.data.response_version===1?'복구 응답':'초기 응답'}</h3>
     {selected.data.title&&<p>{selected.data.title}</p>}
     <h4>판정 대상 응답</h4><pre>{selected.data.raw_text}</pre>
     <details><summary>입력·실행 증거·기존 평가 확인</summary><h4>사용자 요청</h4><pre>{selected.data.user_prompt}</pre><h4>고정 입력</h4><pre>{pretty(selected.data.task_input)}</pre><h4>실행 증거 {selected.kind==='synthetic'?'(작성된 합성 증거 · 실제 실행 아님)':''}</h4><pre>{pretty(selected.data.tool)}</pre><h4>기존 평가</h4><pre>{pretty(selected.data.baseline)}</pre><h4>당시 이력·복구 피드백</h4><pre>{pretty({history:selected.data.ordered_history,feedback:selected.data.recovery_feedback})}</pre>{selected.data.run_id&&<button onClick={()=>onInspect(selected.data.run_id,selected.data.call_id)}>원본 호출 기록 열기</button>}</details>
     {review.status==='draft'?<form onSubmit={e=>{e.preventDefault();saveLabel()}}><p className="muted">이 응답의 기대 판정을 저장하면 Judge 재평가 대상에 포함됩니다. 초기 선택값은 기존 Judge 판정 또는 합성 사례의 제안이며, 저장하기 전에는 사람 검토로 취급하지 않습니다. 판단이 모호하면 U와 근거를 기록하세요.</p><div className="reeval-fields"><label>기대 판정<select aria-label="기대 판정" value={expected} onChange={e=>setExpected(e.target.value)}>{['compliant','violated','uncertain'].map(v=><option key={v} value={v}>{verdicts[v]}</option>)}</select></label><label>검토자<input value={reviewer} onChange={e=>setReviewer(e.target.value)} maxLength={100} required/></label></div><label>판정 근거<textarea aria-label="판정 근거" value={reason} onChange={e=>setReason(e.target.value)} maxLength={5000} rows={3} required/></label><button disabled={busy||!reviewer.trim()||!reason.trim()}>선택한 사례의 기대 판정 저장</button></form>:selected.label.status==='reviewed'?<p>확정 기대 판정: {verdicts[selected.label.verdict]} · {selected.label.reviewer}<br/>{selected.label.reason}</p>:<p>수정하지 않은 사례는 새로 실행할 때 기존 Judge 판정을 그대로 유지하며 Judge를 호출하지 않습니다. 기존 판정이 없는 사례는 미평가로 남습니다. 사람 기대 판정을 사용하는 지표에서는 제외합니다.</p>}
     {execution&&<details><summary>적용 판정·검증 오류 확인</summary><p>{originText(selected)}</p><pre>{pretty(results[selected.id])}</pre><button disabled={busy} onClick={async()=>{try{setAudit(await api<Dict>(`/reevaluations/${execution.id}/cases/${selected.id}`))}catch(e){onError(String(e))}}}>이번 실행의 Judge 로그 보기</button>{audit&&<pre>{pretty(audit.requests)}</pre>}</details>}
    </div>}
    {review.status==='draft'&&<form className="reeval-seal" onSubmit={e=>{e.preventDefault();action(`/review-sets/${review.id}/seal`,{revision:review.revision,reviewer,policy_confirmed:true},setReview)}}><label className="batch-checkbox"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>역할 정책과 저장한 사례의 기대 판정만 확인했습니다.</label><label>정책 확인자<input required maxLength={100} value={reviewer} onChange={e=>setReviewer(e.target.value)}/></label><button disabled={busy||!confirmed||review.reviewed<1||!reviewer.trim()}>선택 검토 확정</button><p className="muted">저장한 {review.reviewed}개만 사람 검토로 확정합니다. 나머지 {review.total-review.reviewed}개는 Judge를 호출하지 않고 기존 판정을 유지합니다. 확정 후 기대 판정을 수정하려면 새 검증 세트를 준비하세요.</p></form>}
    <div className="reeval-start"><p>선택 응답 재평가 기본 {review.reviewed}회 · 전송·검증 재시도 포함 최대 {Math.min(review.reviewed*4,review.config_snapshot.budget.max_requests)}회 · 전체 한도 {review.config_snapshot.budget.max_requests}회. {review.config_snapshot.judge_model==='mock'?'Mock는 연결 검증용입니다.':'실제 모델 요청에는 API 사용료가 발생합니다.'}</p>{review.config_snapshot.budget.max_requests<review.reviewed&&<p>요청 한도가 선택한 사례 수보다 작습니다. 선택한 사례를 모두 평가하려면 새 검증 세트에서 한도를 높이세요.</p>}<button className="primary" disabled={busy||review.status!=='ready'||!!active} onClick={()=>action(`/review-sets/${review.id}/execute`,{},data=>{setExecutionId(data.id);setReview({...review,executions:[data,...review.executions]})})}>재평가 실행 · 새 평가 기록 생성</button></div>
    {!!review.executions.length&&<label>재평가 실행 기록<select aria-label="재평가 실행 기록" value={executionId} onChange={e=>setExecutionId(e.target.value)}>{review.executions.map((r:Dict)=><option key={r.id} value={r.id}>{new Date(r.created_at).toLocaleString('ko-KR')} · {r.id.slice(0,8)}</option>)}</select></label>}
   </>}
   {execution&&metrics&&<div className="reeval-results" aria-label="재평가 결과"><h3>재평가 결과 · {statuses[execution.status]}</h3><p role="status">{execution.evaluation_finished??execution.finished}/{execution.evaluation_total??execution.total} 재평가 · 기존 판정 유지 {execution.retained??0}개 · 기존 판정 없음 {execution.skipped??0}개 · 오류 {execution.errors}개 · 요청 {execution.requests}/{execution.config_snapshot.budget.max_requests}회{execution.cancel_requested?' · 취소 요청됨':''}</p><progress value={execution.evaluation_finished??execution.finished} max={execution.evaluation_total??execution.total} aria-label="재평가 처리 사례 수"/>
    <div className="reeval-actions">{active&&<button disabled={busy||execution.cancel_requested} onClick={()=>action(`/reevaluations/${execution.id}/cancel`,{},()=>{})}>재평가 취소</button>}{['failed','cancelled'].includes(execution.status)&&<button disabled={busy} onClick={()=>action(`/reevaluations/${execution.id}/resume`,{},()=>{})}>미완료 재평가 재개</button>}<button disabled={busy} onClick={async()=>{try{await downloadCsv(`/reevaluations/${execution.id}/results.csv`,`reevaluation-${execution.id}.csv`)}catch(e){onError(String(e))}}}>재평가 결과 CSV 다운로드</button></div>
    {execution.error&&<pre>{pretty(execution.error)}</pre>}
    <p className="reeval-note">전체 {metrics.total}개 중 사람 기대 판정 {metrics.reviewed}개로 품질 지표를 계산합니다. 미검토 {metrics.unreviewed??metrics.total-metrics.reviewed}개는 정확도·오탐·미탐 계산에서 제외합니다. 기존 판정 유지 {metrics.retained??0}개는 전체 저장 응답의 적용 판정 집계에 포함하며, 이번 Judge 호출·검증 시도에는 포함하지 않습니다.</p>
    {metrics.applied&&<p>전체 저장 응답 적용 판정 {metrics.applied.total}개: C {metrics.applied.verdicts.compliant} · V {metrics.applied.verdicts.violated} · U {metrics.applied.verdicts.uncertain} · 오류 {metrics.applied.verdicts.error} · 미평가 {metrics.applied.verdicts.pending}</p>}
    <p className="muted">같은 저장 사례 {metrics.paired.total}개 비교 · 기존 미평가/오류 {metrics.paired.excluded}개 제외. 기대 U는 이진 지표에서 제외하며 U·오류를 C로 계산하지 않습니다. 초기·복구 응답을 모두 포함하므로 원본 M4·L3 통계와 분모가 다릅니다.</p>
    <Scores title="동일 저장 사례 · 평가기 변경 비교" old={metrics.paired.old} next={metrics.paired.new}/>
    <p>검토한 합성 사례 기대 일치 {metrics.synthetic.new.matches}/{metrics.synthetic.new.total} · 검토한 저장 응답 신규 U {metrics.stored.new.uncertain}/{metrics.stored.new.total} · 오류 {metrics.stored.new.errors}/{metrics.stored.new.total}</p>
    <p>Judge schema 실패 {metrics.judge.schema_failures}/{metrics.judge.schema_checks}회 · evidence 실패 {metrics.judge.evidence_failures}/{metrics.judge.evidence_checks}회 · 검증 전체 {metrics.judge.attempts}회</p>
    <details><summary>역할별 비교·혼동 행렬·실패 원인·판정 전이</summary>{(['stored','synthetic'] as const).map(kind=><div key={kind}><h3>{kind==='stored'?'저장 응답':'합성 사례'} · 전체 검토 사례</h3>{Object.entries(metrics[kind].roles).map(([role,data])=><Scores key={role} title={names[role as Role]} old={(data as Dict).old} next={(data as Dict).new}/>)}<details><summary>혼동 행렬 (기대 판정 × 실제 판정)</summary><pre>{pretty(metrics[kind])}</pre></details></div>)}<h4>실패 원인</h4><pre>{pretty(metrics.judge.failure_kinds)}</pre><h4>전체 저장 응답 · 기존 → 적용 판정 전이 (유지 포함)</h4><pre>{pretty(metrics.transitions)}</pre></details>
    <div className="reeval-comparison"><h3>검증 후 L1 / L1+L2 / FULL 비교</h3>{metrics.promotion.ready?<p>검증 기준 충족: 오탐 비증가·탐지 유지·U 비증가·합성 사례 일치.</p>:<ul>{metrics.promotion.reasons.map((r:string)=><li key={r}>{r}</li>)}</ul>}<p className="muted">문제·후보·Agent·정책·Judge·평가 버전을 고정합니다. L1은 사후 평가를 포함합니다. 여러 Round와 FULL 복구 전략은 별도 배치로 실행합니다. reset과 retry는 피드백 없이 이력 삭제 여부만 달리합니다. 여러 번 반복해 생성 변동성을 함께 확인하세요.</p>
     <form onSubmit={e=>{e.preventDefault();action(`/reevaluations/${execution.id}/comparisons/start`,{rounds:conditionRounds,recovery_strategies:reset?['feedback','reset','retry']:['feedback']},()=>{})}}><label>Round 조건 (쉼표로 구분, 최대 3개)<input value={rounds} onChange={e=>setRounds(e.target.value)} required/></label><label className="batch-checkbox"><input type="checkbox" checked={reset} onChange={e=>setReset(e.target.checked)}/>FULL의 feedback · 이력 초기화(reset) · 이력 유지(retry)를 별도 비교</label><p>{validRounds?`${conditionRounds.length*(reset?5:3)}개 배치 · ${conditionRounds.length*(reset?5:3)*30}개 새 Run`:'1~20의 서로 다른 Round 조건을 입력하세요.'} · 새 응답 생성 및 평가 비용이 발생합니다.</p><button className="primary" disabled={busy||execution.status!=='completed'||!metrics.promotion.ready||!validRounds}>같은 평가 기준으로 비교 실험 시작</button></form>
     {execution.comparison_batches.map((g:Dict,i:number)=><div key={i} className="reeval-actions">{g.items.map((item:Dict)=><button key={item.batch_id} onClick={()=>onBatch(item.batch_id)}>{item.mode} · {item.rounds} Round{item.recovery_strategy?` · ${item.recovery_strategy}`:''} 열기</button>)}</div>)}
    </div>
   </div>}
  </>}
 </section>
}
