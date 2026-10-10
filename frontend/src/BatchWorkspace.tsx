import {useEffect, useRef, useState} from 'react';
import {api, downloadCsv, post} from './api';
import {Batch, Dict, ExperimentMode, names, roles} from './types';
import {BatchAnalytics} from './BatchAnalytics';
import {ReevaluationPanel} from './ReevaluationPanel';
import {JudgeReplayPanel} from './JudgeReplayPanel';
import {defaultPrompts} from './defaultPrompts';
import {LearnedExamples} from './LearnedExamples';
import './batches.css';

const modes: ExperimentMode[] = ['L1', 'L1+L2', 'FULL'];
const finished = ['completed', 'completed_with_errors', 'cancelled'];
const labels: Record<string, string> = {queued:'대기', running:'실행 중', completed:'완료', completed_with_errors:'오류 포함 완료', failed:'실패', cancelled:'취소됨', cancel_requested:'취소 중'};
function Status({value}: {value:string}) {return <span className={'badge '+value}>{labels[value]||value}</span>}
function candidateName(label?:string) {return label==='incorrect'?'오답 후보':'정답 후보'}
type RequestKey = {fingerprint:string; key:string}|null;

export function BatchWorkspace({config, batchId, onBatch, onRun, onError}: {
 config:Dict; batchId:string; onBatch:(id:string)=>void; onRun:(id:string,callId?:string)=>void; onError:(error:string)=>void;
}) {
 const [mode,setMode]=useState<ExperimentMode>('L1'), [rounds,setRounds]=useState(1);
 const [candidateLabel,setCandidateLabel]=useState<'correct'|'incorrect'>('correct');
 const [learnedFewShot,setLearnedFewShot]=useState(true);
 const [agent,setAgent]=useState('mock'), [judge,setJudge]=useState('mock'), [budget,setBudget]=useState(1000);
 const [evaluate,setEvaluate]=useState(true), [prompts,setPrompts]=useState(()=>({...defaultPrompts})), [policy,setPolicy]=useState(config.policy_raw as string);
 const [batch,setBatch]=useState<Batch|null>(null), [archive,setArchive]=useState<{items:Batch[];total:number}>({items:[],total:0});
 const [offset,setOffset]=useState(0), [reload,setReload]=useState(0), [busy,setBusy]=useState(false), [loading,setLoading]=useState(false);
 const [rerunMode,setRerunMode]=useState<ExperimentMode>('L1+L2');
 const [downloading,setDownloading]=useState<string[]>([]);
 const downloadIds=useRef(new Set<string>());
 const createKey=useRef<RequestKey>(null), rerunKey=useRef<RequestKey>(null);

 useEffect(()=>{
  let active=true;
  api<typeof archive>(`/batches?offset=${offset}&limit=10`).then(data=>{if(active)setArchive(data)}).catch(e=>{if(active)onError(String(e))});
  return()=>{active=false};
 },[offset,batchId,reload]);
 useEffect(()=>{
  let active=true, timer:ReturnType<typeof setTimeout>;
  setBatch(null);setLoading(!!batchId);
  async function tick() {
   try {
    const data=await api<Batch>(`/batches/${batchId}`);
    if(!active)return;
    setBatch(data);setLoading(false);
    setArchive(old=>({...old,items:old.items.map(item=>item.id===data.id?data:item)}));
    if(!finished.includes(data.status))timer=setTimeout(tick,1500);
   } catch(e) {if(active){setLoading(false);onError(String(e));timer=setTimeout(tick,5000)}}
  }
  if(batchId)tick();
  return()=>{active=false;clearTimeout(timer)};
 },[batchId,reload]);
 useEffect(()=>{if(batch)setRerunMode(batch.mode==='L1'?'L1+L2':'FULL')},[batch?.id]);

 async function submit(path:string, body:unknown, ref:{current:RequestKey}) {
  const fingerprint=path+JSON.stringify(body);
  if(ref.current?.fingerprint!==fingerprint)ref.current={fingerprint,key:crypto.randomUUID()};
  setBusy(true);
  try {
   const result=await api<Batch>(path,{method:'POST',headers:{'Idempotency-Key':ref.current.key},body:JSON.stringify(body)});
   ref.current=null;setOffset(0);setReload(x=>x+1);onBatch(result.id);
  } catch(e) {onError(String(e))} finally {setBusy(false)}
 }
 const taskCount:number=config.dataset.task_count;
 const taskRange=`${config.dataset.first_task_id}–${config.dataset.last_task_id.split('/').pop()}`;
 const estimated=taskCount*rounds*(mode==='L1'?(evaluate?7:4):7);

 async function download(id:string) {
  if(downloadIds.current.has(id))return;
  downloadIds.current.add(id);setDownloading([...downloadIds.current]);
  try {await downloadCsv(`/batches/${id}/results.csv`,`guardrail-batch-${id}.csv`)}
  catch(e) {onError(String(e))}
  finally {downloadIds.current.delete(id);setDownloading([...downloadIds.current])}
 }

 return <div className="workspace batch-workspace">
  <aside className="setup">
   <div className="section-title"><span className="eyebrow">BATCH EXPERIMENT</span><h2>{taskCount}문제 일괄 실행</h2><p className="muted">{taskRange}에서 선택한 유형의 후보를 번호순으로 실행합니다. 각 역할은 이 배치에서 자신의 최신 준수 출력 1개를 이어받습니다. 첫 준수 판정 전에는 이력이 비어 있습니다.</p></div>
   <form onSubmit={e=>{e.preventDefault();submit('/batches',{mode,rounds,candidate_label:candidateLabel,agent_model:agent,judge_model:judge,prompts_by_role:prompts,policy_raw:policy,budget:{max_requests:budget},evaluate_l1:evaluate,learned_few_shot:learnedFewShot},createKey)}}>
    <label>후보 코드<select value={candidateLabel} onChange={e=>setCandidateLabel(e.target.value as 'correct'|'incorrect')}><option value="correct">정답 후보</option><option value="incorrect">오답 후보</option></select></label>
    <label>실험 모드<select value={mode} onChange={e=>setMode(e.target.value as ExperimentMode)}>{modes.map(m=><option key={m}>{m}</option>)}</select></label>
    <div className="two-fields"><label>문제당 Round<input type="number" min={1} max={20} value={rounds} onChange={e=>setRounds(Number(e.target.value))} required/></label><label>Run당 최대 API 요청<input type="number" min={1} max={2000} value={budget} onChange={e=>setBudget(Number(e.target.value))} required/></label></div>
    <label>Agent 모델<select value={agent} onChange={e=>setAgent(e.target.value)}>{config.models.map((m:string)=><option key={m}>{m}</option>)}</select></label>
    <label>Judge 모델<select value={judge} onChange={e=>setJudge(e.target.value)}>{config.models.map((m:string)=><option key={m}>{m}</option>)}</select></label>
    <label className="batch-checkbox"><input type="checkbox" checked={learnedFewShot} onChange={e=>setLearnedFewShot(e.target.checked)}/>검증된 복구 사례를 누적 few-shot으로 적용</label>
    <p className="muted">이전 실험의 사례를 역할별 최대 3개 선택합니다. 배치 시작 시 고정하며, 끄면 기본 역할 예시만 사용합니다.</p>
    <label className="batch-checkbox"><input type="checkbox" checked={evaluate} onChange={e=>setEvaluate(e.target.checked)}/>L1 응답 생성 후 자동 사후 평가</label>
    <p className="muted">L1+L2와 FULL은 온라인 평가를 사용합니다. L1 사후 평가는 Run당 동일한 별도 요청 한도를 사용합니다. L1 자동 사후 평가를 끄면 준수 이력이 쌓이지 않습니다.</p>
    <details className="requests"><summary>역할별 사용자 요청</summary>{roles.map(role=><label key={role}>{names[role]}<textarea aria-label={`${names[role]} 사용자 요청`} rows={6} value={prompts[role]} onChange={e=>setPrompts({...prompts,[role]:e.target.value})} required/></label>)}<p className="muted">공통 정책의 역할·출력 형식에 맞춘 기본 요청입니다. 정책을 수정하면 사용자 요청도 함께 확인하세요.</p></details>
    <details className="policy"><summary>공통 역할 정책</summary><label>YAML / JSON<textarea className="code-input" rows={16} value={policy} onChange={e=>setPolicy(e.target.value)} required/></label></details>
    <div className="batch-estimate"><strong>{candidateName(candidateLabel)} · {taskCount} Run · 기본 {estimated.toLocaleString()}회 요청</strong><p>복구·재시도 제외. 실제 모델 선택 시 API 사용료가 발생합니다.</p></div>
    <button className="primary start" disabled={busy||!rounds||!budget}>{busy?'접수 중…':`${taskCount}문제 일괄 실행 시작`} <span>→</span></button>
   </form>
  </aside>
  <main className="experiment">
   <div className="page-heading"><div><span className="eyebrow">HUMANEVAL / BATCH EXPERIMENT</span><h1>일괄 실험</h1><p>한 모드로 {batch?.total_runs??taskCount}문제를 실행하고, 모드를 바꾸어 같은 구성을 다시 비교합니다.</p></div>{batch&&<Status value={batch.status}/>}</div>
   {loading?<p role="status">일괄 실행을 불러오는 중…</p>:batch?<>
    <section className="batch-summary" aria-label="일괄 실행 진행 상태">
     <div><strong>{batch.finished_runs} / {batch.total_runs}</strong><span>문제 처리 · 오류 포함 {batch.error_runs}개</span></div>
     <progress value={batch.finished_runs} max={batch.total_runs} aria-label="처리된 문제 수"/>
     <p>{batch.mode} · {candidateName(batch.config_snapshot.candidate_label)} · 문제당 {batch.config_snapshot.rounds} Round · Agent {batch.config_snapshot.agent_model} · Judge {batch.config_snapshot.judge_model}</p>
     <p className="muted">{batch.config_snapshot.history_limit===1?'History: 역할별 최신 준수 출력 최대 1개 · 이 배치 안에서만 공유':'기존 History: 문제·역할별 이전 작업 최대 5개 · 문제 간 공유 없음'}</p>
     <LearnedExamples config={batch.config_snapshot}/>
     <p className="muted">{new Date(batch.created_at).toLocaleString('ko-KR')} · {batch.id.slice(0,8)}{batch.mode==='L1'&&batch.config_snapshot.evaluate_l1?' · 사후 평가 포함':''}</p>
     {batch.source_batch_id&&<button className="text-button" onClick={()=>onBatch(batch.source_batch_id!)}>원본 일괄 실행 보기 →</button>}
     {!finished.includes(batch.status)&&<button disabled={busy||batch.status==='cancel_requested'} onClick={async()=>{setBusy(true);try{await post(`/batches/${batch.id}/cancel`);setReload(x=>x+1)}catch(e){onError(String(e))}finally{setBusy(false)}}}>남은 실행 취소</button>}
    </section>
    <BatchAnalytics key={batch.id} batchId={batch.id} onInspect={onRun}/>
    <JudgeReplayPanel key={'judge-replay:'+batch.id} batch={batch} config={config} onInspect={onRun} onError={onError}/>
    <ReevaluationPanel key={'reevaluation:'+batch.id} batch={batch} config={config} onBatch={onBatch} onInspect={onRun} onError={onError}/>
    <section className="batch-rerun" aria-label="모드를 바꿔 다시 실행">
     <div><h2>같은 구성으로 다시 실행</h2><p className="muted">문제·선택한 후보·모델·정책·요청·Round와 누적 few-shot을 유지하고 빈 이력에서 다시 시작합니다. 새 복구 사례까지 반영하려면 왼쪽에서 새 일괄 실험을 시작하세요.</p></div>
     <label>새 실험 모드<select value={rerunMode} onChange={e=>setRerunMode(e.target.value as ExperimentMode)}>{modes.map(m=><option key={m}>{m}</option>)}</select></label>
     <button className="primary" disabled={busy} onClick={()=>submit(`/batches/${batch.id}/rerun`,{mode:rerunMode},rerunKey)}>이 모드로 {batch.total_runs}문제 새 실행</button>
    </section>
    <div className="table-scroll"><table><thead><tr><th>문제</th><th>생성 상태</th><th>처리된 역할 호출</th><th>사후 평가</th><th>결과</th></tr></thead><tbody>{batch.items?.map(item=><tr key={item.run_id}>
     <td><strong>{item.source_task_id}</strong><small>{item.index} / {batch.total_runs} · {candidateName(batch.config_snapshot.candidate_label)}</small></td><td><Status value={item.status}/></td><td>{item.finished_calls} / {item.total_calls}{item.has_errors&&<small>실패·미해결 {item.failed_calls} · 평가 오류 {item.evaluation_errors}</small>}</td><td>{item.offline_status?<Status value={item.offline_status}/>:<span className="muted">해당 없음</span>}</td><td><button className="text-button" onClick={()=>onRun(item.run_id)}>기록 열기 →</button></td>
    </tr>)}</tbody></table></div>
    <p className="comparison-note">브라우저를 닫아도 서버에서 계속 실행합니다. 오류는 기록하고 다음 문제로 넘어갑니다. 취소는 진행 중인 요청이 반환된 뒤 적용됩니다.</p>
   </>:<div className="empty-state"><h3>L1 기준선부터 시작하세요</h3><p>왼쪽에서 후보 코드, 모델과 Round를 설정하면 선택한 유형의 후보 {taskCount}개로 실행이 접수됩니다.</p></div>}
   <section className="batch-archive"><div className="page-heading"><h2>일괄 실행 기록</h2><span className="muted">{archive.total} batches</span></div>
    <p className="muted">결과 CSV · 문제·Round·역할별 결과와 토큰 사용량. 실행 중이면 현재 저장된 결과까지 포함합니다.</p>
    <div className="table-scroll"><table><thead><tr><th>시작 시각</th><th>모드</th><th>모델</th><th>진행</th><th>상태</th><th></th></tr></thead><tbody>{archive.items.map(item=><tr key={item.id}>
     <td>{new Date(item.created_at).toLocaleString('ko-KR')}<small>{item.id.slice(0,8)}</small></td><td>{item.mode}<small>{candidateName(item.config_snapshot.candidate_label)}</small></td><td>{item.config_snapshot.agent_model}<small>Judge · {item.config_snapshot.judge_model}</small></td><td>{item.finished_runs} / {item.total_runs}</td><td><Status value={item.status}/></td><td><div className="batch-row-actions"><button className="text-button" onClick={()=>onBatch(item.id)}>열기 →</button><button className="text-button" disabled={downloading.includes(item.id)} aria-busy={downloading.includes(item.id)} onClick={()=>download(item.id)}>{downloading.includes(item.id)?'다운로드 중…':'결과 다운로드'}</button></div></td>
    </tr>)}</tbody></table></div>
    {!archive.items.length&&<p className="muted">저장된 일괄 실행이 없습니다.</p>}
    <div className="pagination"><button disabled={!offset} onClick={()=>setOffset(x=>x-10)}>← 이전</button><button disabled={offset+10>=archive.total} onClick={()=>setOffset(x=>x+10)}>다음 →</button></div>
   </section>
  </main>
 </div>
}
