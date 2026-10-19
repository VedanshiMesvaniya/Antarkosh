import { useCallback, useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { toast } from 'sonner'
import DatabaseConnectionCard from '../components/DatabaseConnectionCard.jsx'
import DatabaseList from '../components/DatabaseList.jsx'
import { getMyDatabase, listAdminDatabases, setMyDatabase } from '../services/api.js'
import { useAppStore } from '../store/store.js'

const MAIN_DB = 'erp_main'

const loadState = () =>
  Promise.all([listAdminDatabases().catch(() => null), getMyDatabase().catch(() => null)])

// All database settings live here (database admins only): connect a database, edit it, sync its schema,
// and see which connection is active. Database IDs are created automatically; nobody types one.
export default function Databases() {
  const isDatabaseAdmin = useAppStore((state) => state.isDatabaseAdmin)
  const runSchemaSync = useAppStore((state) => state.runSchemaSync)
  const [databases, setDatabases] = useState([])
  const [currentId, setCurrentId] = useState(null) // the database MY questions are answered from
  const [editingId, setEditingId] = useState(null) // null = the "add a database" form
  const [busyId, setBusyId] = useState(null)

  const apply = useCallback(([list, mine]) => {
    if (list) setDatabases(list)
    if (mine) setCurrentId(mine.effective)
  }, [])

  const refresh = () => loadState().then(apply)

  useEffect(() => {
    if (!isDatabaseAdmin) return undefined
    let alive = true
    loadState().then((r) => {
      if (alive) apply(r)
    })
    return () => {
      alive = false
    }
  }, [isDatabaseAdmin, apply])

  if (!isDatabaseAdmin) return <Navigate to="/" replace />

  const sync = async (id) => {
    setBusyId(id)
    try {
      await runSchemaSync(id) // shows its own progress / result toasts
    } finally {
      setBusyId(null)
      refresh()
    }
  }

  const use = async (id) => {
    setBusyId(id)
    try {
      const saved = await setMyDatabase(id === MAIN_DB ? null : id)
      setCurrentId(saved.effective)
      toast.success(`Your questions now use "${saved.effective}"`)
    } catch (e) {
      toast.error(e.response?.data?.detail || e.message || 'Could not change the database')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="page settings-page">
      <div className="settings-page__header">
        <h2 className="settings-page__title">Databases</h2>
        <p className="settings-page__subtitle">Connect databases, keep their schema up to date, and see which one is active.</p>
      </div>

      <DatabaseList
        databases={databases}
        currentId={currentId}
        busyId={busyId}
        onAdd={() => setEditingId(null)}
        onEdit={setEditingId}
        onSync={sync}
        onUse={use}
      />

      <DatabaseConnectionCard
        key={editingId || 'new'}
        editingId={editingId}
        editingName={databases.find((d) => d.id === editingId)?.name}
        onSaved={() => {
          setEditingId(null) // back to a blank "Add a database" form; the list shows what was saved
          refresh()
        }}
        onCancel={() => setEditingId(null)}
      />
    </section>
  )
}
