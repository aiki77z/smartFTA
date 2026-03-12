import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import FaultTreePage from './pages/FaultTreePage.jsx'
import './App.css'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/fta-viewer" replace />} />
        <Route path="/fta-viewer" element={<FaultTreePage />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
