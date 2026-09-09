/**
 * The opportunity screen when the employer is not disclosed (FR-261, FR-282,
 * FR-330, FR-383, CR-405).
 *
 * Where a normal opportunity has one company card, an agency posting has two,
 * and the second one is mostly empty on purpose:
 *
 *   the agency    who posted it, what kind of agency it is, and the evidence
 *                 for saying so - with "This is wrong" one click away.
 *   the employer  "Not named by the agency", followed by what the posting
 *                 itself says about them, verbatim and attributed.
 *
 * The third block is the one this feature exists for. A score of 61 computed
 * without a company dimension looks exactly like a score of 61 computed with
 * one, so the screen has to say which parts of the assessment could not be
 * made and why - and that the missing part is recoverable by asking the
 * recruiter who the client is, which is a question a job seeker is entitled to
 * ask and which the product otherwise never prompts.
 *
 * Nothing here guesses the employer. 83% of agency rows name no client
 * anywhere, the eight richest client descriptions in the corpus resolved to
 * zero companies against the Belgian register, and a fabricated employer in a
 * letter is read by the recruiter who knows the real one - the same class of
 * error as an invented e-mail address (proposal section 5.1).
 */

import { useState } from 'react'

import { Caution, HelpTip } from '../../components/Help'
import EmployerKindBadge, {
  EmployerCorrectionModal,
  EmployerEvidence,
  ROLE_BOARD,
  isIntermediary,
  serviceModelWords,
} from '../../components/EmployerKindBadge'
import { SectionCard } from '../../components/ui'

/** The four questions that turn an unnamed employer into a named one. */
export const RECRUITER_QUESTIONS = [
  'Which company is the employer, and may I know its name before the interview?',
  'Why is the role open — is it growth, a replacement, or a new team?',
  'Is this a direct hire by that employer, or an interim contract through you?',
  'At what point in the process is the employer named?',
]

/**
 * The disclosure, the two cards and the assessment note.
 *
 * `tag` is the payload from `/api/employers/{id}/kind` or the tag the
 * opportunity endpoint joins onto the row; `descriptors` and `disclosureNote`
 * fall back to the posting-level fields when the opportunity carries them
 * itself.
 */
export default function UndisclosedEmployer({
  tag,
  companyId,
  companyName,
  descriptors,
  disclosureNote,
  onChange,
}) {
  const [correcting, setCorrecting] = useState(false)

  if (!tag || !isIntermediary(tag)) return null

  const id = companyId || tag.company_id
  const name = companyName || tag.company_name || 'The posting’s author'
  const said = descriptors || tag.employer_descriptors || []
  const note = disclosureNote || tag.disclosure_note
  const dimensions = tag.dimensions_not_assessed || []
  const board = tag.employer_role === ROLE_BOARD
  const model = serviceModelWords(tag.service_model)

  return (
    <>
      {/* The same treatment FR-263 gives a speculative opening: the backend's
          own sentence, shown verbatim rather than paraphrased. */}
      {note && (
        <Caution title={board ? 'This vacancy came through a job board' : 'This vacancy does not name the employer'}>
          {note} <HelpTip term="employer_not_disclosed" />
        </Caution>
      )}

      <div className="grid grid-2" style={{ marginTop: 14 }}>
        <SectionCard icon="companies" title={board ? 'The job board' : 'The agency'} phase="phase-3">
          <div className="row row-wrap" style={{ gap: 8 }}>
            <strong>{name}</strong>
            <EmployerKindBadge
              tag={tag}
              companyId={id}
              companyName={name}
              onChange={onChange}
            />
          </div>
          {model && <div className="small muted" style={{ marginTop: 4 }}>{model}</div>}
          <div className="col small" style={{ gap: 6, marginTop: 12 }}>
            <EmployerEvidence
              tag={tag}
              companyId={id}
              companyName={name}
              compact={false}
              onCorrect={() => setCorrecting(true)}
            />
          </div>
        </SectionCard>

        <SectionCard icon="profile" title="The employer" phase="phase-3">
          <p className="section-intro" style={{ marginTop: 0 }}>
            Not named by {board ? 'the board' : 'the agency'}. Everything below is what this
            posting says about them, in its own words.
          </p>
          {said.length > 0 ? (
            <div className="col" style={{ gap: 8 }}>
              {said.map((sentence, i) => (
                <q key={i} className="small" style={{ lineHeight: 1.5 }}>
                  {sentence}
                </q>
              ))}
            </div>
          ) : (
            <p className="small muted">
              The posting says nothing about the employer beyond the role itself. We do not infer
              one: a sector and a town is not a company, and a guessed name would be read by the
              recruiter who knows the real one.
            </p>
          )}
          <p className="small muted" style={{ marginTop: 12 }}>
            The profile, the accounts and the competitor comparison read “not available: the
            employer is not named” rather than showing {name}’s. A staffing agency’s balance sheet
            says nothing about its client’s ability to pay.
          </p>
        </SectionCard>
      </div>

      <SectionCard
        icon="target"
        title="What this means for the assessment"
        phase="phase-3"
        edge={false}
      >
        <p className="section-intro" style={{ marginTop: 0 }}>
          The score rests on the role: the title, the duties, the skills, the contract and the
          region. These are the parts that need an employer, and they were not computed rather
          than estimated — an invented number about the wrong organisation is worse than an
          honest gap.
        </p>
        <ul className="col small" style={{ gap: 4, margin: '0 0 12px', paddingLeft: 18 }}>
          {(dimensions.length ? dimensions : DEFAULT_DIMENSIONS).map((d) => (
            <li key={d}>{d} — shown as “—”, not as a guess</li>
          ))}
        </ul>
        <h4 style={{ fontSize: 12.5, margin: '0 0 6px' }}>Ask the recruiter</h4>
        <ol className="col small" style={{ gap: 4, margin: 0, paddingLeft: 18 }}>
          {RECRUITER_QUESTIONS.map((q) => (
            <li key={q}>{q}</li>
          ))}
        </ol>
        <p className="small muted" style={{ marginTop: 10 }}>
          The moment the recruiter names the employer, regenerate the briefing: it is then written
          against the real company, with its profile and its accounts (FR-329).
        </p>
      </SectionCard>

      {correcting && (
        <EmployerCorrectionModal
          tag={tag}
          companyId={id}
          companyName={name}
          onClose={() => setCorrecting(false)}
          onSaved={(payload) => {
            setCorrecting(false)
            onChange?.(payload?.verdict || null)
          }}
        />
      )}
    </>
  )
}

/** Used when the server sent no explicit list (it normally does). */
const DEFAULT_DIMENSIONS = [
  'company attractiveness',
  'financial trajectory and ability to pay',
  'values and culture match',
]

/**
 * A direct employer whose *own* posting speaks of a client.
 *
 * GitLab has 28 loose client-sounding hits in 225 adverts and is not an
 * agency; Smals recruits a "Relations Partner Detachment" for itself. So a
 * single posting's phrasing is shown as a note and never reclassifies the
 * employer (proposal section 2.2).
 */
export function PostingOnBehalfNote({ tag, descriptors }) {
  if (!tag?.posting_on_behalf || isIntermediary(tag)) return null
  const said = descriptors || tag.employer_descriptors || []
  return (
    <div className="alert alert-info" style={{ marginTop: 12 }}>
      <div>
        <strong>This posting mentions a client.</strong> {tag.company_name || 'This employer'} is a
        direct employer on the evidence we have, so the assessment is unchanged; the posting’s own
        wording suggests the work may be at one of its clients.
        {said.length > 0 && <> It says: “{said[0]}”.</>}
      </div>
    </div>
  )
}
