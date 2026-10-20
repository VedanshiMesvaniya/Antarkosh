import { CheckCircle2, Loader2, PencilLine, Plus, RefreshCw } from 'lucide-react'

function formatWhen(value) {
  if (!value) return 'never'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? 'never' : d.toLocaleString()
}

// The one connection the page is about: the database your questions use (or the one you just saved).
// Every other saved database stays out of sight and is only reachable from the "Switch" dropdown.
export default function CurrentDatabaseCard({ db, databases, busy, syncing, onSwitch, onAdd, onEdit, onSync }) {
  return (
    <section className="settings-card db-current">
      <div className="db-current__top">
        <div className="db-current__title">
          <h3 className="db-current__name">{db.name}</h3>
          {db.name !== db.id ? <code className="db-current__id">{db.id}</code> : null}
        </div>
        <div className="db-current__tools">
          {databases.length > 1 ? (
            <select
              className="dialog-card__input db-current__switch"
              aria-label="Switch database"
              value={db.id}
              disabled={busy}
              onChange={(e) => onSwitch(e.target.value)}
            >
              {databases.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                  {d.enabled ? '' : ' (not synced)'}
                </option>
              ))}
            </select>
          ) : null}
          <button type="button" className="secondary-button" disabled={busy} onClick={onAdd}>
            <Plus size={14} />
            <span>Add database</span>
          </button>
        </div>
      </div>

      <div className="db-current__meta">
        <span className={`db-badge ${db.enabled ? 'db-badge--active' : 'db-badge--idle'}`}>
          {db.enabled ? <CheckCircle2 size={11} /> : null}
          {db.enabled ? 'Active' : 'Not active - sync to activate'}
        </span>
        {db.engine ? <span>{db.engine}</span> : null}
        <span>Last sync: {formatWhen(db.last_synced_at)}</span>
      </div>

      <div className="db-current__actions">
        <button type="button" className="secondary-button" disabled={busy} onClick={onEdit}>
          <PencilLine size={13} />
          <span>Edit</span>
        </button>
        <button type="button" className="sync-card__btn" disabled={busy} onClick={onSync}>
          {syncing ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
          <span>{syncing ? 'Syncing...' : 'Sync schema'}</span>
        </button>
      </div>
    </section>
  )
}
