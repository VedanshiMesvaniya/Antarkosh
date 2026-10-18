import { useCallback, useEffect, useState } from 'react'
import { Database, Eye, EyeOff, Loader2, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import {
  getDbConnection,
  testDbConnection,
  saveDbConnection,
  listAdminDatabases,
  getDbConnectionFor,
  getEngineCatalog,
  testConnectDatabase,
  connectDatabase,
} from '../services/api.js'
import { useAppStore } from '../store/store.js'

const MAIN_DB = 'erp_main'
const DB_ID_RE = /^[a-z][a-z0-9_]{1,39}$/

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

// Admin-only card. The server is the real gate (403 for non-admins); if the GET
// fails for any reason this component simply renders nothing.
//
// Any database can be connected here. Leave "Database ID" empty and the server creates the ID from the
// database's name, makes its folder and records you as its owner. It stays inactive until its first
// successful sync, and only its owner (and users the owner shares it with) can use it. The main database
// (erp_main) can still be edited by the main admin by typing its ID.
export default function DatabaseConnectionCard() {
  const [cfg, setCfg] = useState(null) // engine list (+ ODBC default); null = loading, false = unavailable
  const [mainCfg, setMainCfg] = useState(null) // the main database's settings, only for the main admin
  const [dbId, setDbId] = useState('')
  const [source, setSource] = useState('') // which saved database the form currently holds ('' = blank/new)
  const [databases, setDatabases] = useState([])
  const [form, setForm] = useState({})
  const [passwordSet, setPasswordSet] = useState(false)
  const [busy, setBusy] = useState(null) // 'test' | 'save' | 'sync' | null
  const [error, setError] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const runSchemaSync = useAppStore((state) => state.runSchemaSync)

  const isMain = !!mainCfg && dbId === MAIN_DB
  const known = databases.some((d) => d.id === dbId)

  const mainForm = (c) => ({
    engine: c.engine,
    host: c.host,
    port: c.port,
    database: c.database,
    service_name: c.service_name || '',
    schema_name: c.schema_name || '',
    odbc_driver: c.odbc_driver || '',
    username: c.username,
  })

  const blankForm = (c) => {
    const engine = c.engines.find((e) => e.key === (mainCfg?.engine || 'postgresql')) || c.engines[0]
    return {
      engine: engine.key,
      host: '',
      port: engine.default_port ?? '',
      database: '',
      service_name: '',
      schema_name: '',
      odbc_driver: engine.key === 'mssql' ? c.odbc_driver || 'ODBC Driver 18 for SQL Server' : '',
      username: '',
    }
  }

  const refreshDatabases = useCallback(() => {
    listAdminDatabases()
      .then(setDatabases)
      .catch(() => setDatabases([]))
  }, [])

  useEffect(() => {
    getEngineCatalog()
      .then((c) => {
        setCfg(c)
        setForm(blankForm({ ...c }))
      })
      .catch(() => setCfg(false))
    // Only the main admin may read the main database's settings; everyone else just gets no erp_main option.
    getDbConnection()
      .then(setMainCfg)
      .catch(() => setMainCfg(null))
    refreshDatabases()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshDatabases])

  // Choosing another database id: load its saved connection, or start a blank form for a new one.
  // (Typing a brand-new id keeps what you have typed; only leaving a saved database clears the form,
  // so one database's host or user can never be saved under another id by accident.)
  const onDbId = (raw) => {
    const id = raw.trim().toLowerCase()
    setDbId(id)
    setError('')
    if (!cfg) return
    if (id === MAIN_DB && mainCfg) {
      setForm(mainForm(mainCfg))
      setPasswordSet(!!mainCfg.password_set)
      setSource(MAIN_DB)
    } else if (databases.some((d) => d.id === id)) {
      getDbConnectionFor(id)
        .then((c) => {
          setForm({ ...mainForm(c), engine: c.engine || cfg.engine })
          setPasswordSet(!!c.password_set)
          setSource(id)
        })
        .catch(() => {
          setForm(blankForm(cfg))
          setPasswordSet(false)
          setSource('')
        })
    } else if (source) {
      setForm(blankForm(cfg))
      setPasswordSet(false)
      setSource('')
    }
  }

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

  const idProblem = () =>
    !isMain && dbId && !DB_ID_RE.test(dbId)
      ? 'Database ID: use lowercase letters, numbers and underscores, start with a letter, 2 to 40 characters.'
      : ''

  const run = async (kind) => {
    const problem = idProblem()
    if (problem) {
      setError(problem)
      return
    }
    setBusy(kind)
    setError('')
    try {
      const payload = { ...form, db_id: dbId || undefined }
      if (kind === 'test') {
        await (isMain ? testDbConnection(form) : testConnectDatabase(payload))
        toast.success('Connection works')
      } else if (isMain) {
        const saved = await saveDbConnection(form)
        setMainCfg(saved)
        set({ password: undefined }) // never keep the typed password around; omitted = "keep stored"
        toast.success('Database connection saved', {
          description: 'Run "Sync database schema" so Antarkosh learns the new database.',
        })
      } else {
        const saved = await connectDatabase(payload)
        setDbId(saved.db_id)
        setSource(saved.db_id)
        set({ password: undefined })
        setPasswordSet(true)
        refreshDatabases()
        toast.success(saved.folder_created ? `Database "${saved.db_id}" added` : `Database "${saved.db_id}" saved`, {
          description: 'Now click "Sync schema". The database stays inactive until its first successful sync.',
        })
      }
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Request failed')
    } finally {
      setBusy(null)
    }
  }

  const sync = async () => {
    setBusy('sync')
    await runSchemaSync(dbId)
    refreshDatabases()
    setBusy(null)
  }

  const current = databases.find((d) => d.id === dbId)

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
          <span>Database ID</span>
          <input
            className="dialog-card__input"
            list="db-ids"
            value={dbId}
            placeholder="Automatic (from the database name)"
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => onDbId(e.target.value)}
          />
          <datalist id="db-ids">
            {databases.map((d) => d.id).map((id) => (
              <option key={id} value={id} />
            ))}
          </datalist>
        </label>
        <p className="settings-card__desc">
          {isMain
            ? 'erp_main is the main database. Clear this field to connect a different database.'
            : known
              ? `Editing "${dbId}"${current ? (current.enabled ? ' (active)' : ' (not active until synced)') : ''}${
                  current?.owner ? `, owner: ${current.owner}` : ''
                }${current?.synced_by ? `, last synced by ${current.synced_by}` : ''}.`
              : dbId
                ? `New database "${dbId}": saving creates its own folder.`
                : 'New database: its ID and folder are created automatically from the database name when you save.'}
        </p>

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
          {!isMain && known ? (
            <button type="button" className="sync-card__btn" disabled={!!busy} onClick={sync}>
              {busy === 'sync' ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
              <span>Sync schema</span>
            </button>
          ) : null}
        </div>
      </div>
    </section>
  )
}
