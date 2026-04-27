import { useEffect, useState } from 'react'
import { getJob, getJobLogs } from '../api.js'

const POLL_MS = 5000
const LOG_TAIL = 200

export default function JobDetail({ jobId, onClose }) {
  const [job, setJob] = useState(null)
  const [logs, setLogs] = useState('')
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        // Logs may legitimately 404 right after job creation (before the
        // subprocess writes anything) — swallow and keep polling.
        const [j, l] = await Promise.all([
          getJob(jobId),
          getJobLogs(jobId, { tail: LOG_TAIL }).catch(() => ''),
        ])
        if (!cancelled) {
          setJob(j)
          setLogs(l)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(e)
      }
    }

    load()
    const id = setInterval(load, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [jobId])

  return (
    <section>
      <hr />
      <h3>
        작업 상세: {jobId}{' '}
        <button onClick={onClose}>닫기</button>
      </h3>

      {error && <output role="alert">오류: {error.message}</output>}

      {job && (
        <dl>
          <dt>이름</dt>
          <dd>{job.name}</dd>

          <dt>상태</dt>
          <dd>{job.status}</dd>

          <dt>생성</dt>
          <dd>
            <time dateTime={job.created_at}>
              {new Date(job.created_at).toLocaleString()}
            </time>
          </dd>

          {job.started_at && (
            <>
              <dt>시작</dt>
              <dd>
                <time dateTime={job.started_at}>
                  {new Date(job.started_at).toLocaleString()}
                </time>
              </dd>
            </>
          )}

          {job.completed_at && (
            <>
              <dt>완료</dt>
              <dd>
                <time dateTime={job.completed_at}>
                  {new Date(job.completed_at).toLocaleString()}
                </time>
              </dd>
            </>
          )}

          <dt>진행</dt>
          <dd>
            <progress
              max={job.progress.total_runs}
              value={job.progress.completed_runs}
            />{' '}
            {job.progress.completed_runs} / {job.progress.total_runs}
          </dd>

          {job.progress.next_run_at && (
            <>
              <dt>다음 run</dt>
              <dd>
                <time dateTime={job.progress.next_run_at}>
                  {new Date(job.progress.next_run_at).toLocaleString()}
                </time>
              </dd>
            </>
          )}

          <dt>Job 구성</dt>
          <dd>
            <ul>
              {job.jobs_config.map((jc) => (
                <li key={jc.name}>
                  {jc.name} ({jc.mode})
                </li>
              ))}
            </ul>
          </dd>

          <dt>대상 수</dt>
          <dd>{job.targets_count}</dd>

          {job.error && (
            <>
              <dt>에러</dt>
              <dd>{job.error}</dd>
            </>
          )}
        </dl>
      )}

      <details>
        <summary>로그 (마지막 {LOG_TAIL} 라인)</summary>
        <pre>{logs || '(아직 없음)'}</pre>
      </details>
    </section>
  )
}
