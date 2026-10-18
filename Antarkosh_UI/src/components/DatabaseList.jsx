import { CheckCircle2, Loader2, PencilLine, Plus, RefreshCw } from 'lucide-react'

function formatWhen(value) {
  if (!value) return 'never'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? 'never' : d.toLocaleString()
}

// One row per database the admin may manage. "Active" means answers can come from it (it has been synced);
// "Your questions" marks the one YOUR questions are answered from right now.
export default function DatabaseList({ databases, currentId, busyId, onAdd, onEdit, onSync, onUse }) {
  return (
    <section className="settings-card">
      <div className="settings-card__heading db-list__heading">
        <h3 className="settings-card__title">Connected databases</h3>
        <button type="button" className="secondary-button db-list__add" onClick={onAdd}>
          <Plus size={14} />
          <span>Add database</span>
        </button>
      </div>

      {databases.length === 0 ? (
        <p className="settings-card__desc">No database is connected yet. Add one below.</p>
      ) : (
        <ul className="db-list">
          {databases.map((db) => {
            const busy = busyId === db.id
            const isCurrent = db.id === currentId
            return (
              <li key={db.id} className="db-row">
                <div className="db-row__info">
                  <div className="db-row__name">
                    <span>{db.name}</span>
                    {db.name !== db.id ? <code className="db-row__id">{db.id}</code> : null}
                  </div>
                  <div className="db-row__meta">
                    <span className={`db-badge ${db.enabled ? 'db-badge--active' : 'db-badge--idle'}`}>
                      {db.enabled ? <CheckCircle2 size={11} /> : null}
                      {db.enabled ? 'Active' : 'Not active - sync to activate'}
                    </span>
                    {isCurrent ? <span className="db-badge db-badge--current">Your questions use this</span> : null}
                    {db.engine ? <span>{db.engine}</span> : null}
                    {db.owner ? <span>Owner: {db.owner}</span> : null}
                    <span>Last sync: {formatWhen(db.last_synced_at)}</span>
                  </div>
                </div>

                <div className="db-row__actions">
                  {db.enabled && !isCurrent ? (
                    <button type="button" className="secondary-button" disabled={!!busyId} onClick={() => onUse(db.id)}>
                      Use for my questions
                    </button>
                  ) : null}
                  <button type="button" className="secondary-button" disabled={!!busyId} onClick={() => onEdit(db.id)}>
                    <PencilLine size={13} />
                    <span>Edit</span>
                  </button>
                  <button type="button" className="sync-card__btn" disabled={!!busyId} onClick={() => onSync(db.id)}>
                    {busy ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
                    <span>{busy ? 'Syncing...' : 'Sync schema'}</span>
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
