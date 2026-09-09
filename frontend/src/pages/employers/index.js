/**
 * The employer-kind surfaces, in one import.
 *
 * Four parts, one per place the question "is the organisation on this posting
 * the employer?" changes what a screen may say:
 *
 *   EmployerCompanyLine    the company column of a ranked-list or Apply
 *                          Browser row: two lines instead of one when the
 *                          employer is not the poster.
 *   UndisclosedEmployer    the opportunity screen: the agency card, the empty
 *                          employer card, and what could not be assessed.
 *   EmployerKindPanel      the company screen: the verdict in full, with the
 *                          cheapest next rung when there is no verdict.
 *   EmployerCoveragePanel  the administration screen: how much of the corpus
 *                          is answered, by rung, and what the gaps are.
 *
 * The badge itself lives in `components/EmployerKindBadge.jsx` because it is
 * rendered by screens that own none of this - the list, the browser, the
 * company profile - and it carries its own evidence popover and correction
 * modal, so one import gives a surface the whole behaviour.
 */

export { default as EmployerCompanyLine } from './EmployerCompanyLine'
export { default as EmployerKindPanel } from './EmployerKindPanel'
export { default as EmployerCoveragePanel } from './CoveragePanel'
export { default as UndisclosedEmployer, PostingOnBehalfNote, RECRUITER_QUESTIONS } from './UndisclosedEmployer'
