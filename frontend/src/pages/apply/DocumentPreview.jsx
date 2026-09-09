/**
 * A generated PDF, shown rather than described.
 *
 * The document routes are same-origin, so the browser sends the session cookie
 * with an `<iframe>` request on its own and the file needs no blob juggling in
 * JavaScript. Using the browser's own PDF viewer also means the preview is the
 * real file — the same bytes that would be attached or saved — instead of a
 * re-rendering of it that could differ from what a recipient opens.
 *
 * The empty state is deliberately specific. "Not generated yet" and "generated
 * but the file is gone from disk" are different problems with different fixes,
 * and the backend distinguishes them, so the panel does too.
 */

import Icon from '../../components/Icon'

export default function DocumentPreview({ url, label, available, height = 560, actions }) {
  if (!available) {
    return (
      <div className="empty" style={{ padding: '32px 20px' }}>
        <h3>{label} has not been generated</h3>
        <p>
          Generate the package for this job and the document appears here. Nothing is invented for
          a preview.
        </p>
        {actions}
      </div>
    )
  }

  return (
    <div className="col" style={{ gap: 8 }}>
      <div
        style={{
          border: '1px solid var(--line)',
          borderRadius: 'var(--radius)',
          overflow: 'hidden',
          background: 'var(--surface-sunk)',
        }}
      >
        <iframe
          src={`${url}#view=FitH`}
          title={label}
          style={{ width: '100%', height, border: 0, display: 'block' }}
        />
      </div>
      <div className="row row-wrap small muted">
        <Icon name="eye" />
        <span>
          This is the file itself, opened in your browser’s PDF viewer. If it stays blank, your
          browser is set to download PDFs rather than display them — use the download button.
        </span>
        <div className="spacer" />
        {actions}
      </div>
    </div>
  )
}
