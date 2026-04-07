import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
} from 'react'

export const THEME_STORAGE_KEY = 'fta-theme'

const ThemeContext = createContext(null)

function readStoredTheme() {
  if (typeof window === 'undefined') return 'light'
  try {
    const s = localStorage.getItem(THEME_STORAGE_KEY)
    if (s === 'dark' || s === 'light') return s
  } catch {
    // ignore
  }
  return 'light'
}

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(readStoredTheme)

  const setTheme = useCallback((next) => {
    setThemeState((prev) => {
      const resolved = typeof next === 'function' ? next(prev) : next
      return resolved === 'dark' || resolved === 'light' ? resolved : prev
    })
  }, [])

  const toggleTheme = useCallback(() => {
    setThemeState((t) => (t === 'light' ? 'dark' : 'light'))
  }, [])

  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme)
    } catch {
      // ignore
    }
  }, [theme])

  const value = useMemo(
    () => ({
      theme,
      setTheme,
      toggleTheme,
    }),
    [theme, setTheme, toggleTheme],
  )

  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  )
}

export function useTheme() {
  const ctx = useContext(ThemeContext)
  if (!ctx) {
    throw new Error('useTheme must be used within ThemeProvider')
  }
  return ctx
}
