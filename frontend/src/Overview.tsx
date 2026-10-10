import {LangGraphDiagram} from './LangGraphDiagram';
import type {DatasetInfo} from './types';
import './overview.css';

function Architecture() {
 return <section className="overview-architecture" aria-labelledby="architecture-title">
  <div className="overview-section-heading">
   <div><span className="eyebrow">02 / SYSTEM ARCHITECTURE</span><h2 id="architecture-title">시스템 아키텍처</h2></div>
   <p>LangGraph 실행 설계 · 각 역할의 논리 call에 적용되는 흐름입니다.</p>
  </div>
  <figure className="architecture-figure">
   <div className="architecture-scroll" tabIndex={0} role="region" aria-label="LangGraph 실행 그래프. 작은 화면에서는 좌우로 스크롤할 수 있습니다.">
    <LangGraphDiagram/>
   </div>
   <figcaption>출처: 개발 명세 6. LangGraph 실행 설계 · 도구 호출, L2 평가, Judge 재시도와 FULL 복구 분기</figcaption>
  </figure>
  <div className="overview-round">
   <div><span className="eyebrow">ONE ROUND</span><h3>세 역할의 실행 순서</h3></div>
   <ol><li><span>01</span><strong>Problem designer</strong><small>문제 설명·가이드라인</small></li><li><span>02</span><strong>Code reviewer</strong><small>후보 코드 정적 검토</small></li><li><span>03</span><strong>Test runner</strong><small>원본 테스트 실행·결과 보고</small></li></ol>
  </div>
  <p className="overview-round-note">역할별 입력은 분리됩니다. Designer 출력은 Reviewer에게 전달하지 않으며, Reviewer와 Runner는 같은 후보 코드를 사용합니다.</p>
 </section>
}

export function Overview({onExperiment,dataset}:{onExperiment:()=>void;dataset:DatasetInfo}) {
 return <main className="overview">
  <header className="overview-heading"><div><span className="eyebrow">PROJECT OVERVIEW</span><h1>Guardrail Lab</h1><p>HumanEval 기반 세 Agent의 역할 준수 실험 플랫폼</p></div><button className="primary" onClick={onExperiment}>실험 열기 <span aria-hidden="true">→</span></button></header>
  <section className="overview-intro" aria-labelledby="overview-purpose-title">
   <div className="overview-intro-copy"><span className="eyebrow">01 / PURPOSE</span><h2 id="overview-purpose-title">프로젝트 개요와 목적</h2><p>같은 문제와 후보 코드에서 Guardrail 모드를 바꾸며 Agent의 응답과 역할 준수 결과를 비교합니다. 강화 프롬프트, 평가, 복구가 각각 어떻게 작동했는지 저장된 근거로 확인하는 연구용 웹 데모입니다.</p></div>
   <dl className="overview-objectives">
    <div><dt>비교</dt><dd>Base부터 FULL까지 네 모드에서 강화 지침·온라인 평가·자동 복구의 차이를 관찰합니다.</dd></div>
    <div><dt>검증</dt><dd>금지어 검사, Judge의 INFO·SCOPE·EXEC 판정, 위반 인용과 실제 실행 근거를 대조합니다.</dd></div>
    <div><dt>추적</dt><dd>입력 이력, 응답, 최초·복구 판정과 로그를 보존해 결과가 나온 과정을 다시 확인합니다.</dd></div>
   </dl>
  </section>
  <div className="overview-modes" aria-label="Guardrail 모드별 기능">
   <div><strong>Base</strong><span>기본 업무 지시</span></div><div><strong>L1</strong><span>강화 프롬프트</span></div><div><strong>L1+L2</strong><span>강화 프롬프트 + 온라인 평가</span></div><div><strong>FULL</strong><span>강화·평가 + 확정 위반 시 1회 복구</span></div>
  </div>
  <Architecture/>
  <footer className="overview-footer"><p>구현 범위: {dataset.first_task_id}–{dataset.last_task_id.split('/').pop()} · {dataset.task_count}문제 · 단일 연구자 · 단일 worker</p><details><summary>참고 문서</summary><ul><li>README.md — 현재 구현과 실행 방법</li><li>docs/web_guardrail_platform_development_spec_ko.md — 목표·기능·평가 기준</li><li>docs/web_guardrail_platform_system_architecture_ko.md — 시스템 구성·데이터·실행 흐름</li></ul></details></footer>
 </main>
}
