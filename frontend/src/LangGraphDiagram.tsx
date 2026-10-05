// Mirrors the Mermaid flowchart in development spec section 6.
// Keep node labels, edge labels, and connections aligned with that source.
type FlowNode = {
 id:string; lines:string[]; x:number; y:number; width:number; height:number;
 shape?:'decision'|'terminal';
};
type FlowEdge = {
 from:string; to:string; path:string; label?:string[]; x?:number; y?:number;
 anchor?:'start'|'middle'|'end';
};

const nodes:FlowNode[] = [
 {id:'S',lines:['Load fixed config and history'],x:480,y:50,width:320,height:56,shape:'terminal'},
 {id:'P',lines:['Build input and apply L1','if enabled'],x:480,y:155,width:320,height:70},
 {id:'A',lines:['Agent API'],x:480,y:260,width:320,height:56},
 {id:'T',lines:['Authorized tool call?'],x:480,y:370,width:280,height:100,shape:'decision'},
 {id:'X',lines:['Run or reuse sandbox result'],x:820,y:370,width:280,height:64},
 {id:'O',lines:['Persist response'],x:480,y:480,width:320,height:56},
 {id:'E',lines:['L2 enabled?'],x:480,y:585,width:240,height:100,shape:'decision'},
 {id:'D',lines:['Log disabled evaluation'],x:125,y:585,width:220,height:72},
 {id:'L',lines:['Lexical'],x:480,y:695,width:320,height:56},
 {id:'J',lines:['Judge'],x:480,y:795,width:320,height:56},
 {id:'V',lines:['Validate schema and evidence'],x:480,y:895,width:320,height:70},
 {id:'M',lines:['Aggregate and persist metrics'],x:480,y:1010,width:320,height:70},
 {id:'R',lines:['FULL and violated','and recovery unused?'],x:480,y:1140,width:350,height:130,shape:'decision'},
 {id:'C',lines:['Persist recovery feedback','and reset history (v2)'],x:820,y:1140,width:280,height:90},
 {id:'F',lines:['Select final result and finish'],x:480,y:1290,width:320,height:60,shape:'terminal'},
];

const edges:FlowEdge[] = [
 {from:'S',to:'P',path:'M480 78 V120'},
 {from:'P',to:'A',path:'M480 190 V232'},
 {from:'A',to:'T',path:'M480 288 V320'},
 {from:'T',to:'X',path:'M620 370 H680',label:['yes'],x:650,y:357,anchor:'middle'},
 {from:'X',to:'A',path:'M820 338 V260 H640'},
 {from:'T',to:'O',path:'M480 420 V452',label:['final response'],x:495,y:442},
 {from:'O',to:'E',path:'M480 508 V535'},
 {from:'E',to:'D',path:'M360 585 H235',label:['no'],x:298,y:571,anchor:'middle'},
 {from:'E',to:'L',path:'M480 635 V667',label:['yes'],x:495,y:657},
 {from:'L',to:'J',path:'M480 723 V767'},
 {from:'J',to:'V',path:'M480 823 V860'},
 {from:'V',to:'J',path:'M320 895 H270 V795 H320',label:['invalid and','retry available'],x:254,y:838,anchor:'end'},
 {from:'V',to:'M',path:'M480 930 V975',label:['valid or retries exhausted'],x:495,y:957},
 {from:'M',to:'R',path:'M480 1045 V1075'},
 {from:'R',to:'C',path:'M655 1140 H680',label:['yes'],x:668,y:1127,anchor:'middle'},
 {from:'C',to:'A',path:'M960 1140 H1050 V215 H680 V245 H640'},
 {from:'R',to:'F',path:'M480 1205 V1260',label:['no'],x:495,y:1238},
 {from:'D',to:'F',path:'M125 621 V660 H50 V1290 H320'},
];

export function LangGraphDiagram() {
 return <svg className="langgraph-diagram" viewBox="0 0 1100 1370" role="img" aria-labelledby="langgraph-title langgraph-description">
  <title id="langgraph-title">LangGraph 실행 설계</title>
  <desc id="langgraph-description">개발 명세 6절의 각 논리 call 실행 흐름. 고정 설정과 이력을 불러오고 L1을 적용한 뒤 Agent를 호출합니다. 허용된 도구 호출은 sandbox 결과를 실행하거나 재사용하고 Agent로 돌아갑니다. 응답을 저장한 뒤 L2가 꺼져 있으면 비활성을 기록하고 종료합니다. L2가 켜져 있으면 Lexical, Judge, schema와 인용 검증을 수행하며 무효 판정은 재시도합니다. 판정과 지표를 집계한 후 FULL 모드의 확정 위반이고 복구를 사용하지 않았으면 context를 초기화하여 Agent를 다시 호출합니다. 그 외에는 최종 결과를 선택하고 종료합니다.</desc>
  <defs><marker id="langgraph-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M1 1 L9 5 L1 9 Z"/></marker></defs>
  <g className="langgraph-edges">{edges.map(edge=><g key={`${edge.from}-${edge.to}`} data-from={edge.from} data-to={edge.to}>
   <path d={edge.path}/>
   {edge.label&&<text x={edge.x} y={edge.y} textAnchor={edge.anchor||'start'}>{edge.label.map((line,index)=><tspan key={line} x={edge.x} dy={index?16:0}>{line}</tspan>)}</text>}
  </g>)}</g>
  <g className="langgraph-nodes">{nodes.map(node=><g key={node.id} data-node={node.id} className={node.shape==='decision'?'langgraph-decision':''}>
   {node.shape==='decision'?<polygon points={`${node.x},${node.y-node.height/2} ${node.x+node.width/2},${node.y} ${node.x},${node.y+node.height/2} ${node.x-node.width/2},${node.y}`}/>:<rect x={node.x-node.width/2} y={node.y-node.height/2} width={node.width} height={node.height} rx={node.shape==='terminal'?28:6}/>}
   <text x={node.x} y={node.y-(node.lines.length-1)*10+5} textAnchor="middle">{node.lines.map((line,index)=><tspan key={line} x={node.x} dy={index?20:0}>{line}</tspan>)}</text>
  </g>)}</g>
 </svg>
}
