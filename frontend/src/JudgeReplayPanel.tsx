import {useEffect, useRef, useState} from 'react';
import {api, downloadCsv} from './api';
import {Batch, Dict, names, Role} from './types';
import './reevaluation.css';

const verdicts:Record<string,string>={compliant:'C · 준수',violated:'V · 위반',uncertain:'U · 불확실',error:'평가 오류',pending:'미완료',not_applicable:'해당 없음'};
const statuses:Record<string,string>={queued:'대기',running:'실행 중',completed:'완료',failed:'실패',cancelled:'취소됨'};
const pretty=(value:unknown)=>JSON.stringify(value,null,2);
const counts=(value:Dict)=>`${value.compliant} / ${value.violated} / ${value.uncertain} / ${value.error} / ${value.pending}`;
const amount=(value:number|null|undefined)=>value==null?'미보고':value.toLocaleString();

function Judgment({value}:{value:Dict|undefined}) {
 if(!value)return <p>판정 기록 없음</p>;
 return <>{Object.entries(value).flatMap(([rule,result])=>{
  const entries=(result as Dict).criteria||[{criterion_id:rule,...result as Dict}];
  return entries.map((check:Dict)=><div key={check.criterion_id}><h4>{check.criterion_id} · {verdicts[check.verdict]||check.verdict}</h4><p>{check.reason}</p>{check.evidence&&<pre>{check.evidence_path?`${check.evidence_path}\n`:''}{check.evidence}</pre>}</div>);
 })}</>;
}

