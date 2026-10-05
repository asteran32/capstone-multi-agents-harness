async function request(path:string, options:RequestInit = {}):Promise<Response> {
  const auth = sessionStorage.getItem('guardrail-auth');
  const response = await fetch('/api'+path, {...options, headers:{'Content-Type':'application/json', ...(auth?{Authorization:auth}:{}), ...options.headers}});
  if (!response.ok) {
    if(response.status===401) { sessionStorage.removeItem('guardrail-auth'); window.dispatchEvent(new Event('auth-expired')); }
    const body = await response.json().catch(()=>({detail:response.statusText}));
    throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail));
  }
  return response;
}
export async function api<T>(path:string, options:RequestInit = {}):Promise<T> {
  return (await request(path,options)).json();
}
export async function downloadCsv(path:string, fallbackFilename:string):Promise<void> {
  const response = await request(path, {headers:{Accept:'text/csv'}});
  const filename = response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || fallbackFilename;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href=url;link.download=filename;document.body.appendChild(link);
  try {link.click()} finally {link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)}
}
export const post = <T>(path:string, body:unknown = {}) => api<T>(path,{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(body)});
