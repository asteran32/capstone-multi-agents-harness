import {Dict, names, roles} from './types';

export function LearnedExamples({config}: {config:Dict}) {
 const bundle=config.few_shot_snapshot;
 if(!config.learned_few_shot||config.mode==='Base'||!bundle)return <p className="muted">누적 few-shot 미적용</p>;
 const examples=roles.flatMap(role=>(bundle.examples_by_role[role]||[]).map((example:Dict)=>({...example,role})));
 return <details className="raw">
  <summary>누적 few-shot · {examples.length}개 고정 · {roles.map(role=>`${names[role]} ${bundle.examples_by_role[role]?.length||0}`).join(' / ')}</summary>
  <p className="muted">이전 실험의 L3 복구 후 검증을 통과한 사례입니다. 같은 문제의 사례는 해당 호출에서 제외합니다. 실험 중 추가된 사례는 다음 새 실험부터 반영됩니다.</p>
  {!examples.length&&<p>현재 정책·모델·평가 기준에 맞는 복구 사례가 없습니다. 기본 역할 예시로 시작합니다.</p>}
  <p className="mono muted" title={bundle.sha256}>예시 버전 {bundle.sha256.slice(0,12)}</p>
  {examples.map(example=><details key={example.id}><summary>{names[example.role as keyof typeof names]} · {example.source.source_task_id} · {example.source.criteria.join(', ')}</summary>
   <p><a href={`?run=${example.source.run_id}&call=${example.source.call_id}`}>원본 복구 기록 열기</a></p>
   <pre>{JSON.stringify({input:example.input,output:example.output,boundary_note:example.boundary_note},null,2)}</pre>
  </details>)}
 </details>;
}
