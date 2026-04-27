import { useCallback, useEffect, useRef, useState } from 'react'
import { listJobs } from '../api.js'

const POLL_MS = 5000

// Poll the job list while there's at least one active (running/pending)
// job. Idle lists tick more slowly so we don't hammer the server while
// waiting for the user to do something.
export function useJobs() {
  const [jobs, setJobs] = useState([])
  const [concurrency, setConcurrency] = useState(null)
  const [error, setError] = useState(null)
  const inFlight = useRef(false)

  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const data = await listJobs()
      setJobs(data.jobs)
      setConcurrency(data.concurrency)
      setError(null)
    } catch (e) {
      setError(e)
    } finally {
      inFlight.current = false
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [refresh])

  return { jobs, concurrency, error, refresh }
}
