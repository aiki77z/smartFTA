import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ThemeProvider } from './components/ThemeProvider.jsx'
import FaultTreePage from './pages/FaultTreePage.jsx'
import ExplodedPage from './pages/ExplodedPage.jsx'
import HomePage from './pages/HomePage.jsx'
import ProjectListPage from './pages/ProjectListPage.jsx'
import './App.css'

function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<ProjectListPage />} />
          <Route path="/exploded" element={<ExplodedPage />} />
          <Route path="/project/:projectId" element={<HomePage />} />
          <Route path="/fta-viewer" element={<FaultTreePage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  )
}

export default App
