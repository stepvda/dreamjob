import { createContext, useContext } from 'react'

/**
 * The signed-in job seeker. Every private screen reads it from here rather
 * than re-fetching, so the isolation boundary (FR-101, FR-344) has one
 * client-side representation.
 */
export const SessionContext = createContext(null)

export function useSession() {
  const ctx = useContext(SessionContext)
  if (!ctx) throw new Error('useSession must be used inside a SessionContext provider')
  return ctx
}
