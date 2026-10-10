import type {Role} from './types';

export type LiveMode = 'L1+L2'|'FULL';
export interface LiveSettings {mode:LiveMode; agent_model:string; judge_model:string}
export interface LiveFraction {numerator:number; denominator:number; rate:number|null}
export interface LiveMetrics {
 calls:number; evaluated:number; info_pass:LiveFraction; lexical_pass:LiveFraction;
 judge_compliance:LiveFraction; compliant_delivery:LiveFraction; judge_validity:LiveFraction;
 window:{window_count:number; warmup:boolean; V:number; C:number; U:number; role_violation_rate:number|null; decision_coverage:number|null};
}
export interface DesignerStatement {description:string; signature:string; inputs:string[]; outputs:string[]; examples:string[]; constraints:string[]}
export interface ReviewerReport {summary:string; findings:{location:string;cause:string;impact:string}[]; uncertainties:string[]; limitations?:string}
export interface RunnerReport {status:string; exit_code:number|null; tests:number|null; failures:number|null; errors:number|null; mock?:boolean}
export interface LiveAgent {
 id:string; run_id:string; role:Role; status:string; verdict:string|null; withheld:boolean;
 error:{message?:string;code?:string}|null;
 response:{parsed_output:DesignerStatement|ReviewerReport|RunnerReport; response_version:number}|null;
 tool_result:RunnerReport|null;
}
export interface LiveSubmissionSummary {id:string; status:string; created_at:string}
export interface LiveSubmission extends LiveSubmissionSummary {code:string; agents:Partial<Record<Role,LiveAgent>>}
export interface LiveSession {
 id:string; status:'generating'|'ready'|'blocked'|'failed'; created_at:string;
 settings:LiveSettings & {sandbox_mode:string}; problem:{title:string; difficulty:string; starter_code:string};
 designer:LiveAgent; submission:LiveSubmission|null; submissions:LiveSubmissionSummary[];
 metrics:Record<Role,LiveMetrics>; error:{message?:string;code?:string}|null;
}
