// Fetch wrapper for the Ranker Service API (docs/openapi.yaml).
// Empty base means same-origin in production and Vite-proxied in dev.
const API_BASE = import.meta.env.VITE_API_BASE || ''

async function readError(res) {
  // Server returns { error, message, details? } on 4xx/5xx per the spec.
  // Fall back gracefully when the response isn't JSON (network mid-flight,
  // proxy down, etc.) so the UI gets a useful message either way.
  try {
    const body = await res.json()
    return new Error(body.message || `${res.status} ${res.statusText}`)
  } catch {
    return new Error(`${res.status} ${res.statusText}`)
  }
}

export async function listJobs({ status, limit } = {}) {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  if (limit) params.set('limit', String(limit))
  const qs = params.toString()
  const res = await fetch(`${API_BASE}/api/jobs${qs ? '?' + qs : ''}`)
  if (!res.ok) throw await readError(res)
  return res.json()
}

export async function getJob(id) {
  const res = await fetch(`${API_BASE}/api/jobs/${id}`)
  if (!res.ok) throw await readError(res)
  return res.json()
}

export async function createJob({ manifest, posts, name }) {
  const form = new FormData()
  form.append('manifest', manifest)
  form.append('posts', posts)
  if (name) form.append('name', name)
  const res = await fetch(`${API_BASE}/api/jobs`, { method: 'POST', body: form })
  if (!res.ok) throw await readError(res)
  return res.json()
}

export async function deleteJob(id) {
  const res = await fetch(`${API_BASE}/api/jobs/${id}`, { method: 'DELETE' })
  if (!res.ok) throw await readError(res)
}

export async function getJobLogs(id, { tail } = {}) {
  const qs = tail ? `?tail=${tail}` : ''
  const res = await fetch(`${API_BASE}/api/jobs/${id}/logs${qs}`)
  if (!res.ok) throw await readError(res)
  return res.text()
}

// The result is a YAML download — let the browser handle it via a real link
// rather than fetching here.
export function jobResultUrl(id) {
  return `${API_BASE}/api/jobs/${id}/result`
}

// WebSocket URL for the live log stream. Picks ws:// or wss:// to match
// the page's protocol so deployments behind HTTPS upgrade automatically.
// In dev, Vite proxies the WS upgrade to the FastAPI server (see
// vite.config.js, ``ws: true``).
export function jobLogsStreamUrl(id) {
  if (API_BASE) {
    // Explicit base override (e.g. cross-origin staging) — swap the
    // http(s) prefix for the matching ws(s) prefix.
    return `${API_BASE.replace(/^http/, 'ws')}/api/jobs/${id}/logs/stream`
  }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/api/jobs/${id}/logs/stream`
}
