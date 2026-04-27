import { deleteJob, jobResultUrl } from '../api.js'

export default function JobList({ jobs, onSelect, onChanged, onRefresh }) {
  if (jobs.length === 0) {
    return (
      <>
        <p>작업 없음.</p>
        <button onClick={onRefresh}>새로고침</button>
      </>
    )
  }
  return (
    <>
      <table>
        <thead>
          <tr>
            <th>이름</th>
            <th>상태</th>
            <th>진행</th>
            <th>대기</th>
            <th>생성</th>
            <th>작업</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              onSelect={onSelect}
              onChanged={onChanged}
            />
          ))}
        </tbody>
      </table>
      <button onClick={onRefresh}>새로고침</button>
    </>
  )
}

function JobRow({ job, onSelect, onChanged }) {
  const { completed_runs, total_runs } = job.progress
  const resultAvailable =
    job.status === 'completed' ||
    job.status === 'running' ||
    job.status === 'cancelled'

  async function handleDelete() {
    const verb = job.status === 'running' ? '취소' : '삭제'
    if (!window.confirm(`${verb}하시겠습니까?\n${job.id}`)) return
    try {
      await deleteJob(job.id)
      onChanged?.()
    } catch (err) {
      window.alert(`실패: ${err.message}`)
    }
  }

  return (
    <tr>
      <td>
        <a
          href="#"
          onClick={(e) => {
            e.preventDefault()
            onSelect(job.id)
          }}
        >
          {job.name}
        </a>
      </td>
      <td>{job.status}</td>
      <td>
        <progress max={total_runs} value={completed_runs} />{' '}
        {completed_runs}/{total_runs}
      </td>
      <td>{job.queue_position ?? '-'}</td>
      <td>
        <time dateTime={job.created_at}>
          {new Date(job.created_at).toLocaleString()}
        </time>
      </td>
      <td>
        {resultAvailable && (
          <>
            <a href={jobResultUrl(job.id)} download>
              결과
            </a>{' '}
          </>
        )}
        <button onClick={handleDelete}>
          {job.status === 'running' ? '취소' : '삭제'}
        </button>
      </td>
    </tr>
  )
}
