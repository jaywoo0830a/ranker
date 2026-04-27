import { useState } from 'react'
import { createJob } from '../api.js'

export default function JobCreate({ onCreated }) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(null)

  async function handleSubmit(event) {
    event.preventDefault()
    const form = event.currentTarget
    const fd = new FormData(form)
    setSubmitting(true)
    setError(null)
    setSuccess(null)
    try {
      const job = await createJob({
        manifest: fd.get('manifest'),
        posts: fd.get('posts'),
        name: fd.get('name') || undefined,
      })
      setSuccess(`생성됨: ${job.id} — 상태 ${job.status}`)
      form.reset()
      onCreated?.(job)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <p>
        <label>
          매니페스트 YAML{' '}
          <input
            type="file"
            name="manifest"
            accept=".yaml,.yml,application/yaml,text/yaml"
            required
          />
        </label>
      </p>
      <p>
        <label>
          보고서 (posts) YAML{' '}
          <input
            type="file"
            name="posts"
            accept=".yaml,.yml,application/yaml,text/yaml"
            required
          />
        </label>
      </p>
      <p>
        <label>
          이름 (선택){' '}
          <input
            type="text"
            name="name"
            maxLength="100"
            placeholder="예: 0427 morning run"
          />
        </label>
      </p>
      <button type="submit" disabled={submitting}>
        {submitting ? '생성 중…' : '생성'}
      </button>
      {error && <output role="alert">오류: {error}</output>}
      {success && <output role="status">{success}</output>}
    </form>
  )
}
