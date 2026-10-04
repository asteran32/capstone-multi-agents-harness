export type Role = 'problem_designer' | 'code_reviewer' | 'test_runner';
export const roles: Role[] = ['problem_designer','code_reviewer','test_runner'];
export const names: Record<Role,string> = {problem_designer:'Problem designer',code_reviewer:'Code reviewer',test_runner:'Test runner'};
export type Mode = 'Base' | 'L1' | 'L1+L2' | 'FULL';
export type ExperimentMode = Exclude<Mode, 'Base'>;
export type EvaluationPhase = 'online' | 'offline';
export type ModelType = 'mock' | 'live';
export type Dict = Record<string, any>;
export interface Task { id: string; source_task_id: string; prompt: string; entry_point: string }
export interface Candidate {id: string; private_label: string; code: string; code_sha256: string}
export interface Run {id:string; mode:Mode; model_type:ModelType; status:string; created_at:string; total_calls:number; finished_calls:number; failed_calls:number; config_snapshot:Dict; jobs:Dict[]; error:Dict|null}
export interface BatchItem {index:number; source_task_id:string; run_id:string; status:string; finished:boolean; has_errors:boolean; total_calls:number; finished_calls:number; failed_calls:number; evaluation_errors:number; offline_status:string|null; error:Dict|null}
export interface Batch {id:string; created_at:string; mode:ExperimentMode; status:string; total_runs:number; finished_runs:number; error_runs:number; source_batch_id:string|null; config_snapshot:Dict; items?:BatchItem[]}
export interface ModelUsage {input_tokens?:number; output_tokens?:number; total_tokens?:number}
export interface ModelRequest {id:string; operation_key:string; evaluation_id:string|null; usage:ModelUsage|null; response_payload?:{usage?:ModelUsage}|null}
export interface Call {id:string; role:Role; call_seq:number; round_index:number; status:string; selected_response_id:string|null; error:Dict|null; response_count:number; evaluations:Dict[]; snapshots?:Dict[]; responses?:Dict[]; requests?:ModelRequest[]; tools?:Dict[]}
export const terminal = ['completed','failed','blocked','unresolved','cancelled'];
