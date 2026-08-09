import { createRoot } from 'react-dom/client'
import '@fontsource-variable/manrope'
import App from './App'
import './theme.css'
import './styles.css'

const root = document.getElementById('root')

if (!root) {
  throw new Error('No se encontró el contenedor raíz de la aplicación.')
}

createRoot(root).render(<App />)
