import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import { useAppStore } from './store/store.js'
import { ErrorBoundary } from './components/ErrorBoundary.jsx'
import '@fontsource/hanken-grotesk/400.css'
import '@fontsource/hanken-grotesk/500.css'
import '@fontsource/hanken-grotesk/600.css'
import '@fontsource/jetbrains-mono/400.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)

// Session ended on the server (401): drop local user state so the login page shows.
window.addEventListener('auth:expired', () => {
  if (useAppStore.getState().currentUser) {
    useAppStore.setState({
      currentUser: null, chats: [], messagesByChatId: {}, activeChatId: null,
      documents: [], overview: null, loading: false,
    })
  }
})
