import { Check, Palette } from 'lucide-react'
import { motion } from 'framer-motion'
import { useAppStore } from '../store/store.js'

const THEME_OPTIONS = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
  { value: 'system', label: 'System' },
]

function ThemePreview({ mode }) {
  return (
    <span className={`theme-preview theme-preview--${mode}`} aria-hidden="true">
      <span className="theme-preview__sidebar">
        <span className="theme-preview__brand" />
        <span className="theme-preview__nav-item" />
        <span className="theme-preview__nav-item" />
        <span className="theme-preview__nav-item" />
      </span>
      <span className="theme-preview__main">
        <span className="theme-preview__topbar" />
        <span className="theme-preview__messages">
          <span className="theme-preview__msg theme-preview__msg--assistant" />
          <span className="theme-preview__msg theme-preview__msg--user" />
          <span className="theme-preview__msg theme-preview__msg--assistant theme-preview__msg--short" />
        </span>
        <span className="theme-preview__composer" />
      </span>
    </span>
  )
}

export default function Settings() {
  const settings = useAppStore((state) => state.settings)
  const updateSettings = useAppStore((state) => state.updateSettings)
  const current = settings || { theme: 'light' }
  const setSetting = (patch) => updateSettings(patch)

  return (
    <section className="page settings-page">
      <div className="settings-page__header">
        <h2 className="settings-page__title">Settings</h2>
        <p className="settings-page__subtitle">Appearance preferences.</p>
      </div>

      <motion.section
        className="settings-card"
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div className="settings-card__heading">
          <Palette size={15} />
          <h3 className="settings-card__title">Appearance</h3>
        </div>
        <p className="settings-card__desc">Choose how Antarkosh looks.</p>

        <div className="theme-picker" role="radiogroup" aria-label="Theme">
          {THEME_OPTIONS.map((option) => {
            const selected = current.theme === option.value
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={selected}
                className={`theme-option ${selected ? 'theme-option--selected' : ''}`}
                onClick={() => setSetting({ theme: option.value })}
              >
                <ThemePreview mode={option.value} />
                <span className="theme-option__label">
                  {option.label}
                  {selected ? <Check size={12} strokeWidth={3} className="theme-option__check" /> : null}
                </span>
              </button>
            )
          })}
        </div>
      </motion.section>
    </section>
  )
}
