/**
 * The last line of defence around the routed area (NFR-701).
 *
 * A render-time exception in React unmounts the whole tree: without a
 * boundary, a single bad field on one screen leaves a blank white page, and
 * the person using it has nothing to report but "it went blank". This catches
 * that, writes the error and the component stack to the client log, and shows
 * a screen that says what happened, offers a way out, and names the
 * correlation id that lets the same incident be found in the backend log.
 */

import { Component } from 'react'

import Icon from '../components/Icon'
import { correlationId, log } from './logger'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { failed: false, correlation: null }
  }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error, info) {
    // The component stack is the one piece of context a stack trace alone does
    // not give: which screen was rendering when it broke.
    log.error('react render failed', {
      error,
      component_stack: info?.componentStack,
    })
    this.setState({ correlation: correlationId() })
  }

  render() {
    if (!this.state.failed) return this.props.children

    const { correlation } = this.state
    return (
      <div className="empty" style={{ paddingTop: 120 }}>
        <h3>
          <Icon name="warning" /> This screen stopped responding
        </h3>
        <p>
          Nothing you have entered is lost — the problem is in how this screen was drawn,
          not in your data. Reloading usually clears it.
        </p>
        <div className="row" style={{ justifyContent: 'center' }}>
          <button className="btn btn-primary" onClick={() => window.location.reload()}>
            <Icon name="refresh" /> Reload this screen
          </button>
          <button className="btn btn-ghost" onClick={() => window.location.assign('/overview')}>
            Back to the overview
          </button>
        </div>
        {correlation && (
          <p className="tiny muted" style={{ marginTop: 18 }}>
            If it happens again, quote reference <span className="mono">{correlation}</span>.
          </p>
        )}
      </div>
    )
  }
}
