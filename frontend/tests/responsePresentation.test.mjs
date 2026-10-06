import test from 'node:test';
import assert from 'node:assert/strict';
import {presentResponse} from '../src/responsePresentation.ts';

test('empty findings display no errors without inventing locations or changing source',()=>{
 const value={summary:'검토함',findings:[],uncertainties:['판단 불가'],limitations:'실행하지 않음'};
 const before=JSON.stringify(value);
 const rendered=presentResponse('code_reviewer',value);
 assert.match(rendered,/정적 검토에서 확인된 오류 없음/);
 assert.doesNotMatch(rendered,/위치:/);
 assert.equal(JSON.stringify(value),before);
});

test('actual findings retain location cause and impact and do not show no errors',()=>{
 const value={summary:'오류 확인',findings:[{location:'total',cause:'덮어씀',impact:'누적 안 됨'}],uncertainties:[],limitations:'실행하지 않음'};
 const rendered=presentResponse('code_reviewer',value);
 assert.match(rendered,/위치: total\n원인: 덮어씀\n영향: 누적 안 됨/);
 assert.doesNotMatch(rendered,/확인된 오류 없음/);
 assert.equal(presentResponse('code_reviewer',null),null);
 assert.equal(presentResponse('test_runner',{status:'pass'}),null);
});

test('designer retains exact signature and original values without adding code',()=>{
 const signature='def f(values: List[int]) -> int:';
 const rendered=presentResponse('problem_designer',{description:'합계',signature,inputs:['정수'],outputs:['합'],examples:['없음'],constraints:['없음']});
 assert.match(rendered,/함수 서명\ndef f\(values: List\[int\]\) -> int:/);
 assert.doesNotMatch(rendered,/import|return|pass/);
});
