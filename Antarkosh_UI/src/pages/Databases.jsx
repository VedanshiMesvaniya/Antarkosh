import { useCallback, useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { toast } from 'sonner'
import CurrentDatabaseCard from '../components/CurrentDatabaseCard.jsx'
import DatabaseConnectionCard from '../components/DatabaseConnectionCard.jsx'
import { getMyDatabase, listAdminDatabases, setMyDatabase } from '../services/api.js'
import { useAppStore } from '../store/store.js'

const MAIN_DB = 'erp_main'

const loadState = () =>
  Promise.all([listAdminDatabases().catch(() => null), getMyDatabase().catch(() => null)])

// Database settings (database admins only). Only ONE connection is shown: the database your questions use,
// or the one you just saved. Other saved databases are reachable from the "Switch" dropdown. The connection
// form is hidden until you press Edit or Add database. Database IDs are created automatically.
export default function Databases() {
  const isAdmin = useAppStore((state) => state.isAdmin)
  const runSchemaSync = useAppStore((state) => state.runSchemaSync)
  const [databases, setDatabases] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [currentId, setCurrentId] = useState(null) // the database MY questions are answered from
  const [shownId, setShownId] = useState(null) // null = show the current one
  const [form, setForm] = useState(null) // null | 'add' | 'edit'
  const [busy, setBusy] = useState(false)
  const [syncing, setSyncing] = useState(false)

  const apply = useCallback(([list, mine]) => {
    if (list) setDatabases(list)
    if (mine) setCurrentId(mine.effective)
    setLoaded(true)
  }, [])

  const refresh = () => loadState().then(apply)

  useEffect(() => {
    if (!isAdmin) return undefined
    let alive = true
    loadState().then((r) => {
      if (alive) apply(r)
    })
    return () => {
      alive = false
    }
  }, [isAdmin, apply])

  if (!isAdmin) return <Navigate to="/" replace />

  const shown = databases.find((d) => d.id === (shownId ?? currentId)) || databases.find((d) => d.id === currentId) || databases[0] || null
  const showForm = form !== null || (loaded && databases.length === 0)

  const sync = async () => {
    setBusy(true)
    setSyncing(true)
    try {
      await runSchemaSync(shown.id) // shows its own progress / result toasts
    } finally {
      setSyncing(false)
      setBusy(false)
      refresh()
    }
  }

  // Choosing a synced database also makes it the one your questions use. A not-yet-synced one is only shown.
  const switchTo = async (id) => {
    setShownId(id)
    setForm(null)
    const db = databases.find((d) => d.id === id)
    if (!db || !db.enabled || id === currentId) return
    setBusy(true)
    try {
      const saved = await setMyDatabase(id === MAIN_DB ? null : id)
      setCurrentId(saved.effective)
      toast.success(`Your questions now use "${db.name}"`)
    } catch (e) {
      setShownId(null)
      toast.error(e.response?.data?.detail || e.message || 'Could not change the database')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="page settings-page">
      <div className="settings-page__header">
        <h2 className="settings-page__title">Databases</h2>
        <p className="settings-page__subtitle">Your current connection. Other saved databases are in the Switch menu.</p>
      </div>

      {shown ? (
        <CurrentDatabaseCard
          db={shown}
          databases={databases}
          busy={busy}
          syncing={syncing}
          onSwitch={switchTo}
          onAdd={() => setForm('add')}
          onEdit={() => setForm('edit')}
          onSync={sync}
        />
      ) : null}

      {showForm ? (
        <DatabaseConnectionCard
          key={form === 'edit' && shown ? shown.id : 'new'}
          editingId={form === 'edit' && shown ? shown.id : null}
          editingName={shown?.name}
          onSaved={(id) => {
            setShownId(id) // show what was just saved
            setForm(null)
            refresh()
          }}
          onCancel={databases.length ? () => setForm(null) : undefined}
        />
      ) : null}
    </section>
  )
}
