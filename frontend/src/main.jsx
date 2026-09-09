import React from 'react'
import ReactDOM from 'react-dom/client'

import App from './App'
import ErrorBoundary from './observability/ErrorBoundary'
import { installGlobalHandlers } from './observability/logger'
import './styles/app.css'
import './styles/theme.css'

// NFR-701: installed before the first render, so a failure during boot - the
// session check, a lazy chunk that will not load - is logged like any other.
installGlobalHandlers()

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
