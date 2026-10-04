import type {EvaluationPhase, ModelRequest} from './types';

function tokenCount(value: unknown): number | null {
 return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function summarizeUsage(requests: ModelRequest[]) {
 let input: number | null = null, output: number | null = null, total: number | null = null;
 let reported = 0;
 for (const request of requests) {
  const usage = request.usage ?? request.response_payload?.usage;
  const currentInput = tokenCount(usage?.input_tokens);
  const currentOutput = tokenCount(usage?.output_tokens);
  const currentTotal = tokenCount(usage?.total_tokens)
   ?? (currentInput !== null && currentOutput !== null ? currentInput + currentOutput : null);
  if (currentInput !== null) input = (input ?? 0) + currentInput;
  if (currentOutput !== null) output = (output ?? 0) + currentOutput;
  if (currentTotal !== null) total = (total ?? 0) + currentTotal;
  if (currentInput !== null && currentOutput !== null && currentTotal !== null) reported++;
 }
 return {input, output, total, reported, requests: requests.length};
}

export function summarizeAgentUsage(requests: ModelRequest[] = []) {
 return summarizeUsage(requests.filter(r => r.evaluation_id == null && r.operation_key.startsWith('online:agent:')));
}

export function summarizeJudgeUsage(requests: ModelRequest[] = [], phase: EvaluationPhase = 'online') {
 return summarizeUsage(requests.filter(r => r.evaluation_id != null && r.operation_key.startsWith(`${phase}:judge:`)));
}
