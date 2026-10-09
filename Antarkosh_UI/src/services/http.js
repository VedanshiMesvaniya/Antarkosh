import axios from 'axios'

export const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  headers: {
    'Content-Type': 'application/json',
  },
})

// A 401 on a data call means the session ended: tell the app so it can show the login page.
http.interceptors.response.use(
  (response) => response,
  (error) => {
    const url = String(error?.config?.url || '')
    if (error?.response?.status === 401 && !url.startsWith('/auth/')) {
      window.dispatchEvent(new Event('auth:expired'))
    }
    return Promise.reject(error)
  },
)
