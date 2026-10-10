import {useEffect, useRef, useState} from 'react';
import type {KeyboardEvent} from 'react';
import {api} from './api';
import {names, roles, terminal} from './types';
import {MetricHelp} from './MetricHelp';
import type {DesignerStatement, LiveAgent, LiveFraction, LiveSession, LiveSettings, ReviewerReport} from './liveTypes';
import './live.css';

const statusNames:Record<string,string>={queued:'대기 중',running:'실행 중',completed:'완료',failed:'실패',blocked:'차단',unresolved:'미해결',cancelled:'취소됨',cancel_requested:'취소 중',compliant:'준수',violated:'위반',uncertain:'불확실',pass:'통과',fail:'실패',error:'오류',timeout:'시간 초과',not_executed:'미실행'};
const percent=(value:number|null|undefined)=>value==null?'—':`${Math.round(value*100)}%`;
function saved(key:string) {try{return sessionStorage.getItem(key)}catch{return null}}
function save(key:string,value:string) {try{sessionStorage.setItem(key,value)}catch{/* Editing remains available if storage is full. */}}
function initialSettings(models:string[]):LiveSettings {
 try {
  const value=JSON.parse(saved('guardrail-live-settings')||'{}');
  return {mode:value.mode==='L1+L2'?'L1+L2':'FULL',agent_model:models.includes(value.agent_model)?value.agent_model:'mock',judge_model:models.includes(value.judge_model)?value.judge_model:'mock'};
 } catch {return {mode:'FULL',agent_model:'mock',judge_model:'mock'}}
}
function Status({value}:{value?:string|null}) {return <span className={'badge '+(value||'pending')}>{value?statusNames[value]||value:'미평가'}</span>}
function Rate({value}:{value?:LiveFraction}) {return <><strong>{percent(value?.rate)}</strong><small>{value&&value.denominator?`${value.numerator} / ${value.denominator}`:'관측 없음'}</small></>}

function AgentState({agent,waiting}:{agent?:LiveAgent;waiting:string}) {
 if(agent?.withheld)return <p className="live-result-notice">역할 준수 평가를 통과하지 못해 응답을 표시하지 않습니다. 상세 기록에서 판정을 확인하세요.</p>;
 if(agent?.error)return <p className="error" role="alert">{agent.error.message||agent.error.code}</p>;
 const pending=agent&&!terminal.includes(agent.status);
 return <div className="live-placeholder" role="status">{pending&&<span className="live-working"/>}<p>{pending?'에이전트가 처리하고 있습니다.':waiting}</p></div>;
}

function Reviewer({agent}:{agent?:LiveAgent}) {
 const report=agent?.response?.parsed_output as ReviewerReport|undefined;
 if(!report)return <AgentState agent={agent} waiting="Submit을 누르면 코드 검토가 시작됩니다."/>;
 return <div className="live-review live-reveal" key={agent!.id}>
  <p>{report.summary}</p>
  {report.findings.length?<ol className="live-findings">{report.findings.map((finding,index)=><li key={index}><strong>{finding.location}</strong><p>{finding.cause}</p><p className="muted">{finding.impact}</p></li>)}</ol>:<p className="live-no-findings">정적 검토에서 확인된 오류 없음</p>}
  {report.uncertainties.length>0&&<details><summary>판단 불가 사항 {report.uncertainties.length}개</summary><ul>{report.uncertainties.map((item,index)=><li key={index}>{item}</li>)}</ul></details>}
  {report.limitations&&<p className="live-limitations">{report.limitations}</p>}
 </div>;
}

