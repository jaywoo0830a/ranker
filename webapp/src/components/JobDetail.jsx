import { useEffect, useState } from 'react'
import { getJob, jobLogsStreamUrl } from '../api.js'

const POLL_MS = 5000
const LOG_TAIL = 200

export default function JobDetail({ jobId, onClose }) {
  const [job, setJob] = useState(null)
  const [logs, setLogs] = useState('')
  const [error, setError] = useState(null)

  // Job state is small and infrequent — keep polling rather than
  // multiplexing it onto the log WS. Failures here are user-visible
  // (the dl block disappears) so we surface them as ``error``.
  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const j = await getJob(jobId)
        if (!cancelled) {
          setJob(j)
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

  // Live logs via WebSocket — initial tail then streamed deltas. The
  // server closes the WS once the job reaches a terminal state, so we
  // don't reconnect on close: the next chunk is whatever final bytes
  // the server flushed, and there's nothing more to stream.
  useEffect(() => {
    setLogs('')
    const ws = new WebSocket(jobLogsStreamUrl(jobId))
    ws.onmessage = (event) => {
      setLogs((prev) => prev + event.data)
    }
    return () => {
      // Triggered on unmount or jobId change. close() is a no-op if the
      // server already closed.
      ws.close()
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
