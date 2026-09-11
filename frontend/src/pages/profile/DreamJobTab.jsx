/**
 * The "Dream job" tab on the profile screen (FR-109, FR-128).
 *
 * This tab used to embed the full dream-job editor, which meant the same editor
 * existed twice: here and on the /dream-job route. It is now a summary that
 * links to that single editor. The statement, the structured model, the build
 * and the confirm step all still exist - they just live in one place, so a
 * change made through either entry point is the same change.
 *
 * The summary is deliberately read-only: it says what is saved and whether the
 * model is built, current and confirmed, then hands off to the editor.
 */

import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import Icon from '../../components/Icon'
import { Badge, Empty, ErrorBox, Loading, formatDate, useFetch } from '../../components/ui'

const absent = (e) => {
  if (e.status === 404) return null
  throw e
}

export default function DreamJobTab() {
  const { data, error, loading, reload } = useFetch(async () => {
    const [statement, model] = await Promise.all([
      api.get('/profile/dream-job').catch(absent),
      // No model yet is an empty screen, not a failure.
      api.get('/enrichment/dream-job').catch(absent),
    ])
    return { statement: statement?.statement || '', model }
  }, [])

  if (loading) return <Loading rows={2} />
  if (error) return <ErrorBox error={error} onRetry={reload} />

  const text = data?.statement || ''
  const model = data?.model || null
  const words = text.trim() ? text.trim().split(/\s+/).filter(Boolean).length : 0
  // FR-128: the model is only meaningful against the text it was built from.
  const stale = Boolean(model && (model.statement || '').trim() !== text.trim())

  return (
    <div className="card phase-edge phase-1">
      <div className="card-header">
        <Icon name="dream" />
        <h3>Your dream job</h3>
        <div className="spacer" />
        {model?.confirmed_by_user && (
          <Badge tone="ok">
            <Icon name="check" />
            Confirmed
          </Badge>
        )}
        {model && !model.confirmed_by_user && (
          <Badge tone="warn">
            <Icon name="warning" />
            Model not confirmed
          </Badge>
        )}
      </div>

      {!text && !model ? (
        <Empty
          title="Nothing written yet"
          action={
            <Link className="btn btn-primary btn-sm" to="/dream-job">
              <Icon name="edit" />
              Write your dream-job statement
            </Link>
          }
        >
          The statement is the one thing the search cannot proceed without. Open the editor
          to write it in your own words, then build and confirm the model the search reads.
        </Empty>
      ) : (
        <>
          <p className="small muted" style={{ marginTop: 0 }}>
            {words > 0
              ? `${words} word${words === 1 ? '' : 's'} written${
                  model
                    ? ` · model version ${model.version} built ${formatDate(model.created_at)}`
                    : ''
                }.`
              : 'The statement is saved, but the structured model has not been built yet.'}
          </p>

          {text && (
            <p className="small" style={{ whiteSpace: 'pre-wrap', marginTop: 0 }}>
              {text.length > 320 ? `${text.slice(0, 320).trimEnd()}…` : text}
            </p>
          )}

          {stale && (
            <p className="small" style={{ marginTop: 0 }}>
              <Icon name="warning" /> The model was built from an older version of your
              statement, so it needs rebuilding before you rely on it.
            </p>
          )}
          {model && !model.confirmed_by_user && !stale && (
            <p className="small muted" style={{ marginTop: 0 }}>
              The model is not confirmed yet, so scoring does not prefer it.
            </p>
          )}

          <div className="row row-wrap" style={{ marginTop: 12 }}>
            <Link className="btn btn-sm btn-primary" to="/dream-job">
              <Icon name="edit" />
              {model ? 'Review and confirm the model' : 'Build the structured model'}
            </Link>
            <span className="small muted">
              Writing, building and confirming all happen on the Dream job screen.
            </span>
          </div>
        </>
      )}
    </div>
  )
}