function Runner({agent,sandbox}:{agent?:LiveAgent;sandbox?:string}) {
 const result=agent?.tool_result;
 if(!result)return <AgentState agent={agent} waiting="Submit을 누르면 고정 테스트를 실행합니다."/>;
 return <div className="live-runner live-reveal" key={agent!.id}>
  <div className="live-test-outcome"><Status value={result.status}/><span>{result.status==='not_executed'?'코드가 실행되지 않았습니다.':'격리 환경의 실제 도구 결과'}</span></div>
  <dl className="live-test-counts"><div><dt>테스트</dt><dd>{result.tests??'—'}</dd></div><div><dt>실패</dt><dd>{result.failures??'—'}</dd></div><div><dt>오류</dt><dd>{result.errors??'—'}</dd></div><div><dt>종료 코드</dt><dd>{result.exit_code??'—'}</dd></div></dl>
  <p className="live-limitations">{sandbox==='mock'?'Mock sandbox에서는 테스트를 실행하지 않습니다.': '테스트 수는 고정 테스트 묶음 기준입니다. 코드의 테스트 결과와 에이전트의 역할 준수 판정은 별개입니다.'}</p>
  {agent?.withheld&&<p className="live-result-notice">Runner 보고가 역할 평가를 통과하지 못했습니다. 위 수치는 서버에 저장된 도구 결과입니다.</p>}
  {!agent?.response&&!agent?.withheld&&<AgentState agent={agent} waiting="Runner 보고를 기다립니다."/>}
 </div>;
}