export function JudgeReplayPanel({batch,config,onInspect,onError}:{batch:Batch;config:Dict;onInspect:(id:string,callId?:string)=>void;onError:(message:string)=>void}) {
 const [open,setOpen]=useState(false),[sets,setSets]=useState<Dict[]>([]),[setId,setSetId]=useState(''),[review,setReview]=useState<Dict|null>(null);
 const [model,setModel]=useState(batch.config_snapshot.judge_model),[policySource,setPolicySource]=useState('original');
 const [budget,setBudget]=useState(360),[tokens,setTokens]=useState(batch.config_snapshot.budget?.max_output_tokens||1500);
 const [preview,setPreview]=useState<Dict|null>(null),[executionId,setExecutionId]=useState(''),[execution,setExecution]=useState<Dict|null>(null);
 const [caseId,setCaseId]=useState(''),[filter,setFilter]=useState('all'),[role,setRole]=useState('all');
 const [busy,setBusy]=useState(false),[reload,setReload]=useState(0),[showAudit,setShowAudit]=useState(false),[audit,setAudit]=useState<Dict|null>(null);
 const locked=useRef(false),keys=useRef<Record<string,string>>({});
 const settings={judge_model:model,policy_source:policySource,budget:{max_requests:budget,max_output_tokens:tokens}};
 useEffect(()=>{setPreview(null)},[model,policySource,budget,tokens,setId]);
 useEffect(()=>{
  if(!open)return;let active=true;
  api<Dict[]>(`/batches/${batch.id}/review-sets`).then(rows=>{if(active)setSets(rows.filter(row=>row.mode==='judge_replay'))}).catch(e=>{if(active)onError(String(e))});
  return()=>{active=false};
 },[open,batch.id,reload]);
 useEffect(()=>{
  if(!setId){setReview(null);setCaseId('');setExecutionId('');return}let active=true;
  api<Dict>(`/review-sets/${setId}`).then(data=>{if(active){setReview(data);setCaseId(current=>data.cases.some((c:Dict)=>c.id===current)?current:data.cases[0]?.id||'');setExecutionId(current=>data.executions.some((r:Dict)=>r.id===current)?current:data.executions[0]?.id||'')}}).catch(e=>{if(active)onError(String(e))});
  return()=>{active=false};
 },[setId,reload]);
 useEffect(()=>{
  setExecution(null);if(!executionId)return;let active=true,timer:ReturnType<typeof setTimeout>;
  async function poll(){try{const data=await api<Dict>(`/reevaluations/${executionId}`);if(!active)return;setExecution(data);if(['queued','running'].includes(data.status))timer=setTimeout(poll,2000)}catch(e){if(active){onError(String(e));timer=setTimeout(poll,5000)}}}
  poll();return()=>{active=false;clearTimeout(timer)};
 },[executionId,reload]);
 useEffect(()=>{setShowAudit(false);setAudit(null)},[caseId,executionId]);
 useEffect(()=>{
  if(!showAudit||!executionId||!caseId)return;let active=true;setAudit(null);
  api<Dict>(`/reevaluations/${executionId}/cases/${caseId}`).then(data=>{if(active)setAudit(data)}).catch(e=>{if(active)onError(String(e))});
  return()=>{active=false};
 },[showAudit,executionId,caseId,reload]);

 async function action(path:string,body:unknown,done:(data:Dict)=>void){
  if(locked.current)return;locked.current=true;setBusy(true);
  const fingerprint=path+pretty(body);keys.current[fingerprint] ||= crypto.randomUUID();
  try{const data=await api<Dict>(path,{method:'POST',headers:{'Idempotency-Key':keys.current[fingerprint]},body:JSON.stringify(body)});delete keys.current[fingerprint];done(data);setReload(n=>n+1)}
  catch(e){onError(String(e))}finally{locked.current=false;setBusy(false)}
 }
 const metrics=execution?.metrics,active=execution&&['queued','running'].includes(execution.status);
 const outcomes:Record<string,Dict>=Object.fromEntries((metrics?.items||[]).map((r:Dict)=>[r.case_id,r]));
 const results:Record<string,Dict>=Object.fromEntries((execution?.results||[]).map((r:Dict)=>[r.case_id,r]));
 const cases=(review?.cases||[]).filter((c:Dict)=>(role==='all'||role===c.role)&&(filter==='all'||(filter==='changed'&&outcomes[c.id]?.changed)||(filter==='error'&&outcomes[c.id]?.new==='error')));
 const selected=review?.cases.find((c:Dict)=>c.id===caseId);
 const frozen=execution?.config_snapshot;

 return <section className="reevaluation judge-replay" aria-label="Judge만 재평가">
  <div className="analytics-heading"><div><span className="eyebrow">FIXED RESPONSES / JUDGE</span><h2>Judge만 재평가</h2><p className="muted">세 Agent의 저장 응답과 실행 증거를 고정하고 Judge 판정의 변화를 비교합니다.</p></div><button aria-expanded={open} onClick={()=>setOpen(v=>!v)}>{open?'접기':'Judge만 재평가 열기'}</button></div>
  {open&&<>
   <p className="reeval-note">Agent 생성·테스트 실행·L3 복구는 수행하지 않습니다. 원본 배치의 판정과 history는 유지됩니다. 사람 기대 판정과 합성 사례 없이 실행합니다.</p>
   <label>고정 응답 세트<select aria-label="고정 응답 세트" value={setId} disabled={busy} onChange={e=>{setReview(null);setExecutionId('');setSetId(e.target.value)}}><option value="">새 응답 세트 준비</option>{sets.map(s=><option key={s.id} value={s.id}>{new Date(s.created_at).toLocaleString('ko-KR')} · {s.total}개 · {s.id.slice(0,8)}</option>)}</select></label>
   {!setId&&<button className="primary" disabled={busy||!['completed','completed_with_errors','cancelled'].includes(batch.status)} onClick={()=>action(`/batches/${batch.id}/review-sets`,{mode:'judge_replay'},data=>setSetId(data.id))}>저장 응답 고정 · 호출 없음</button>}
   {setId&&!review&&<p role="status">고정 응답을 불러오는 중…</p>}
   {review&&<>
    <div className="reeval-meta"><strong>저장 응답 {review.total}개 · 합성 사례 0개</strong><p>{Object.entries(review.roles).map(([r,n])=>`${names[r as Role]} ${n}개`).join(' · ')}</p><p>응답 버전 {Object.entries(review.versions).map(([v,n])=>`${v==='0'?'초기':`복구 v${v}`} ${n}개`).join(' · ')}</p><small>응답 세트 해시 {review.corpus_sha256}</small></div>
    <form className="reeval-prepare" onSubmit={e=>{e.preventDefault();setPreview(null);action(`/review-sets/${review.id}/replay-preview`,settings,setPreview)}}>
     <div className="reeval-fields"><label>Judge 모델<select aria-label="Judge 모델" value={model} disabled={busy} onChange={e=>setModel(e.target.value)}>{[...new Set([model,...config.models])].map(m=><option key={m}>{m}</option>)}</select></label>
      <label>역할 정책<select aria-label="역할 정책" value={policySource} disabled={busy} onChange={e=>setPolicySource(e.target.value)}><option value="original">원본 정책 유지 · {review.config_snapshot.policy.version}</option><option value="current">현재 default.yaml 적용</option></select></label>
      <label>전체 요청 한도<input type="number" min={review.total} max={2000} required value={budget} disabled={busy} onChange={e=>setBudget(Number(e.target.value))}/></label>
      <label>응답당 최대 출력 토큰<input type="number" min={128} max={4096} required value={tokens} disabled={busy} onChange={e=>setTokens(Number(e.target.value))}/></label></div>
     <p className="muted">새 실행은 현재 judge.yaml을 읽습니다. 현재 역할 정책까지 적용하면 정책과 Judge가 함께 변경된 비교입니다. 재시도·재개는 해당 실행에 저장된 설정을 사용합니다.</p>
     <button disabled={busy||!!active}>실행 설정 확인 · 호출 없음</button>
    </form>
    {preview&&<div className="reeval-start"><strong>{preview.total}개 고정 응답 · Judge {preview.config_snapshot.judge_model} · 정책 {preview.config_snapshot.policy.version}</strong><p>기본 {preview.base_requests}회 · 재시도 포함 최대 {preview.max_requests}회 · 요청 한도 {preview.config_snapshot.budget.max_requests}회</p>
     <p>{preview.config_snapshot.policy_changed?'역할 정책 변경 포함':'원본 역할 정책 유지'} · {model==='mock'?'Mock 연결 검증':'실제 모델 호출 시 API 사용료가 발생합니다.'}</p>
     <details><summary>실행할 정책·Judge 프롬프트 확인</summary><p>평가 버전 {preview.config_snapshot.evaluation_version}</p><h4>역할 정책</h4><pre>{preview.config_snapshot.policy_raw}</pre><h4>Judge 평가 프로필 · few-shot 포함</h4><pre>{pretty(preview.config_snapshot.evaluation_profile)}</pre></details>
     <button className="primary" disabled={busy||!!active} onClick={()=>action(`/review-sets/${review.id}/replay`,{...settings,expected_config_sha256:preview.config_sha256},data=>{setExecutionId(data.id);setPreview(null)})}>Judge 실행 · 새 평가 기록 생성</button>
    </div>}
    {!!review.executions.length&&<label>Judge 재실행 기록<select aria-label="Judge 재실행 기록" value={executionId} onChange={e=>setExecutionId(e.target.value)}>{review.executions.map((r:Dict)=><option key={r.id} value={r.id}>{new Date(r.created_at).toLocaleString('ko-KR')} · {r.id.slice(0,8)} · {r.config_snapshot.judge_model} · 정책 {r.config_snapshot.policy.version}</option>)}</select></label>}
    {execution&&metrics&&<div className="reeval-results"><h3>재평가 결과 · {statuses[execution.status]||execution.status}</h3><p role="status">{execution.finished}/{execution.total} 처리 · 평가 오류 {execution.errors}개 · 요청 {execution.requests}/{frozen.budget.max_requests}회</p><progress value={execution.finished} max={execution.total} aria-label="Judge 재평가 처리 응답 수"/>
     <p>Judge {frozen.judge_model} · 정책 {frozen.policy.version} · {frozen.policy_changed?'역할 정책 변경 포함':'원본 역할 정책 유지'}</p>
     <details><summary>이 실행에 고정된 설정</summary><pre>{pretty(frozen)}</pre></details>
     <div className="reeval-actions">{active&&<button disabled={busy||execution.cancel_requested} onClick={()=>action(`/reevaluations/${execution.id}/cancel`,{},()=>{})}>{execution.cancel_requested?'취소 요청됨':'Judge 실행 취소'}</button>}{['failed','cancelled'].includes(execution.status)&&<button disabled={busy} onClick={()=>action(`/reevaluations/${execution.id}/resume`,{},()=>{})}>미완료 재평가 재개</button>}<button onClick={async()=>{try{await downloadCsv(`/reevaluations/${execution.id}/results.csv`,`judge-replay-${execution.id}.csv`)}catch(e){onError(String(e))}}}>재평가 결과 CSV 다운로드</button></div>
     {execution.error&&<pre>{pretty(execution.error)}</pre>}
     <p>보고된 토큰: 입력 {amount(execution.usage?.input_tokens)} · 출력 {amount(execution.usage?.output_tokens)} · 합계 {amount(execution.usage?.total_tokens)}<br/>사용량 보고 {execution.usage?.reported_requests}/{execution.usage?.total_requests} 요청 · 미보고가 있으면 표시 값은 알려진 사용량의 합계입니다.</p>
     <p>판정이 바뀐 응답 {metrics.changed}/{metrics.total}개. 아래 수치는 <strong>C / V / U / 오류 / 미완료</strong> 순서입니다. 종합 판정에는 서버 검사가 포함됩니다.</p>
     <div className="table-scroll"><table><thead><tr><th>역할 · 응답 수</th><th>기존 Judge</th><th>신규 Judge</th><th>기존 종합</th><th>신규 종합</th></tr></thead><tbody>{[['전체',metrics],...Object.entries(metrics.roles).map(([r,m])=>[names[r as Role],m])].map(([name,m])=><tr key={name as string}><th>{name as string} · {(m as Dict).total}</th><td>{counts((m as Dict).judge.old)}</td><td>{counts((m as Dict).judge.new)}</td><td>{counts((m as Dict).aggregate.old)}</td><td>{counts((m as Dict).aggregate.new)}</td></tr>)}</tbody></table></div>
     <p className="muted">사람 기대 판정이 없어 정확도·오탐률·미탐률은 계산하지 않습니다. C→V 변화 자체가 개선을 뜻하지 않습니다. 원본 L3·M4와 응답의 uncertainties 개수는 바뀌지 않습니다.</p>
     <p>Schema 실패 {metrics.validation.schema_failures}/{metrics.validation.schema_checks} 검사 · 증거 실패 {metrics.validation.evidence_failures}/{metrics.validation.evidence_checks} 검사 · 검증 시도 {metrics.validation.attempts}회</p>
     <details><summary>기준별 판정 변화·실패 원인</summary><div className="table-scroll"><table><thead><tr><th>기준</th><th>동일 기준 비교 / 전체</th><th>기존 C/V/U/오류/미완료</th><th>신규 C/V/U/오류/미완료</th><th>전이</th></tr></thead><tbody>{Object.entries(metrics.criteria).map(([key,v])=>{const m=v as Dict;return <tr key={key}><th>{key}</th><td>{m.paired}/{m.total}</td><td>{counts(m.old)} · 해당 없음 {m.old.not_applicable}</td><td>{counts(m.new)} · 해당 없음 {m.new.not_applicable}</td><td>{m.transitions.map((t:Dict)=>`${verdicts[t.old]} → ${verdicts[t.new]}: ${t.count}`).join(', ')}</td></tr>})}</tbody></table></div><pre>{pretty(metrics.validation.failure_kinds)}</pre><h4>역할별 기준·판정 전이</h4><pre>{pretty(metrics.roles)}</pre></details>
    </div>}
    <div className="reeval-fields"><label>역할 필터<select aria-label="역할 필터" value={role} onChange={e=>setRole(e.target.value)}><option value="all">전체 역할</option>{Object.entries(names).map(([r,name])=><option key={r} value={r}>{name}</option>)}</select></label><label>결과 필터<select aria-label="결과 필터" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">전체 응답</option><option value="changed">판정 변경</option><option value="error">평가 오류</option></select></label></div>
    <div className="table-scroll reeval-case-list"><table><thead><tr><th>문제</th><th>역할 / 응답 버전</th><th>Judge 기존 → 신규</th><th>종합 기존 → 신규</th></tr></thead><tbody>{cases.map((c:Dict)=>{const item=outcomes[c.id];return <tr key={c.id} aria-selected={caseId===c.id}><td><button className="text-button" onClick={()=>setCaseId(c.id)}>{c.data.source_task_id}</button></td><td>{names[c.role as Role]} · v{c.data.response_version}</td><td>{item?`${verdicts[item.old_judge]} → ${verdicts[item.new_judge]}`:'실행 후 표시'}</td><td>{item?`${verdicts[item.old]} → ${verdicts[item.new]}`:'실행 후 표시'}</td></tr>})}</tbody></table></div>
    {!cases.length&&<p>필터에 해당하는 응답이 없습니다.</p>}
    {selected&&<div className="reeval-case"><h3>{selected.data.source_task_id} · {names[selected.role as Role]} · v{selected.data.response_version}</h3><h4>고정된 Agent 출력</h4><pre>{selected.data.raw_text}</pre><small>응답 해시 {selected.data.response_sha256}</small>
     <details><summary>당시 입력·실행 증거·history</summary><pre>{pretty({task_input:selected.data.task_input,user_prompt:selected.data.user_prompt,tool:selected.data.tool,history:selected.data.ordered_history,recovery_feedback:selected.data.recovery_feedback})}</pre><button onClick={()=>onInspect(selected.data.run_id,selected.data.call_id)}>원본 호출 기록 열기</button></details>
     <div className="judge-replay-pair"><div><h4>기존 Judge</h4>{selected.data.baseline?.status==='error'?<p>평가 오류 · 유효한 Judge 판정 없음</p>:<Judgment value={selected.data.baseline?.judge_result}/>}</div><div><h4>신규 Judge</h4>{results[selected.id]?.status==='error'?<p>평가 오류 · 유효한 Judge 판정 없음</p>:<Judgment value={results[selected.id]?.status==='completed'?results[selected.id].result?.judge_result:undefined}/>}</div></div>
     {execution&&<><details><summary>신규 서버 검사·schema/evidence 검증 결과</summary><pre>{pretty(results[selected.id])}</pre></details><button onClick={()=>setShowAudit(v=>!v)}>{showAudit?'요청 로그 접기':'Judge 요청·응답 로그 보기'}</button>{showAudit&&(audit?<pre>{pretty(audit.requests)}</pre>:<p role="status">요청 로그를 불러오는 중…</p>)}</>}
    </div>}
   </>}
  </>}
 </section>;
}
