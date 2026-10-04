export type Role = 'problem_designer' | 'code_reviewer' | 'test_runner';
export const roles: Role[] = ['problem_designer','code_reviewer','test_runner'];
export const names: Record<Role,string> = {problem_designer:'Problem designer',code_reviewer:'Code reviewer',test_runner:'Test runner'};
export type Mode = 'Base' | 'L1' | 'L1+L2' | 'FULL';
export type Dict = Record<string, any>;
export interface Task { id: string; source_task_id: string; prompt: string; entry_point: string }
export interface Candidate {id: string; private_label: string; code: string; code_sha256: string}
export interface Run {id:string; mode:Mode; status:string; created_at:string; total_calls:number; finished_calls:number; failed_calls:number; config_snapshot:Dict; jobs:Dict[]; error:Dict|null}
export interface Call {id:string; role:Role; call_seq:number; round_index:number; status:string; selected_response_id:string|null; error:Dict|null; response_count:number; evaluations:Dict[]; snapshots?:Dict[]; responses?:Dict[]; requests?:Dict[]; tools?:Dict[]}
export const terminal = ['completed','failed','blocked','unresolved','cancelled'];