export function LiveWorkspace({models,sessionId,onSession,onRun}:{models:string[];sessionId:string;onSession:(id:string)=>void;onRun:(id:string,callId?:string)=>void}) {
 const [settings,setSettings]=useState<LiveSettings>(()=>initialSettings(models));
 const [session,setSession]=useState<LiveSession|null>(null),[draft,setDraft]=useState({id:'',code:''});
 const [creating,setCreating]=useState(false),[submitting,setSubmitting]=useState(false),[error,setError]=useState('');
 const [revision,setRevision]=useState(0),[retry,setRetry]=useState(0),[viewSubmission,setViewSubmission]=useState('');
 const createRequest=useRef<{fingerprint:string;key:string}|null>(null),submitRequest=useRef<{fingerprint:string;key:string}|null>(null);
 const editor=useRef<HTMLTextAreaElement>(null),gutter=useRef<HTMLDivElement>(null),escapeTab=useRef(false),locked=useRef(false);
 const mounted=useRef(true);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[]);
 useEffect(()=>save('guardrail-live-settings',JSON.stringify(settings)),[settings]);

 async function requestSession(previous?:string) {
  const body={...settings,...(previous?{previous_session_id:previous}:{})};
  const fingerprint=JSON.stringify(body);
  if(createRequest.current?.fingerprint!==fingerprint)createRequest.current={fingerprint,key:crypto.randomUUID()};
  return api<LiveSession>('/live/sessions',{method:'POST',headers:{'Idempotency-Key':createRequest.current.key},body:JSON.stringify(body)});
 }
 useEffect(()=>{
  if(sessionId)return;
  let active=true;
  setCreating(true);setError('');
  requestSession().then(data=>{if(active){createRequest.current=null;setSession(data);onSession(data.id)}})
   .catch(e=>{if(active)setError(String(e))}).finally(()=>{if(active)setCreating(false)});
  return()=>{active=false};
 },[sessionId,retry]);
 useEffect(()=>{
  if(!sessionId)return;
  let active=true,timer:ReturnType<typeof setTimeout>;
  setCreating(false);setSession(old=>old?.id===sessionId?old:null);
  async function tick() {
   try {
    const data=await api<LiveSession>(`/live/sessions/${sessionId}${viewSubmission?'?submission_id='+viewSubmission:''}`);
    if(!active)return;
    setSession(data);
    if(data.status==='generating'||data.submissions.some(item=>!terminal.includes(item.status)))timer=setTimeout(tick,1200);
   } catch(e){if(active){setError(String(e));timer=setTimeout(tick,5000)}}
  }
  tick();return()=>{active=false;clearTimeout(timer)};
 },[sessionId,viewSubmission,revision]);
 useEffect(()=>{
  if(!session)return;
  setDraft({id:session.id,code:saved('guardrail-live-draft:'+session.id)??session.submission?.code??session.problem.starter_code});
 },[session?.id]);

 const code=draft.id===session?.id?draft.code:'';
 const pending=!!session?.submissions.some(item=>!terminal.includes(item.status));
 const busy=creating||submitting||pending||session?.status==='generating';
 const canSubmit=!!session&&session.status==='ready'&&!busy&&!!code.trim()&&code.length<=20000;
 const statement=session?.designer.response?.parsed_output as DesignerStatement|undefined;
 const changedSettings=!!session&&(['mode','agent_model','judge_model'] as const).some(key=>session.settings[key]!==settings[key]);

 function edit(value:string) {
  if(!session)return;
  setDraft({id:session.id,code:value});save('guardrail-live-draft:'+session.id,value);
 }
 async function newProblem() {
  if(locked.current||busy)return;
  locked.current=true;setCreating(true);setError('');
  try {
   const data=await requestSession(sessionId||undefined);
   if(!mounted.current)return;
   createRequest.current=null;setViewSubmission('');setSession(data);onSession(data.id);
  }catch(e){if(mounted.current)setError(String(e))}
  finally{locked.current=false;if(mounted.current)setCreating(false)}
 }
 async function submit() {
  if(!canSubmit||locked.current||!session)return;
  locked.current=true;setSubmitting(true);setError('');
  const fingerprint=session.id+'\n'+code;
  if(submitRequest.current?.fingerprint!==fingerprint)submitRequest.current={fingerprint,key:crypto.randomUUID()};
  try {
   const data=await api<LiveSession>(`/live/sessions/${session.id}/submissions`,{method:'POST',headers:{'Idempotency-Key':submitRequest.current.key},body:JSON.stringify({code})});
   if(!mounted.current)return;
   submitRequest.current=null;setViewSubmission('');setSession(data);
  }catch(e){if(mounted.current)setError(String(e))}
  finally{locked.current=false;if(mounted.current){setSubmitting(false);setRevision(value=>value+1)}}
 }
 function editorKey(event:KeyboardEvent<HTMLTextAreaElement>) {
  if((event.metaKey||event.ctrlKey)&&event.key==='Enter'){event.preventDefault();void submit();return}
  if(event.key==='Escape'){escapeTab.current=true;return}
  if(event.key!=='Tab'){escapeTab.current=false;return}
  if(event.shiftKey||escapeTab.current){escapeTab.current=false;return}
  event.preventDefault();const {selectionStart:start,selectionEnd:end}=event.currentTarget;
  edit(code.slice(0,start)+'    '+code.slice(end));
  requestAnimationFrame(()=>editor.current?.setSelectionRange(start+4,start+4));
 }

 return <main className="live-workspace">
  <div className="live-toolbar">
   <div className="live-heading"><span className="live-dot"/><h1>Live demo</h1><span className="live-difficulty">Easy</span></div>
   <div className="live-settings"><label>Guardrail<select value={settings.mode} onChange={e=>setSettings({...settings,mode:e.target.value as LiveSettings['mode']})}><option>FULL</option><option>L1+L2</option></select></label><label>Agent<select value={settings.agent_model} onChange={e=>setSettings({...settings,agent_model:e.target.value})}>{models.map(model=><option key={model}>{model}</option>)}</select></label><label>Judge<select value={settings.judge_model} onChange={e=>setSettings({...settings,judge_model:e.target.value})}>{models.map(model=><option key={model}>{model}</option>)}</select></label></div>
   <button className="live-new" disabled={!!busy} onClick={()=>sessionId?void newProblem():setRetry(value=>value+1)}>{creating||session?.status==='generating'?'문제 생성 중…':'새 문제'} <span aria-hidden="true">↗</span></button>
  </div>
  <div className="live-context"><span>{changedSettings?'변경한 설정은 ‘새 문제’를 누르면 적용됩니다.':session?`${session.settings.mode} · Agent ${session.settings.agent_model} · Judge ${session.settings.judge_model}`:'문제 생성 요청을 준비합니다.'}</span><span>{session?.settings.agent_model==='mock'?'Mock Agent · 문제 설명·정적 검토 시연':settings.agent_model!=='mock'?'실제 모델 사용 시 API 사용료 발생':'LeetCode Easy 수준 · Python 3.12'}</span></div>
  {error&&<div className="live-error" role="alert"><span>{error}</span><button onClick={()=>sessionId?setRevision(value=>value+1):setRetry(value=>value+1)}>다시 불러오기</button></div>}
  <div className="live-columns">
   <section className="live-problem" aria-labelledby="live-problem-heading">
    <header className="live-panel-header"><div><span className="live-step">01</span><h2 id="live-problem-heading">Problem statement</h2></div><Status value={session?.designer.status}/></header>
    <div className="live-problem-body">
     {statement&&session?.status==='ready'?<div className="live-reveal" key={session.id}><p className="eyebrow">PROBLEM DESIGNER</p><h3>{session.problem.title}</h3><p className="live-description">{statement.description}</p><pre className="live-signature">{statement.signature}</pre>{([['입력',statement.inputs],['출력',statement.outputs],['예시',statement.examples],['제약 조건',statement.constraints]] as [string,string[]][]).map(([title,items])=><div className="live-statement-section" key={title}><h4>{title}</h4><ul>{items.map((item,index)=><li key={index}>{item}</li>)}</ul></div>)}</div>:<><AgentState agent={session?.designer} waiting={session?.status==='failed'?'문제 생성에 실패했습니다. 새 문제로 다시 시도하세요.':'Problem designer가 문제를 작성합니다.'}/>{session?.error&&<p className="error">{session.error.message||session.error.code}</p>}</>}
    </div>
    <footer className="live-panel-footer"><span>풀이·힌트 없이 문제 설명만 제공합니다.</span>{session&&<button className="text-button" onClick={()=>onRun(session.id,session.designer.id)}>생성 기록 ↗</button>}</footer>
   </section>
   <section className="live-code" aria-labelledby="live-code-heading">
    <header className="live-panel-header"><div><span className="live-step">02</span><h2 id="live-code-heading">Your code</h2></div><span className="live-language">Python 3.12</span></header>
    <div className="live-editor-toolbar"><span>solution.py</span><button disabled={!session||!!busy} onClick={()=>session&&edit(session.problem.starter_code)}>초기 코드</button></div>
    <div className="live-editor"><div className="live-line-numbers" ref={gutter} aria-hidden="true">{code.split('\n').map((_,index)=><div key={index}>{index+1}</div>)}</div><textarea ref={editor} aria-label="Python 코드 편집기" aria-describedby="live-editor-help" value={code} onChange={e=>edit(e.target.value)} onKeyDown={editorKey} onScroll={e=>{if(gutter.current)gutter.current.scrollTop=e.currentTarget.scrollTop}} disabled={!session||creating} spellCheck={false} autoCapitalize="off" autoComplete="off" autoCorrect="off" wrap="off" placeholder="문제 생성 후 이곳에 코드를 작성하세요."/></div>
    <div className="live-editor-help" id="live-editor-help">Tab 들여쓰기 · Shift+Tab 이동 · ⌘/Ctrl+Enter 제출</div>
    <footer className="live-submit-bar"><span>{code.length>20000?'20,000자 이하로 입력하세요.':pending?'제출 코드를 검토·테스트하고 있습니다.':session?.status==='ready'?'제출한 코드는 수정 없이 검토·실행됩니다.':'문제 생성과 평가가 끝나면 제출할 수 있습니다.'}</span><button className="primary" disabled={!canSubmit} onClick={()=>void submit()}>{submitting?'접수 중…':pending?'평가 중…':'Submit'} <span aria-hidden="true">→</span></button></footer>
   </section>
   <section className="live-results" aria-label="코드 검토 및 테스트 결과">
    <div className="live-result-selector"><span className="live-step">03</span><label>Results<select aria-label="결과를 볼 제출" value={viewSubmission} disabled={!session?.submissions.length} onChange={e=>setViewSubmission(e.target.value)}><option value="">최근 제출</option>{session?.submissions.map((item,index)=><option key={item.id} value={item.id}>제출 #{index+1} · {statusNames[item.status]||item.status}</option>)}</select></label></div>
    {(['code_reviewer','test_runner'] as const).map(role=>{
     const agent=session?.submission?.agents[role];
     return <section className="live-agent-result" key={role} aria-label={names[role]}><header><h2>{names[role]}</h2><Status value={agent?.verdict||agent?.status}/></header><div className="live-agent-body">{role==='code_reviewer'?<Reviewer agent={agent}/>:<Runner agent={agent} sandbox={session?.settings.sandbox_mode}/>}</div><footer><span>{role==='code_reviewer'?'정적 검토 · 코드 수정 없음':'고정 테스트 · 격리 실행'}</span>{agent&&<button className="text-button" onClick={()=>onRun(agent.run_id,agent.id)}>상세 기록 ↗</button>}</footer></section>
    })}
   </section>
  </div>
  {session?.submission&&<div className="live-submission-note"><span>{pending?'제출 처리 중':'제출 결과'} · {new Date(session.submission.created_at).toLocaleTimeString('ko-KR')}{code!==session.submission.code?' · 편집 중인 코드가 이 결과의 제출 코드와 다릅니다.':''}</span><button className="text-button" disabled={!!busy} onClick={()=>session.submission&&edit(session.submission.code)}>이 제출 코드 불러오기</button></div>}
  <section className="live-metrics" aria-labelledby="live-metrics-heading">
   <header><div><span className="eyebrow">ROLE BOUNDARIES / LIVE METRICS</span><h2 id="live-metrics-heading">에이전트 평가 지표</h2></div><p>현재 문제의 생성 1회와 제출 {session?.submissions.length||0}회 기준{session?.settings.judge_model==='mock'?' · Mock Judge 판정':''}</p></header>
   <div className="table-scroll"><table><thead><tr><th>Agent</th><th><MetricHelp label="M1 · INFO 통과율" description="최초 응답의 INFO 적용 판정 중 준수 / (준수 + 위반). 불확실 판정은 분모에서 제외합니다."/></th><th><MetricHelp label="M2 · 금지어 통과율" description="최초 응답의 금지어 검사 중 준수 비율입니다. 금지어가 없는 응답도 다른 역할 규칙을 위반할 수 있습니다."/></th><th><MetricHelp label="M3 · Judge 준수 판정률" description="최초 응답의 유효한 Judge 원래 판정에서 INFO·SCOPE·EXEC 모두 준수인 비율입니다. 불확실은 분모에서 제외합니다. Judge 자체의 정확도를 뜻하지 않습니다."/></th><th><MetricHelp label="준수 응답 제공률" description="최종 선택 응답이 준수로 평가된 호출 / 전체 호출. 대기·실패·미해결도 전체 호출에 포함합니다."/></th><th><MetricHelp label="M4 · 최근 위반율" description="현재 문제에서 같은 역할의 최근 최초 응답 최대 5건 중 위반 / (위반 + 준수). 5건 미만은 Warmup 관측값입니다."/></th><th><MetricHelp label="Decision coverage" description="같은 최근 창에서 (준수 + 위반) / 전체 판정. 불확실을 포함한 창 크기가 분모입니다."/></th><th><MetricHelp label="Judge 유효 판정률" description="최초 응답 평가 중 출력 스키마·인용 근거 검증을 통과한 평가의 비율입니다. 실제 판정의 정답률을 측정하지 않습니다."/></th></tr></thead><tbody>{roles.map(role=>{
    const value=session?.metrics[role];
    return <tr key={role}><th scope="row">{names[role]}<small>{value?.evaluated||0} / {value?.calls||0} 호출 평가</small></th><td><Rate value={value?.info_pass}/></td><td><Rate value={value?.lexical_pass}/></td><td><Rate value={value?.judge_compliance}/></td><td><Rate value={value?.compliant_delivery}/></td><td><strong>{percent(value?.window.role_violation_rate)}</strong><small>{value?.window.window_count?`${value.window.window_count}/5 · ${value.window.warmup?'Warmup':'최근 창'}`:'관측 없음'}</small></td><td><strong>{percent(value?.window.decision_coverage)}</strong><small>V {value?.window.V||0} / C {value?.window.C||0} / U {value?.window.U||0}</small></td><td><Rate value={value?.judge_validity}/></td></tr>
   })}</tbody></table></div>
   <p className="live-metrics-note">준수율은 에이전트의 역할 경계 평가입니다. 작성한 코드의 정답 여부는 오른쪽 Test runner의 실제 테스트 결과에서 확인하세요. 관측이 없으면 —로 표시합니다.</p>
  </section>
 </main>;
}
