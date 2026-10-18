import { useEffect, useState } from 'react'
import { Database, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { getMyDatabase, setMyDatabase } from '../services/api.js'

// Shown to every user: which database YOUR questions are answered from. The choice belongs to you alone,
// so other people using Antarkosh at the same time never change it.
export default function ActiveDatabaseCard() {
  const [info, setInfo] = useState(null) // null = loading, false = unavailable
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    getMyDatabase()
      .then(setInfo)
      .catch(() => setInfo(false))
  }, [])

  if (!info) return null

  const choose = async (value) => {
    setBusy(true)
    try {
      const saved = await setMyDatabase(value)
      setInfo((prev) => ({ ...prev, active: saved.active, effective: saved.effective }))
      toast.success(saved.active ? `Now answering from "${saved.active}"` : 'Now answering from the main database')
    } catch (e) {
      toast.error(e.response?.data?.detail || e.message || 'Could not change the database')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="settings-card">
      <div className="settings-card__heading">
        <Database size={15} />
        <h3 className="settings-card__title">My Database</h3>
      </div>
      <p className="settings-card__desc">
        Choose which database your questions are answered from. This only changes it for you.
      </p>
      <div className="db-form">
        <label className="db-form__row">
          <span>Answer from</span>
          <select
            className="dialog-card__input"
            value={info.active || ''}
            disabled={busy}
            onChange={(e) => choose(e.target.value)}
          >
            <option value="">Main database ({info.default})</option>
            {info.options.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
        </label>
        {busy ? <Loader2 size={14} className="spin" /> : null}
        {info.options.length === 0 ? (
          <p className="settings-card__desc">No other database has been shared with you yet.</p>
        ) : null}
      </div>
    </section>
  )
}
