import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { ThemeProvider } from './components/ThemeProvider.jsx'
import FaultTreePage from './pages/FaultTreePage.jsx'
import ExplodedPage from './pages/ExplodedPage.jsx'
import HomePage from './pages/HomePage.jsx'
import ProjectListPage from './pages/ProjectListPage.jsx'
import './App.css'

/** 查询串（canvasId / faultTreeId 等）变化时重新挂载，避免画布状态串会话 */
function FaultTreeRoute() {
  const { search } = useLocation()
  return <FaultTreePage key={search || 'default'} />
}

function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<ProjectListPage />} />
          <Route path="/exploded" element={<ExplodedPage />} />
          <Route path="/project/:projectId" element={<HomePage />} />
          <Route path="/fta-viewer" element={<FaultTreeRoute />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  )
}

export default App
