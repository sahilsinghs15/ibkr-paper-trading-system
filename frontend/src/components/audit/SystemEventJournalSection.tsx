import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { SystemEventJournal } from './SystemEventJournal'

/**
 * Collapsible home for the machine/system event journal (event_log) on the
 * System Monitor page. Mounted lazily so it adds no load until opened.
 */
export function SystemEventJournalSection() {
  const location = useLocation()
  const [open, setOpen] = useState(location.hash === '#event-journal')

  useEffect(() => {
    if (location.hash === '#event-journal') {
      setOpen(true)
      document.getElementById('event-journal')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [location.hash])

  return (
    <section id="event-journal" className="audit-disclosure journal-section">
      <button
        type="button"
        className="audit-disclosure-head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="audit-disclosure-caret">{open ? '▾' : '▸'}</span>
        <span className="audit-disclosure-title">System Event Journal</span>
        <span className="audit-disclosure-hint">
          Engine, RMS, reconcile and service events (machine activity — operator actions are in Audit Logs)
        </span>
      </button>
      {open && (
        <div className="journal-body">
          <SystemEventJournal />
        </div>
      )}
    </section>
  )
}
