import { useEffect, useState } from 'react'
import { Database, Eye, EyeOff, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import {
  getDbConnection,
  testDbConnection,
  saveDbConnection,
  getDbConnectionFor,
  getEngineCatalog,
  testConnectDatabase,
  connectDatabase,
} from '../services/api.js'

const MAIN_DB = 'erp_main'

const LABELS = {
  host: 'Host',
  port: 'Port',
  database: 'Database',
  username: 'Username',
  password: 'Password',
  service_name: 'Service name',
  schema_name: 'Schema (optional)',
  odbc_driver: 'ODBC driver',
}

const connectionForm = (c) => ({
  engine: c.engine,
  host: c.host,
  port: c.port,
  database: c.database,
  service_name: c.service_name || '',
  schema_name: c.schema_name || '',
  odbc_driver: c.odbc_driver || '',
  username: c.username,
})

// Connection form for the Databases page (database admins only; the server is the real gate).
//
// There is no Database ID to type. For a new database the server creates the ID from the database's name,
// makes its folder and records you as the owner. To change a saved database, pick it in the list: this form
// is remounted (key) with `editingId` set, and the ID is sent only so the SAME database is updated.
// `onSaved(dbId)` tells the page to refresh its list.
export default function DatabaseConnectionCard({ editingId = null, onSaved, onCancel }) {
  const [cfg, setCfg] = useState(null) // engine list; null = loading, false = unavailable
  const [form, setForm] = useState({})
  const [passwordSet, setPasswordSet] = useState(false)
  const [busy, setBusy] = useState(null) // 'test' | 'save' | null
  const [error, setError] = useState('')
  const [showPassword, setShowPassword] = useState(false)

  const isMain = editingId === MAIN_DB

  useEffect(() => {
    let alive = true
    const load = async () => {
      let catalog
      try {
        catalog = await getEngineCatalog()
      } catch {
        if (alive) setCfg(false)
        return
      }
      // Only the main admin may read the main database's settings (everyone else gets a 403, ignored).
      const main = await getDbConnection().catch(() => null)
      let saved = null
      if (editingId === MAIN_DB) {
        saved = main
      } else if (editingId) {
        saved = await getDbConnectionFor(editingId).catch(() => null)
      }
      if (!alive) return
      if (saved) {
        setForm({ ...connectionForm(saved), engine: saved.engine || catalog.engines[0].key })
        setPasswordSet(!!saved.password_set)
      } else {
        const engine = catalog.engines.find((e) => e.key === main?.engine) || catalog.engines[0]
        setForm({
          engine: engine.key,
          host: '',
          port: engine.default_port ?? '',
          database: '',
          service_name: '',
          schema_name: '',
          odbc_driver: engine.key === 'mssql' ? catalog.odbc_driver || 'ODBC Driver 18 for SQL Server' : '',
          username: '',
        })
        setPasswordSet(false)
      }
      setCfg(catalog)
    }
    load()
    return () => {
      alive = false
    }
  }, [editingId])

  if (!cfg) return null

  const spec = cfg.engines.find((e) => e.key === form.engine) || cfg.engines[0]
  const set = (patch) => setForm((f) => ({ ...f, ...patch }))

  const onEngine = (key) => {
    const next = cfg.engines.find((e) => e.key === key)
    setError('')
    set({
      engine: key,
      port: next?.default_port ?? '',
      ...(key === 'mssql' && !form.odbc_driver ? { odbc_driver: cfg.odbc_driver || 'ODBC Driver 18 for SQL Server' } : {}),
    })
  }

  const run = async (kind) => {
    setBusy(kind)
    setError('')
    try {
      const payload = { ...form, db_id: editingId && !isMain ? editingId : undefined }
      if (kind === 'test') {
        await (isMain ? testDbConnection(form) : testConnectDatabase(payload))
        toast.success('Connection works')
      } else if (isMain) {
        await saveDbConnection(form)
        set({ password: undefined }) // never keep the typed password around; omitted = "keep stored"
        toast.success('Database connection saved', {
          description: 'Run "Sync schema" so Antarkosh learns the new database.',
        })
        onSaved?.(MAIN_DB)
      } else {
        const saved = await connectDatabase(payload)
        set({ password: undefined })
        toast.success(saved.existing ? 'Database connection updated' : 'Database added', {
          description: 'Now click "Sync schema" on it. It stays inactive until its first successful sync.',
        })
        onSaved?.(saved.db_id)
      }
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Request failed')
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="settings-card">
      <div className="settings-card__heading">
        <Database size={15} />
        <h3 className="settings-card__title">{editingId ? 'Edit connection' : 'Add a database'}</h3>
      </div>
      <p className="settings-card__desc">
        Credentials are tested first, then saved to the server&apos;s configuration file.
        {editingId ? '' : ' The database gets its own ID and folder automatically.'}
      </p>

      <div className="db-form">
        <label className="db-form__row">
          <span>Database type</span>
          <select
            className="dialog-card__input"
            value={form.engine}
            onChange={(e) => onEngine(e.target.value)}
          >
            {cfg.engines.map((e) => (
              <option key={e.key} value={e.key} disabled={!e.ready && !e.testable}>
                {e.label}
                {e.ready || e.testable ? '' : ' (coming soon)'}
              </option>
            ))}
          </select>
        </label>

        {spec.fields.length === 0 ? (
          <p className="settings-card__desc">
            SQLite uses the local file data/live_data.db. No credentials needed.
          </p>
        ) : (
          spec.fields.map((f) => (
            <label key={f} className="db-form__row">
              <span>{LABELS[f] || f}</span>
              {f === 'password' ? (
                <div className="db-form__password-wrap">
                  <input
                    className="dialog-card__input db-form__password-input"
                    type={showPassword ? 'text' : 'password'}
                    autoComplete="new-password"
                    placeholder={passwordSet ? '•••••••• (unchanged)' : ''}
                    value={form[f] ?? ''}
                    onChange={(e) => set({ [f]: e.target.value })}
                  />
                  <button
                    type="button"
                    className="db-form__password-toggle"
                    onClick={() => setShowPassword((prev) => !prev)}
                    tabIndex={-1}
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                    title={showPassword ? 'Hide password' : 'Show password'}
                  >
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              ) : (
                <input
                  className="dialog-card__input"
                  type={f === 'port' ? 'number' : 'text'}
                  autoComplete="off"
                  value={form[f] ?? ''}
                  onChange={(e) => set({ [f]: e.target.value })}
                />
              )}
            </label>
          ))
        )}

        {error ? <p className="sync-card__result sync-card__result--error">{error}</p> : null}

        <div className="db-form__actions">
          <button type="button" className="sync-card__btn" disabled={!!busy} onClick={() => run('test')}>
            {busy === 'test' ? <Loader2 size={14} className="spin" /> : null}
            <span>Test connection</span>
          </button>
          <button type="button" className="sync-card__btn" disabled={!!busy} onClick={() => run('save')}>
            {busy === 'save' ? <Loader2 size={14} className="spin" /> : null}
            <span>Save connection</span>
          </button>
          {editingId && onCancel ? (
            <button type="button" className="secondary-button" disabled={!!busy} onClick={onCancel}>
              Cancel
            </button>
          ) : null}
        </div>
      </div>
    </section>
  )
}
