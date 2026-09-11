/**
 * How much an address can be trusted (FR-303).
 *
 * A published address — read off the company's own site or a press page — needs
 * no warning. An inferred one was *composed* from the domain's convention
 * (firstname.lastname@, f.lastname@, …) and never confirmed against the mail
 * server, so it is a guess. The screen must never let that guess read as a
 * valid address, so it is marked wherever the address is listed.
 *
 * Two signals say "inferred": the API's own `email_uncertain` flag, or the
 * source method `pattern_inference` when validation has not come back valid.
 * Both are treated the same, because either one alone is enough to mean the
 * address was not published.
 */

import Icon from '../../components/Icon'
import { HelpTip } from '../../components/Help'
import { Badge } from '../../components/ui'

export function isUncertainEmail(contact) {
  if (!contact) return false
  if (Number(contact.email_uncertain) === 1) return true
  return (
    contact.email_source_method === 'pattern_inference' &&
    contact.email_validation !== 'valid'
  )
}

export default function EmailCertaintyBadge({ contact }) {
  if (!isUncertainEmail(contact)) return null
  return (
    <span className="nowrap">
      <Badge tone="warn">
        <Icon name="warning" /> unverified · inferred from domain pattern
      </Badge>
      <HelpTip title="Unverified address">
        This address was composed from the e-mail convention used elsewhere on
        the company&apos;s domain. It could not be confirmed against the mail
        server, so it is a guess — never treat it as a valid address.
      </HelpTip>
    </span>
  )
}
