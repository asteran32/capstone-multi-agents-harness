// Presentation only. The model's raw JSON remains the evaluation and history source.
export function presentResponse(role:string, parsed:unknown):string|null {
 if(!parsed||typeof parsed!=='object')return null;
 const p=parsed as Record<string,any>;
 if(role==='problem_designer'&&typeof p.description==='string'&&typeof p.signature==='string'&&
    ['inputs','outputs','examples','constraints'].every(k=>Array.isArray(p[k]))) {
  return [['문제 설명',p.description],['함수 서명',p.signature],['입력',p.inputs.join('\n')],
   ['출력',p.outputs.join('\n')],['예시',p.examples.join('\n')],['제약 조건',p.constraints.join('\n')]]
   .map(([title,text])=>`${title}\n${text}`).join('\n\n');
 }
 if(role==='code_reviewer'&&typeof p.summary==='string'&&Array.isArray(p.findings)&&
    Array.isArray(p.uncertainties)) {
  const findings=p.findings.length?p.findings.map((f:any,i:number)=>`${i+1}. 위치: ${f.location}\n원인: ${f.cause}\n영향: ${f.impact}`).join('\n\n'):'정적 검토에서 확인된 오류 없음';
  return `검토 요약\n${p.summary}\n\n발견한 오류\n${findings}${p.uncertainties.length?'\n\n판단 불가 사항\n'+p.uncertainties.join('\n'):''}${typeof p.limitations==='string'?'\n\n정적 검토의 한계\n'+p.limitations:''}`;
 }
 return null;
}
