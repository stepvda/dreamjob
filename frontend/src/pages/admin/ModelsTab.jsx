/**
 * Models: provider, per-task routing, budgets and cost (FR-362, NFR-306).
 *
 * Every setting is shown as its *effective* value — the .env default with the
 * administrator's override on top — because that is the only figure that
 * explains behaviour, and because an override with no visible default is how
 * an installation quietly drifts from what its operator thinks it is doing.
 * The precedence is the backend's own (llm/client.route), so this screen only
 * has to display it, never re-derive it.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import {
  Badge,
  ChipSelect,
  ErrorBox,
  Field,
  Loading,
  SectionCard,
  useFetch,
} from '../../components/ui'
import { eur, num, tokens as fmtTokens } from './format'
import TaskRoutingTable from './TaskRouting'

/**
 * NFR-306 names exactly three privacy-sensitive steps, and llm/client.py
 * matches them by task id, group and suffix. These are the canonical ids; a
 * task outside this set can be named in `local_tasks` and will still be sent
 * to the provider, so offering the wider set would be a lie.
 */
const PRIVACY_TASKS = [
  { value: 'profile.composite', label: 'Composite profile' },
  { value: 'generate.cv', label: 'Tailored CV' },
  { value: 'generate.motivation', label: 'Motivation document' },
]


/**
 * Emptying a field means "I have not changed this", not "erase it" — the PUT
 * ignores nulls but would happily store an empty provider or an empty model
 * name, which breaks every later call. The two local fields are the exception:
 * blanking the base URL is how local routing is switched off.
 */
const CLEARABLE = new Set(['local_base_url', 'local_model'])

const NUMERIC = new Set([
  'default_token_budget',
  'cost_per_1m_input_eur',
  'cost_per_1m_output_eur',
  'budget_degrade_at',
])

