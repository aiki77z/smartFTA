import { IconMoon, IconSun } from './icons.jsx'
import { useTheme } from './ThemeProvider.jsx'
import './theme-toggle.css'

/**
 * @param {{ variant?: 'default' | 'fta', className?: string }} props
 */
export default function ThemeToggle({ variant = 'default', className = '' }) {
  const { theme, toggleTheme } = useTheme()
  const base =
    variant === 'fta'
      ? 'theme-toggle-btn fta-btn ghost theme-toggle-btn--icon'
      : 'theme-toggle-btn theme-toggle-btn--icon'
  const merged = [base, className].filter(Boolean).join(' ')

  return (
    <button
      type="button"
      className={merged}
      onClick={toggleTheme}
      aria-pressed={theme === 'dark'}
      title={theme === 'light' ? '切换到夜间模式' : '切换到日间模式'}
      aria-label={theme === 'light' ? '切换到夜间模式' : '切换到日间模式'}
    >
      {theme === 'light' ? <IconMoon /> : <IconSun />}
    </button>
  )
}
