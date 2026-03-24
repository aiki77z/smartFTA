import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import FaultTreePage from './pages/FaultTreePage.jsx'
import HomePage from './pages/HomePage.jsx'
import ProjectListPage from './pages/ProjectListPage.jsx'
import './App.css'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<ProjectListPage />} />
        <Route path="/project/:projectId" element={<HomePage />} />
        <Route path="/fta-viewer" element={<FaultTreePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
