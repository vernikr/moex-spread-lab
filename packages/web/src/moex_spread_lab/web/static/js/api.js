export async function api(path, body, signal) {
  const r = await window.fetch(
    path,
    body ? { signal, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined,
  );
  const d = await r.json();
  if (!r.ok) throw Error(d.error || `Request failed (${r.status})`);
  return d;
}

export async function apiStream(path, body, signal, onProgress) {
  const r = await window.fetch(path, {
    signal,
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Progress-Stream': '1' },
    body: JSON.stringify(body),
  });
  const ctype = (r.headers.get('Content-Type') || '').toLowerCase();
  if (!r.ok || !ctype.includes('ndjson') || !r.body) {
    const d = await r.json();
    if (!r.ok) throw Error(d.error || `Request failed (${r.status})`);
    return d;
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let result = null;
  while (true) {
    const { value, done } = await reader.read();
    if (value) buf += dec.decode(value, { stream: !done });
    let nl;
    while ((nl = buf.indexOf('\n')) !== -1) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.type === 'progress') onProgress?.(msg);
      else if (msg.type === 'error') throw Error(msg.error || 'Request failed');
      else if (msg.type === 'result') result = msg.data;
    }
    if (done) break;
  }
  const tail = buf.trim();
  if (tail) {
    const msg = JSON.parse(tail);
    if (msg.type === 'progress') onProgress?.(msg);
    else if (msg.type === 'error') throw Error(msg.error || 'Request failed');
    else if (msg.type === 'result') result = msg.data;
  }
  if (result == null) throw Error('Пустой ответ сервера');
  return result;
}
