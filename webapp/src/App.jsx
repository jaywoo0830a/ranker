import { useState } from 'react'
import { useJobs } from './hooks/useJobs.js'
import JobCreate from './components/JobCreate.jsx'
import JobList from './components/JobList.jsx'
import JobDetail from './components/JobDetail.jsx'

export default function App() {
  const { jobs, concurrency, refresh, error } = useJobs()
  const [selectedId, setSelectedId] = useState(null)

  return (
    <main>
      <h1>Ranker</h1>

      <output role="status">
        {concurrency && (
          <p>
            동시 실행: {concurrency.running} / {concurrency.max}
            {concurrency.queued > 0 && ` (대기 ${concurrency.queued})`}
          </p>
        )}
        {error && <p>오류: {error.message}</p>}
      </output>

      <details>
        <summary>새 작업 생성</summary>
        <JobCreate onCreated={refresh} />
      </details>

      <h2>작업 목록</h2>
      <JobList
        jobs={jobs}
        onSelect={setSelectedId}
        onChanged={() => {
          refresh()
          setSelectedId(null)
        }}
        onRefresh={refresh}
      />

      {selectedId && (
        <JobDetail jobId={selectedId} onClose={() => setSelectedId(null)} />
      )}
    </main>
  )
}