export default function ModelsTab() {
  const config = useFetch(() => api.get('/admin/llm-config'), [])
  const routing = useFetch(() => api.get('/admin/llm-config/task-routing'), [])
  const overview = useFetch(() => api.get('/admin/overview'), [])

  const [draft, setDraft] = useState(null)
  const [syncedFrom, setSyncedFrom] = useState(null)
  const [busy, setBusy] = useState(false)
  const [saveError, setSaveError] = useState(null)
  const [saved, setSaved] = useState(false)

  /* The form is seeded from the fetched configuration, and re-seeded whenever a
     save or a reset brings a new one back. Adjusted during render rather than
     in an effect so the form never shows one frame of the previous values. */
  if (config.data?.effective && config.data.effective !== syncedFrom) {
    setSyncedFrom(config.data.effective)
    setDraft({ ...config.data.effective })
  }

  if (config.loading || !draft) return <Loading rows={6} />
  if (config.error) return <ErrorBox error={config.error} onRetry={config.reload} />

  const effective = config.data.effective
  const defaults = config.data.defaults_from_env || {}
  const overrides = config.data.overrides || {}
  const byTask = overview.data?.llm_by_task || []

  /** `overrides` is keyed with the "llm." prefix the backend stores under. */
  const isOverridden = (key) => Object.prototype.hasOwnProperty.call(overrides, `llm.${key}`)

  const dirty = Object.keys(draft).filter(
    (k) => JSON.stringify(draft[k]) !== JSON.stringify(effective[k]),
  )

  function set(key, value) {
    setSaved(false)
    setDraft((d) => ({ ...d, [key]: value }))
  }

  async function save() {
    setBusy(true)
    setSaveError(null)
    try {
      const payload = {}
      for (const k of dirty) {
        const v = draft[k]
        if (v === '' && !CLEARABLE.has(k)) continue
        payload[k] = NUMERIC.has(k) ? Number(v) : v
      }
      await api.put('/admin/llm-config', payload)
      setSaved(true)
      config.reload()
      routing.reload()
    } catch (err) {
      setSaveError(err)
    } finally {
      setBusy(false)
    }
  }

  /** Drop one override so the .env default is in force again (FR-362). */
  async function reset(key) {
    setBusy(true)
    setSaveError(null)
    try {
      await api.del(`/admin/llm-config/${key}`)
      config.reload()
      routing.reload()
    } catch (err) {
      setSaveError(err)
    } finally {
      setBusy(false)
    }
  }

  const localConfigured = Boolean(draft.local_base_url)
  const localTasks = new Set(draft.local_tasks || [])
  /* NFR-306: a task routes locally only when it is privacy-sensitive, named in
     local_tasks, AND an endpoint exists. Two of the three are settings on this
     page, so the third has to be shown next to them or the setting reads as
     effective when it is not. */
  const remoteSensitive = PRIVACY_TASKS.filter(
    (t) => !(localConfigured && localTasks.has(t.value)),
  )

  const totals = byTask.reduce(
    (a, r) => ({
      calls: a.calls + (r.calls || 0),
      tokens: a.tokens + (r.tokens || 0),
      cost: a.cost + (r.cost_eur || 0),
    }),
    { calls: 0, tokens: 0, cost: 0 },
  )

  return (
    <div className="stack">
      {saveError && <ErrorBox error={saveError} />}

      <SectionCard
        icon="admin"
        title="Provider and models"
        phase="phase-0"
        actions={
          <div className="row">
            {saved && !dirty.length && <span className="small muted">Saved.</span>}
            <button
              className="btn btn-sm btn-primary"
              disabled={busy || !dirty.length}
              onClick={save}
            >
              {busy ? (
                <span className="spinner" />
              ) : dirty.length ? (
                `Save ${dirty.length} change${dirty.length === 1 ? '' : 's'}`
              ) : (
                'Save'
              )}
            </button>
          </div>
        }
      >
        <p className="small muted" style={{ marginTop: 0 }}>
          The value in force is the <code>.env</code> default with your override on top.
          <HelpTip term="effective_setting" /> A change takes effect on the next call — the
          client's configuration cache is dropped when you save.
        </p>

        <div className="grid grid-2">
          <SettingField
            label="Provider"
            name="provider"
            value={draft.provider}
            fallback={defaults.provider}
            overridden={isOverridden('provider')}
            onChange={set}
            onReset={reset}
            hint="OpenAI-compatible provider key, e.g. deepseek."
          />
          <Field label="API key">
            <div className="row">
              {config.data.api_key_configured ? (
                <Badge tone="ok">Configured</Badge>
              ) : (
                <Badge tone="danger">Missing</Badge>
              )}
              <span className="small muted">
                Held in <code>.env</code> only — never editable from a browser.
              </span>
            </div>
          </Field>
          <SettingField
            label="Cheap model"
            name="model_cheap"
            value={draft.model_cheap}
            fallback={defaults.model_cheap}
            overridden={isOverridden('model_cheap')}
            onChange={set}
            onReset={reset}
            hint="Bulk work: page summaries, skill normalisation."
          />
          <SettingField
            label="Strong model"
            name="model_strong"
            value={draft.model_strong}
            fallback={defaults.model_strong}
            overridden={isOverridden('model_strong')}
            onChange={set}
            onReset={reset}
            hint="Anything a task has not been routed away from."
          />
        </div>
      </SectionCard>

      <SectionCard icon="money" title="Budget and price" phase="phase-0">
        <div className="grid grid-2">
          <SettingField
            label={<>Default token budget <HelpTip term="token_budget" /></>}
            name="default_token_budget"
            type="number"
            value={draft.default_token_budget}
            fallback={defaults.default_token_budget}
            overridden={isOverridden('default_token_budget')}
            onChange={set}
            onReset={reset}
            hint="Per campaign, unless the campaign sets its own."
          />
          <SettingField
            label={<>Degrade threshold <HelpTip term="budget_degrade" /></>}
            name="budget_degrade_at"
            type="number"
            step="0.05"
            value={draft.budget_degrade_at}
            fallback={0.85}
            overridden={isOverridden('budget_degrade_at')}
            onChange={set}
            onReset={reset}
            hint="0–1. Above this share of the budget, optional work is shed."
          />
          <SettingField
            label="Input price, € per 1M tokens"
            name="cost_per_1m_input_eur"
            type="number"
            step="0.01"
            value={draft.cost_per_1m_input_eur}
            fallback={defaults.cost_per_1m_input_eur}
            overridden={isOverridden('cost_per_1m_input_eur')}
            onChange={set}
            onReset={reset}
            hint="Every cost figure in the product is computed from this."
          />
          <SettingField
            label="Output price, € per 1M tokens"
            name="cost_per_1m_output_eur"
            type="number"
            step="0.01"
            value={draft.cost_per_1m_output_eur}
            fallback={defaults.cost_per_1m_output_eur}
            overridden={isOverridden('cost_per_1m_output_eur')}
            onChange={set}
            onReset={reset}
            hint="Prices are not fetched from the provider; keep them current."
          />
        </div>

        <div className="row row-wrap" style={{ marginTop: 6 }}>
          <span className="small muted">Spent so far on this installation:</span>
          <Badge>{num(totals.calls)} calls</Badge>
          <Badge>{fmtTokens(totals.tokens)} tokens</Badge>
          <Badge tone="accent">{eur(totals.cost)}</Badge>
          {overview.loading && <span className="spinner" />}
        </div>
      </SectionCard>

      <SectionCard
        icon="lock"
        title="Local model for privacy-sensitive tasks"
        phase="phase-0"
        actions={
          localConfigured ? (
            <Badge tone="ok">Endpoint configured</Badge>
          ) : (
            <Badge tone="warn">No local endpoint</Badge>
          )
        }
      >
        <p className="small muted" style={{ marginTop: 0 }}>
          NFR-306 allows three steps to run on a model you host yourself.
          <HelpTip term="local_routing" /> All three conditions must hold: the task is one
          of the three, it is ticked below, and an endpoint answers.
        </p>

        <div className="grid grid-2">
          <SettingField
            label="Local base URL"
            name="local_base_url"
            value={draft.local_base_url}
            fallback={defaults.local_base_url}
            overridden={isOverridden('local_base_url')}
            onChange={set}
            onReset={reset}
            hint="OpenAI-compatible, e.g. http://localhost:11434/v1"
          />
          <SettingField
            label="Local model"
            name="local_model"
            value={draft.local_model}
            fallback={defaults.local_model}
            overridden={isOverridden('local_model')}
            onChange={set}
            onReset={reset}
            hint="Whatever that endpoint serves."
          />
        </div>

        <Field label="Steps routed to it">
          <ChipSelect
            options={PRIVACY_TASKS}
            value={draft.local_tasks || []}
            onChange={(v) => set('local_tasks', v)}
          />
        </Field>

        {/* NFR-306 / CR-410: naming this on the screen rather than in help,
            because the default configuration sends profile text abroad. */}
        {remoteSensitive.length > 0 && (
          <Caution title="Your profile text is sent to the provider">
            {remoteSensitive.map((t) => t.label).join(', ')}
            {remoteSensitive.length === 1 ? ' is' : ' are'} handled by{' '}
            <strong>{draft.provider || 'the configured provider'}</strong>, not locally.
            {!localConfigured
              ? ' No local endpoint is configured, so ticking a step above changes nothing until one is.'
              : ' Tick the step above to route it to your own endpoint instead.'}{' '}
            Job seekers consent to that transfer separately (CR-410) before their composite
            profile is built.
          </Caution>
        )}
      </SectionCard>

      <SectionCard icon="sort" title="Per-task model" phase="phase-0">
        <p className="small muted" style={{ marginTop: 0 }}>
          A task with no entry uses the strong model.
          <HelpTip term="task_routing" /> Routing the bulk tasks to the cheap model is
          where the cost is saved; the cost column shows where it is actually going.
        </p>
        <TaskRoutingTable
          draft={draft}
          effective={effective}
          routing={routing.data || []}
          byTask={byTask}
          onChange={(map) => set('task_models', map)}
        />
      </SectionCard>
    </div>
  )
}

/* --- One override-aware setting ------------------------------------------- */

function SettingField({
  label,
  name,
  value,
  fallback,
  overridden,
  onChange,
  onReset,
  hint,
  type = 'text',
  step,
}) {
  return (
    <Field
      label={
        <>
          {label}
          {overridden && (
            <span style={{ marginLeft: 6 }}>
              <Badge tone="accent">overridden</Badge>
            </span>
          )}
        </>
      }
      hint={
        overridden ? (
          <>
            <code>.env</code> default: {String(fallback ?? '—') || '—'}{' '}
            <button className="btn btn-sm btn-ghost" onClick={() => onReset(name)}>
              Reset to default
            </button>
          </>
        ) : (
          hint
        )
      }
    >
      <input
        type={type}
        step={step}
        value={value ?? ''}
        onChange={(e) => onChange(name, e.target.value)}
      />
    </Field>
  )
}
