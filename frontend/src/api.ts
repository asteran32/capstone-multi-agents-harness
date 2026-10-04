export async function api<T>(path:string, options:RequestInit = {}):Promise<T> {
  const auth = sessionStorage.getItem('guardrail-auth');
  const response = await fetch('/api'+path, {...options, headers:{'Content-Type':'application/json', ...(auth?{Authorization:auth}:{}), ...options.headers}});
  if (!response.ok) {
    if(response.status===401) { sessionStorage.removeItem('guardrail-auth'); window.dispatchEvent(new Event('auth-expired')); }
    const body = await response.json().catch(()=>({detail:response.statusText}));
    throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail));
  }
  return response.json();
}
export const post = <T>(path:string, body:unknown = {}) => api<T>(path,{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(body)});
