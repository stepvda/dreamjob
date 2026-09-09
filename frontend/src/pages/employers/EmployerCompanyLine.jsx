/**
 * The company column of a ranked-list row and of an Apply Browser row
 * (FR-282, FR-284, FR-321).
 *
 * On a direct employer this is the line the list has always shown. On an
 * agency row it becomes two lines, because one line cannot hold two
 * organisations without implying they are the same one:
 *
 *     Agency: NOEL FRANKLIN BV            [Via an agency — employer not disclosed]
 *     Employer: not named
 *
 * When the posting names a third party in a client-referring clause the second
 * line carries it as *named in the posting, not confirmed* - and it stays that
 * way until the named entity has been confirmed against a register, because a
 * relay title prefix is as likely to name another agency as an employer
 * (Randstad Digital, Talencia and Egov Select all appear as ICTJOB prefixes).
 */

import { Link } from 'react-router-dom'

import EmployerKindBadge, {
  ROLE_BOARD,
  employerRole,
  isIntermediary,
} from '../../components/EmployerKindBadge'

export default function EmployerCompanyLine({
  tag,
  companyId,
  companyName,
  namedInPosting,
  trailing,
  onChange,
}) {
  const id = companyId || tag?.company_id
  const name = companyName || tag?.company_name
  const role = employerRole(tag)
  const intermediary = isIntermediary(tag)
  const named = namedInPosting || tag?.employer_named_in_posting

  const nameNode = id ? (
    <Link to={`/companies/${id}`}>{name || 'Company profile'}</Link>
  ) : (
    <span>{name || 'Company not identified'}</span>
  )

  return (
    <span className="opp-company">
      <span className="row row-wrap" style={{ gap: 6 }}>
        {intermediary && <span className="muted">{role === ROLE_BOARD ? 'Job board:' : 'Agency:'}</span>}
        {nameNode}
        <EmployerKindBadge
          tag={tag}
          companyId={id}
          companyName={name}
          onChange={onChange}
        />
        {trailing}
      </span>
      {intermediary && (
        <span className="tiny muted" style={{ display: 'block', marginTop: 2 }}>
          {named
            ? `Employer named in the posting: ${named} — not confirmed`
            : 'Employer: not named'}
        </span>
      )}
    </span>
  )
}
