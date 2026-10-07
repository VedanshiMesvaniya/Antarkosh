import { useEffect, useState } from 'react'
import { Database, Eye, EyeOff, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { getDbConnection, testDbConnection, saveDbConnection } from '../services/api.js'

const LABELS = {
  host: 'Host',
  port: 'Port',
  database: 'Database',
  username: 'Username',
  password: 'Password',
  service_name: 'Service name',
  odbc_driver: 'ODBC driver',
}

// Admin-only card. The server is the real gate (403 for non-admins); if the GET
// fails for any reason this component simply renders nothing.
export default function DatabaseConnectionCard() {
  const [cfg, setCfg] = useState(null) // null = loading, false = unavailable
  const [form, setForm] = useState({})
  const [busy, setBusy] = useState(null) // 'test' | 'save' | null
  const [error, setError] = useState('')
  const [showPassword, setShowPassword] = useState(false)

  useEffect(() => {
    getDbConnection()
      .then((c) => {
        setCfg(c)
        setForm({
          engine: c.engine,
          host: c.host,
          port: c.port,
          database: c.database,
          username: c.username,
        })
      })
      .catch(() => setCfg(false))
  }, [])

  if (!cfg) return null

  const spec = cfg.engines.find((e) => e.key === form.engine) || cfg.engines[0]
  const set = (patch) => setForm((f) => ({ ...f, ...patch }))

  const onEngine = (key) => {
    const next = cfg.engines.find((e) => e.key === key)
    setError('')
    set({ engine: key, port: next.default_port ?? '' })
  }

  const run = async (kind) => {
    setBusy(kind)
    setError('')
    try {
      if (kind === 'test') {
        await testDbConnection(form)
        toast.success('Connection works')
      } else {
        const saved = await saveDbConnection(form)
        setCfg(saved)
        set({ password: undefined }) // never keep the typed password around; omitted = "keep stored"
        toast.success('Database connection saved', {
          description: 'Run "Sync database schema" so Antarkosh learns the new database.',
        })
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
        <h3 className="settings-card__title">Database Connection</h3>
      </div>
      <p className="settings-card__desc">
        Admin only. Credentials are tested first, then saved to the server&apos;s configuration file.
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
              <option key={e.key} value={e.key} disabled={!e.ready}>
                {e.label}
                {e.ready ? '' : ' (coming soon)'}
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
                    placeholder={cfg.password_set ? '•••••••• (unchanged)' : ''}
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
        </div>
      </div>
    </section>
  )
}
