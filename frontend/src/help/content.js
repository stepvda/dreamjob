/**
 * Help content registry.
 *
 * All user-facing guidance lives here rather than being scattered through the
 * screens, for three reasons: it can be reviewed as a whole for tone and
 * accuracy, it can be translated as one unit (NFR-501 asks for English, Dutch
 * and French), and a screen cannot quietly ship without help because the page
 * shell looks its entry up by route.
 *
 * Structure:
 *   PAGE_HELP[route]  - what a screen is for, how to work through it, and the
 *                       caveats that matter on that screen specifically.
 *   GLOSSARY[term]    - a concept the product invents or uses in a particular
 *                       way. Anything a first-time user could reasonably
 *                       misread belongs here, referenced from fields with
 *                       <HelpTip term="..." />.
 */

export const PAGE_HELP = {
  '/home': {
    title: 'Start here',
    purpose:
      'Three steps: set up your profile, let Dream Job search and rank the market for you, then choose who to write to. Everything in between runs on its own.',
    steps: [
      'Create your profile — import your LinkedIn export or your CV, and write a few lines about the job you actually want. That is the only input the search needs.',
      'Press "Find my opportunities". Dream Job works out what to search for, picks the sources that fit, collects from them and ranks everything it found. It runs in the background, so you can close the page.',
      'Review and select. Open the ranked list, read why each role is there, and tick the ones worth pursuing.',
      'Apply. Dream Job writes a tailored CV, a briefing and a motivation document for each, and an email — all for you to read, edit and send.',
    ],
    tips: [
      'Nothing is ever sent on its own. Every message waits for you to approve it.',
      'The search is tunable: the directives, campaigns and sources behind it are under Advanced in the sidebar.',
      'A speculative opening is a role Dream Job thinks a company may need, not one it has advertised. They are labelled everywhere so an email never claims a vacancy exists.',
      'The full fifteen-stage picture is still available under Advanced → Full journey map.',
    ],
  },

  '/overview': {
    title: 'Where I am',
    purpose:
      'The whole process on one screen: what is done, what is running, what is blocked and what the next useful move is. Every stage links to the screen that advances it.',
    steps: [
      'Read the map left to right. A green mark is finished, a pulsing one is running, a dotted one is waiting on an earlier stage.',
      'Follow the suggested next action if you are not sure where to go.',
      'A blocked stage tells you what it needs — start there instead.',
    ],
    tips: [
      'The same map appears as a strip on each working screen, so you always know where you are.',
      'Counts on each stage are live: opportunities found, contacts validated, applications sent, responses recorded.',
      'The headline counts across the top are links — each opens the screen its number comes from.',
      'While a campaign is running, its token budget and cost on this screen refresh every few seconds. The refreshing stops when the campaign does.',
    ],
  },

  '/responses': {
    title: 'Responses received',
    purpose:
      'Every answer to an application, however it arrived. Replies to a connected Gmail account are detected automatically; everything else — a phone call, a LinkedIn message, an ATS portal, or any reply when you send through Resend — you record here.',
    steps: [
      'Pick the application the response belongs to from the list of sent applications awaiting one.',
      'Say what actually happened: interest, a request for information, an interview invitation, a rejection, a referral, or an automatic reply.',
      'Paste the text if you have it. It is read for detail — proposed interview times, who to contact instead — but your own reading of the outcome is what counts.',
    ],
    tips: [
      'Recording rejections matters as much as recording good news. Both feed the pattern analysis on the “What works” screen.',
      'If a classification is wrong you can correct it. A misread rejection quietly distorts every rate it is counted in.',
      'Recording a response moves the application on the pipeline board automatically.',
      'An application counts as answered or as silence, and nothing in between: until 21 days have passed, silence is not yet evidence. The summary at the top says how many have resolved and how many more the analysis needs.',
      'A reply the system detected on its own is listed here too, but it has no hand-entered record behind it — its outcome is corrected on the pipeline board rather than on this screen.',
    ],
  },

  '/insights': {
    title: 'What works, and where to redirect',
    purpose:
      'Which kinds of job and company actually answer you. Rates are computed from your recorded outcomes and shown with the sample size behind them, so you can judge them rather than trust them.',
    steps: [
      'Choose the outcome you care about — any reply, an interview, or an offer. Every figure below is computed for that one.',
      'Read the segment tables: the kind of work, the seniority, the company size and stage, the sector, the arrangement, the country, advertised versus speculative, and the language you wrote in.',
      'Look at the sample size and the width of the plausible range before you look at the percentage. Three applications is not a pattern, and the band drawn behind each rate shows how little a thin segment can carry.',
      'Run the analysis to turn those figures into redirection proposals. Each one names the counts on both sides of the move.',
      'Apply one to create a new version of your directives, or dismiss it with a reason. Everything you decided is kept at the bottom of the screen so you can trace it and revert it.',
    ],
    tips: [
      'Applying a proposal never overwrites your directives — it creates a new version you can revert to.',
      'A proposal that conflicts with your dream-job statement is flagged rather than hidden. The data does not get to overrule what you want; it only tells you what it is costing.',
      'Advice needs roughly six resolved applications before it will say anything at all.',
      'Segments marked “thin” are dimmed and excluded from advice: they are shown so you know they exist, not so you can act on them.',
      'The figures are cached to the last stored run, since they only move when a response is recorded. “Recompute” re-reads your responses.',
    ],
    caution:
      'These are observed rates, not causes. A weak segment may reflect which companies happened to be in it, how many roles were speculative, or when the applications went out — not the kind of work itself.',
  },
  '/profile': {
    title: 'Your profile',
    purpose:
      'Everything the system does starts here. Your profile is the only source of facts about you — nothing in a generated CV or email can say anything that is not grounded in this page.',
    steps: [
      'Upload your LinkedIn export. In LinkedIn, open your profile, click "Resources", then "Save to PDF". Drop that file here.',
      'Upload your CV as PDF or DOCX. Your photo is extracted automatically if the file contains one.',
      'Resolve the conflicts. Where the two documents disagree about a date, title or employer, you are asked which is right. Nothing is silently picked for you, and the answers are written into a new version when you apply them.',
      'Correct the sections. Summary, experience, education, certifications, publications, projects and languages are all editable; anything the parser read with low confidence is flagged for you to check.',
      'Tidy the skills. Matching runs on the normalised label, not the words in your CV, so a mis-normalised skill is invisible rather than merely untidy.',
      'Mark anything you never want disclosed. Flagged fields are excluded from every generated document and are stripped before anything is sent to the AI provider.',
    ],
    tips: [
      'Each save creates a new version, and you can view or restore an older one. Campaigns record which version they used, so an old ranking stays explainable.',
      'Your uploads are kept, so the profile can be re-extracted later without asking you for the files again.',
      'Evidence items (repositories, publications, talks, certificates) attach to skills and get cited with links in tailored CVs.',
      'Personas let you run one search as, say, an architect and another as a product leader, each with its own emphasis and dream-job statement.',
      'Your dream-job statement has its own screen. This one is about the facts; that one is about what you want.',
    ],
  },

  '/composite': {
    title: 'Composite profile',
    purpose:
      'A synthesis of what you supplied and what could be verified about you online. This is what the AI actually reads when matching, scoring and writing on your behalf.',
    steps: [
      'Review each statement on the "Composite profile" tab. Every one carries a source — your input, the LinkedIn export, your CV, or a specific URL. Hover the source marker to see it, and to see the confidence behind a web citation.',
      'Work the "Online findings" queue. Open "Why this was attributed to you" on any finding to see the individual identity signals — name variants, employer overlap, cross-links, location, timeline, photo similarity — each with its own score and the text that matched.',
      'Confirm or reject each one. Only findings that matched your identity with high confidence were merged automatically.',
      'Edit anything that reads wrong. Your edits win over the synthesis, and anything you write is attributed to you rather than to a page.',
    ],
    tips: [
      'A rejected finding is remembered and never proposed again — the button says so because the decision is permanent.',
      'Online enrichment can be switched off entirely on the "Enrichment" tab; the composite profile is then built from your documents alone.',
      'Statements marked "needs confirming" could not be traced to any source. They are kept and flagged rather than quietly presented as fact.',
      'Synthesising the profile sends it to a provider outside the EU, so the consent notice at the top has to be accepted before the profile can be built.',
    ],
    caution:
      'Findings about someone who shares your name are the main risk here. Anything not marked "confirmed" is held back for you to approve precisely so a stranger\'s career cannot end up in your CV.',
  },

  '/dream-job': {
    title: 'Dream job',
    purpose:
      'Describe the job you actually want, in your own words and your own language — the work itself, the kind of organisation, the people, the impact, the conditions, and what you want to avoid. There is no length limit.',
    steps: [
      'Write freely. Prose works better than bullet points; the model reads it for nuance, not keywords.',
      'Read the structured model it produces — target roles, responsibilities, company characteristics, values, deal-breakers.',
      'Correct it and confirm. It is used for matching, so a wrong deal-breaker costs you real opportunities.',
    ],
    tips: [
      'Say what you want to avoid. Deal-breakers filter more decisively than preferences.',
      'Mention conditions that matter: commute, remote days, team size, autonomy, travel. They become directive defaults.',
      'This is separate from your search directives. Directives bound where the system looks; this describes what would make you happy if it found it.',
      'Rewriting the statement does not update the model. The screen tells you when the two have drifted apart — rebuild before you launch a campaign, because planning and scoring read the model, not your text.',
      'Nothing acts on the model until you confirm it, and you can correct any line of it first.',
    ],
  },

  '/directives': {
    title: 'Search directives',
    purpose:
      'Directives bound the search. They are structured controls rather than free text so they translate reliably into each source\'s own query language.',
    steps: [
      'Start from a proposal built out of your composite profile, or from an empty set.',
      'Work through the five groups: job content, company type, location, work arrangement and compensation. Each one collapses, so you can take them one at a time.',
      'Values pre-filled from your composite profile are suggestions — change anything that does not fit.',
      'Turn on discretion mode if you are searching while employed, and name your current employer before anything else.',
      'Check the collection estimate beside the form before launching. It tells you how many sources and pages the current settings imply.',
      'Save the set under a name. Saving again after a campaign has used it creates a new version rather than overwriting it.',
    ],
    tips: [
      'Directive sets are saved, named, versioned and reusable across campaigns.',
      '"Notes to the AI" is the only free-text field; use it for nuance the controls cannot express. Hard constraints belong in the controls, where a source adapter can actually act on them.',
      'Compensation is used for filtering and scoring only. It is never written into a CV or an email unless you explicitly opt in, and that opt-in is off by default.',
      'A commute tolerance is a tighter statement than a plain radius: set one and your search areas are narrowed to match it.',
      'Ticking "speculative openings only" changes which sources run, so the estimate moves as soon as you set it.',
    ],
    caution:
      'The collection estimate is an order of magnitude derived from your settings, not a promise. Discretion mode excludes companies and contacts by name and domain matching — strong, but it cannot catch a group company trading under an unrelated name unless you name it yourself.',
  },

  '/campaigns': {
    title: 'Campaigns',
    purpose:
      'A campaign is one run of the collection and analysis pipeline under one set of directives. Each stage stores what it produced, so any stage can be re-run on its own.',
    steps: [
      'Create a campaign from a directive set.',
      'Review the generated plan: which sources, which queries, expected volume, duration and cost. Exclude any source you do not want.',
      'Launch, then watch the dashboard. You can pause, resume or cancel at any point.',
      'Re-run a single stage when something needs redoing — the plan, the reuse assessment, collection, opportunity synthesis or scoring — instead of repeating the whole campaign.',
    ],
    tips: [
      'The planner checks the shared knowledge base first and only collects what is missing or stale. The saving is reported before you launch.',
      'A crash loses at most the page in flight; jobs resume from their last checkpoint.',
      'The token budget caps AI spend. Near the limit the system degrades gracefully, dropping speculative openings for low-ranked companies first.',
      'The activity list shows the last thing each source did while a run is going. The refreshing stops when the campaign does.',
    ],
  },

  '/browser': {
    title: 'Browser session',
    purpose:
      'Some sites — LinkedIn, Glassdoor — block automated access. Instead of circumventing that, the system drives a browser window that you opened and logged into yourself.',
    steps: [
      'Read the terms warning at the top and decide. Nothing starts until you have acknowledged it, and you can withdraw that at any time.',
      'Run scripts/browser.sh, or copy the command for your platform from the launch instructions. It opens a browser with a separate profile, leaving your normal one untouched.',
      'Log in yourself, in that window, then press "Check connection" and "Check sign-in" here.',
      'Choose the campaign, the site and the driver, and ask for the duration estimate. You see the exact list of pages that will be opened.',
      'Confirm the estimate to start, then watch it: pause, skip a single page, or cancel at any moment.',
    ],
    tips: [
      'You can watch it work, pause at any moment, and skip individual targets.',
      'It stops immediately if it hits a captcha, challenge or rate-limit page, and says so rather than retrying.',
      'Automation is slow by design — a few seconds per page. Campaigns assume tens to low hundreds of targets, not thousands.',
      'Chromium-family browsers are attached to over the debugging port; Firefox has no such port, so Dream Job re-opens the dedicated profile itself.',
      'The estimate is refined from measurements while the run is going, so the time remaining gets more accurate as it works.',
    ],
    caution:
      'LinkedIn\'s user agreement prohibits automated access, including through a session you logged into yourself. Your account could be restricted. The system shows this warning and records your acknowledgement before the first run — the decision is yours.',
  },

  '/opportunities': {
    title: 'Opportunities',
    purpose:
      'Every advertised vacancy and every speculative opening the system found, ranked. The score is advisory; you decide what to pursue.',
    steps: [
      'Filter and sort to taste — kind, tag, timing, work arrangement, seniority, country, a score floor, or a keyword that also matches the company name. Speculative openings are marked distinctly everywhere.',
      'Open “Why this rank?” on any row for the seven sub-scores and the dream-job criteria behind them.',
      'Pin what interests you, drag the ⠿ handle to place rows by hand, and mark the rest as not interested with a reason.',
      'Tick two or three and compare them side by side before you commit.',
      'Then generate applications for everything you ticked, and read them on the Applications screen before anything is sent.',
    ],
    tips: [
      'Your manual order overrides the computed ranking and survives recalculation. Positions are numbered within one campaign, so hand-ordering asks you to pick a campaign and stay on the first page.',
      'Rejection reasons feed back into your weights — the ranking learns from them, which is why the reason is required.',
      'The dream-job fit meter is separate from the overall score. It shows which of your criteria the role meets, partly meets and violates.',
      'The tick box is your shortlist and it lives on the server, so it stays correct across filters and pages.',
    ],
    caution:
      'The score is advisory and nothing acts on it. It orders a list; every decision to keep, reject or apply is yours, and a low-ranked role you know something about beats a high-ranked one you do not.',
  },

  '/companies': {
    title: 'Companies',
    purpose:
      'The shared knowledge base of company profiles, five-year financial analyses, competitors and hiring signals — searchable on its own, without picking a campaign.',
    steps: [
      'Search or browse. The full-text index covers the business summary, products and markets, not only the name; country, size band and sector narrow the whole knowledge base, while stage and trajectory narrow the page you are looking at.',
      'Read the freshness mark before you read the figures. A stale row is still shown, because old research beats none, but it describes the company as it was.',
      'Open a company for the standardised profile: identity, what it does, size, locations, the departmental map, key people, reference customers, technology and news, each field carrying the page it was read from.',
      'Work through the financial tab. Five filed years, the revenue and headcount trends, and the two 0–100 scores with the reasoning that cites the figures.',
      'Check the market tab for competitors, dated hiring signals and the application window, and the values tab for anything the company says that contradicts what you want.',
      'Watch the companies worth following, and refresh a profile when it has gone stale.',
    ],
    tips: [
      'Company research is shared across job seekers and reused while it stays fresh, so a second search costs nothing rather than re-crawling.',
      'Nothing here links back to any job seeker — the knowledge base holds market data only. Your watchlist and the values comparison are private and sit on top of it.',
      'Estimated years are marked as estimated, and figures that did not reconcile against the filing carry a flag. Both weaken every score computed from them.',
      'Adding a competitor to your list also creates the shared record for it when it has not been profiled yet, so the next campaign can pick it up.',
      'A refresh is a paced crawl of the company\'s own site, roughly a minute for thirty pages. It re-derives signals and competitors from the same pages rather than walking the site twice.',
    ],
    caution:
      'Ability to pay and investment capacity are advisory. They are computed from filed accounts, which are late by construction and sometimes estimated, and they describe what a company could afford — never what it will offer.',
  },

  '/contacts': {
    title: 'Hiring contacts',
    purpose:
      'Who to write to at a target company, and whether there is a warmer way in than a cold email. Every address is validated before it is ever used.',
    steps: [
      'Run "Find contacts for my shortlist" to scrape your target companies: each company’s own site and press pages are read for a named hiring manager or a published careers mailbox, and every address is validated before it is stored. It is a background job because it is rate-limited per domain.',
      'Pick a company to see its ranked contacts: the hiring manager of the relevant department where one can be identified, then talent acquisition, then a generic careers mailbox. "Find contacts for this company" re-runs the ladder for just that one.',
      'Check the validation result. Addresses that came back invalid are never sent to.',
      'Look at the introduction routes before settling for a cold email — a warm introduction does better.',
      'Import your own network once, so routes can be found for every company from then on.',
    ],
    tips: [
      'A company showing "unreachable" is a real finding: the ladder was walked and nothing survived, so the screen tells you that rather than offering an address somebody made up.',
      'An inferred address — guessed from the pattern of other addresses on the same domain — is marked as such and deserves less confidence than one published on the company site.',
      'A "risky" result usually means a catch-all domain, where the server accepts anything and proves nothing.',
      'Contacts collected through browser automation expire with the campaign that collected them and are never shared.',
    ],
    caution:
      'These are real people who did not ask to hear from you. Only professional contact details are stored, every message offers a way to object, and an objection blocks the address permanently for everyone on this installation.',
  },

  '/applications': {
    title: 'Applications',
    purpose:
      'For each selected opportunity the system produces a tailored CV, a company and job briefing, a motivation and fit document, and an introduction email.',
    steps: [
      'Generate packages for the opportunities you selected in the ranked list, then pick one on the left.',
      'Read the Email tab: subject and body are editable, and you can regenerate with an instruction like "shorter" or "lead with the platform work".',
      'Open the CV tab to choose a template or a language, see whether your photograph is included, and download the DOCX or the PDF.',
      'Read the Briefing and Motivation tabs. Both are yours to keep and neither is ever attached to anything.',
      'Work through the Checks tab. It lists every claim, whether your profile supports it, and what it was matched against. A failed check blocks approval.',
      'Approve — one or a whole selection. Either way you first see a table of exactly what goes to whom.',
    ],
    tips: [
      'The briefing and the motivation document are for you alone. They are never attached to the email.',
      'For a speculative opening the email is written as a spontaneous application — it never claims a vacancy exists, and a phrase that implies one blocks dispatch.',
      'Documents are generated in the language of the opportunity; changing the language rewrites all four.',
      'Editing the email, or regenerating the CV, withdraws an approval you already gave and re-runs the checks over the new text.',
      'Approving authorises dispatch but sends nothing. The messages go out from your own mailbox on the Mail screen, inside the daily cap.',
    ],
    caution:
      'Read the generated CV before approving it. The consistency check catches invented employers, dates and titles, but you are the last line of defence for anything that is technically true and still wrong for the audience.',
  },

  '/pipeline': {
    title: 'Application pipeline',
    purpose:
      'What happened after you applied: sent, replied, interview, offer, closed. Replies move a card on their own where they say something unambiguous; everything else you move by dragging it. What is due today sits above the board.',
    steps: [
      'Clear \u201cDue now\u201d first \u2014 it is the follow-ups and answers whose moment has arrived or passed.',
      'Work the board. Drag a card when you want to override the detected stage; a manual move may go backwards.',
      'Open a card to see the classified reply, the drafted answer, the stage dates and your notes. Edit the draft, then approve it \u2014 approval queues it for the mail screen and sends nothing.',
      'Run a mock interview before the real one. It uses the vacancy and the briefing, gives feedback per answer, and closes with the weak spots to rehearse.',
      'From the interview stage, build the salary negotiation brief: a figure, the arguments behind it, and what to ask for if base salary will not move.',
      'When an application ends, drop it in \u201cClosed\u201d and say how it ended. That outcome is the only thing \u201cWhat works\u201d has to learn from.',
    ],
    tips: [
      'Nothing is ever sent automatically. Drafts wait for you, and approving one only marks it ready.',
      'An application that has gone quiet is reported, never closed for you \u2014 \u201cslow\u201d and \u201cnot interested\u201d look identical from outside.',
      'A card\u2019s history says whether a reply moved it or you did, so an automatic move never has to be re-checked by hand.',
      'Record a response on the Responses screen when it did not arrive by e-mail \u2014 a phone call, a portal, a LinkedIn message. It moves the card here.',
      'Outcomes feed back into generation defaults and scoring weights on the \u201cWhat works\u201d screen, and the learned effects are reported with the sample size they rest on.',
    ],
    caution:
      'The negotiation brief is advisory. Its figures are estimates from filed accounts and comparable roles, and the arguments are written from those estimates \u2014 what you ask for, and what you accept, stays your decision.',
  },

  '/monitoring': {
    title: 'Monitoring',
    purpose:
      'The part of the search that keeps running after you stop. Watched companies are rechecked on a schedule for new vacancies, hiring signals, news and filings; anything new becomes a notification, and a weekly digest turns the period into one page with one recommended next action.',
    steps: [
      'Add companies to the watchlist and set how often each is checked. A new watch is due at once, so its first pass runs on the next cycle rather than in a week.',
      'Point a watch at a campaign if you want matching vacancies added to that campaign\u2019s ranked list. Leave it empty to be notified only.',
      'Work the notifications. Each new vacancy, signal or filing is announced once, so an empty list means nothing changed \u2014 not that nothing was looked at.',
      'Check the timing tab before you approach anyone: it lists the watched companies whose moment is favourable and the events that made it so.',
      'Read the weekly digest and do the one thing it recommends. Generate one early if you would rather not wait for the schedule.',
    ],
    tips: [
      'Timing intelligence flags when a company has just done something that usually precedes hiring \u2014 a funding round, a reorganisation, a new office, a run of related vacancies.',
      'The five channels are independent. A company with no newsroom and no filed accounts still gets its careers page read, and the watchlist names the channel that failed.',
      '\u201cRun the cycle now\u201d rechecks your own watches, and only those that are due. The scheduler tab shows the loop that would otherwise do it, and whether it is running.',
      'Removing a watch deletes nothing that was collected: company profiles, vacancies and signals belong to the shared knowledge base.',
    ],
    caution:
      'Everything here is advisory. A favourable moment says a company is more likely to be hiring, never that the role suits you, and it never changes an opportunity\u2019s score. The digest recommends one action and sends nothing on your behalf (NFR-305).',
  },

  '/intelligence': {
    title: 'Dream-job intelligence',
    purpose:
      'How far the market and your profile are from the job you described, and what would close the distance. Five readings of the same question, on five tabs.',
    steps: [
      'Read the gap analysis. Each gap names a concrete closing action, an estimated effort, and the opportunities where that gap measurably cost you points — follow those links to see the price of the gap.',
      'Look at the fit across the market: how much of what this campaign collected clears your dream-job threshold at all. That distribution is the condition the next tab turns on.',
      'If nothing clears the threshold, review the stepping-stone paths — two or three routes that are reachable now and plausibly lead there. You can raise or lower the threshold, and tag the routes onto your ranked list when you want them there.',
      'Check the values conflicts: companies whose own material contradicts something you said you wanted, with the passage it came from.',
      'Finally, the LinkedIn suggestions. Edit the draft, copy what is useful, and apply it yourself.',
    ],
    tips: [
      'The gap analysis and the paths both work without the language model. A model only sharpens the wording; the gaps, the numbers and the routes are computed.',
      '“Decisive” is arithmetic, not opinion: an opportunity is listed under a gap only when it measurably lost profile-fit points on that dimension.',
      'The dream-job fit threshold is yours and is stored per job seeker, so changing it changes nothing for anyone else — but it does change which opportunities count as destinations.',
      'Tagging the ranked list only ever adds “destination” and “stepping_stone”. Your own tags, pins, statuses and manual order are never touched.',
      'A values mismatch is reported only where a company contradicts you. Silence comes back as unknown — a question for the interview, not a warning.',
      'The values check runs one request per company and covers the companies behind your current ranked list, which is why it is on a button rather than automatic.',
    ],
    caution:
      'Dream Job never signs in to LinkedIn and never edits a profile. The suggestions are text you copy and apply by hand — and while discretion mode is on, editing a headline, an About section or a skills list notifies your network.',
  },

  '/networking': {
    title: 'Networking and export',
    purpose:
      'Warm routes into a company, the events where the right people actually are, and a way to take a whole campaign with you.',
    steps: [
      'Pick a target company. The routes into it are ranked across every open role it has, because one company has one network around it.',
      'Choose a role and draft. Each route gets a message asking that person for an introduction — first-degree connections, former colleagues, alumni of the same employer or school, fellow members of a community.',
      'Read the message, edit it, copy it, and send it yourself. Then mark the route “asked” so the list stops looking untouched.',
      'Work the event radar for what is coming up inside your travel tolerance, and add anything worth attending to your calendar.',
      'Export a campaign as one package — a readable PDF bundle and the complete JSON — for your own records or for a career coach.',
    ],
    tips: [
      'The message here goes to somebody in your own network. The introduction email to the hiring contact is a different thing and lives on the Applications screen.',
      'Routes are ranked on two separate figures: strength (would they help at all) and relevance (how close they sit to the decision). Both are shown, because they call for different messages.',
      'An edited draft is not stored — the API has no endpoint for it. Copy it before you leave the screen.',
      'Events beyond the travel tolerance in your directives are hidden unless you ask to see them. Online events have no distance and are always shown.',
      'Adding an event to your calendar writes to a connected calendar if you have one, and otherwise produces a file you can import. Nothing registers you or contacts an organiser.',
      'Exports respect your do-not-disclose flags and contain no other job seeker\'s data — the package is checked for that before it is sealed, and the manifest says which fields were withheld.',
    ],
    caution:
      'Nobody on this screen is contacted for you. Ranking a route, drafting its message and marking an event interesting are all preparation; every approach is yours to make. Making an export is recorded in your audit trail, because the file can be sent to anyone once it exists.',
  },

  '/mail': {
    title: 'Mail setup',
    purpose:
      'Where applications leave from, under what limits, and what became of each one. Two backends are supported and they are not equivalent: only a connected Gmail mailbox can read the answers back.',
    steps: [
      'Connect Gmail with OAuth. It sends from your own address and, because replies and bounces arrive in your own inbox, it is the only backend that detects them for you.',
      'Resend is the alternative \u2014 a one-way relay from stepvda.com \u2014 but its API key has not been issued yet, so it cannot send today. The Mailboxes tab says exactly what to do when the key exists.',
      'Read the sending rules before your first dispatch. The pace, the daily cap and the send window are set on the server and reported here rather than adjusted from this screen.',
      'Use the dispatch log to see what went out: recipient, time, attachments, message id, delivery status, and any bounce or reply.',
      'Set how many days of silence make a follow-up due, then read each draft before it is sent.',
    ],
    tips: [
      'Send windows are applied in the recipient\u2019s time zone, derived from the company\u2019s country and locations \u2014 not in yours.',
      'Only addresses that validated as usable are ever sent to, and an address that has objected is blocked permanently.',
      'Access tokens are stored encrypted, scoped to sending and to reading your mail for replies, and revocable here \u2014 revoking asks Google as well as erasing the local copy.',
      'A queued message is not an error. It is waiting for the pace, the cap or the window, and it leaves on its own.',
      'Mail sent through Resend has no inbox to poll, so replies to it are recorded by hand on the Responses screen.',
    ],
    caution:
      'Daily caps and pacing exist to protect your sender reputation. Raising them sharply is the fastest way to have your mail classified as spam \u2014 including the applications that already went out.',
  },

  '/admin': {
    title: 'Administration',
    purpose:
      'How this installation is configured and what it has actually done: the accounts on it, which model handles which task and what that costs, which sources may be used and on what terms, the run history, the audit trail, and the AI call log with its retention control.',
    steps: [
      'Users: create accounts, grant or revoke administrator access, reset a password, sign someone out everywhere, suspend or restore an account, and delete one. Search by e-mail or name.',
      'Models: choose the provider and the model each task uses, set the default token budget and the prices the cost figures are computed from, and route the privacy-sensitive steps to a local endpoint if you have one.',
      'Sources: enable the adapters you want and cap their pace. A source whose terms prohibit automated access cannot be switched on until an administrator acknowledges that in writing, and switching it on afterwards is a second, separate decision.',
      'Activity: job runs, records per source, errors, and tokens and cost over time — for the whole installation or for one campaign.',
      'Audit: the append-only trail. Filter it to applications to see who approved and sent each one, and which profile version went with it.',
      'Logs: the server\u2019s own files. Read the level summary first \u2014 it says how many warnings and errors the installation has produced over the window \u2014 then open the file behind a count and read the end of it.',
      'Data: browse the AI call log, run the redaction sweep, export everything held about you, or erase your account.',
    ],
    tips: [
      'Users: the last remaining administrator cannot be demoted, suspended or deleted, and an administrator cannot suspend or delete their own account from here — both would end the session they are working in. Grant the role to a second person before removing it from yourself.',
      'A suspended account is signed out everywhere immediately and cannot sign in again until restored. Its data is untouched, so restoring it puts everything back.',
      'Deleting an account erases its private data for good; the shared company knowledge base stays because it belongs to no one.',
      'Every setting shows its effective value: the .env default with your override on top. An overridden field says so and can be reset to the default in one click.',
      'Only three steps are privacy-sensitive enough to route to a local model — the composite profile, the tailored CV and the motivation document. Naming any other task has no effect.',
      'Sources whose terms prohibit automated access are disabled until an administrator explicitly acknowledges the risk, and withdrawing that acknowledgement disables the source again.',
      'Prompts and responses are nulled from the call log after the retention period. Tokens, cost and model survive, because the cost reports need them and they are not personal data.',
      'Opening one call\u2019s prompt is itself written to the audit trail.',
      'Every log line carries the correlation id of the request that caused it, so one click in the browser can be followed across the request, application and database files by searching for eight characters.',
      'The errors file is a copy: every warning and worse is written both to the file it came from and to errors.log. That is why it is left out of the totals \u2014 counting it would double every warning.',
      'The log summary reads the end of each file rather than all of it. A file that says \u201cpartial scan\u201d had more inside the window than the scan could afford; narrow the window to be sure of the counts.',
      'Erasure removes your account and everything private to it. The shared company knowledge base stays, because it belongs to no one.',
    ],
    caution:
      'Two actions on this screen cannot be undone. The redaction sweep destroys prompt and response text older than the cutoff rather than archiving it, and erasing a job seeker deletes the account and every private row behind it.',
  },
  '/apply': {
    title: 'Apply browser',
    purpose:
      'The whole application, one job at a time: the letter that goes out, the CV that is attached to it, the two documents that are yours alone, and the checks that stand between them and a recipient. Nothing is sent from here while the machine is in dry run \u2014 the banner at the top says which it is.',
    steps: [
      'Pick a job on the left. The list shows every job you selected with its company, its contact and how far its package has got.',
      'If nothing has been generated, generate the four documents \u2014 the tailored CV, the briefing, the motivation document and the email.',
      'Read the Email tab. It is the letter that will be sent and it refers to the CV; edit it in place, or ask for a rewrite with an instruction like \u201cshorter\u201d or \u201clead with the platform work\u201d.',
      'Open the CV tab and read the actual PDF. This is the file that gets attached, and it is the only one that does.',
      'Read the Briefing and the Motivation tabs if you want them. Both are for you and neither can be attached to anything.',
      'Work through Checks: every claim, whether your profile supports it, and what it was matched against. A failure here blocks dispatch.',
      'Approve, then press Send. In dry run the whole path runs and the finished message is written to disk instead of being sent, and you are told exactly what it contained.',
    ],
    tips: [
      'The two send buttons are never greyed out. Press one: it runs the whole path and reports where it stopped, which is more useful than a button that will not respond.',
      'Only the tailored CV is ever attached. The briefing and the motivation document are refused by the code that builds an outgoing message, not merely left out of it.',
      'The first three rows of filter chips narrow the whole selection on the server, so the count is true. The row labelled \u201cthis page\u201d narrows only the rows on screen.',
      'Approving and sending are two decisions. Approval authorises dispatch; the send window, the pacing rule and the daily cap decide when a message may actually go.',
      'Changing the CV template re-renders the same facts in another layout and costs nothing. Changing the language rewrites all four documents and withdraws the approval.',
      'Editing the email re-opens an approved package as a draft, because the consistency check has to read the words you actually wrote.',
      '\u201cSend all with attachment\u201d always shows a table of exactly who receives what before it will run, however many are in it.',
    ],
    caution:
      'Read the CV before you approve it. The consistency check catches invented employers, dates and titles, but you are the last line of defence against something that is technically true and still wrong for this audience. And these are real people who did not ask to hear from you: every message carries a way to object, and an objection blocks the address permanently for everyone on this installation.',
  },
}

/**
 * Concepts worth explaining where they appear. Reference from any control with
 * <HelpTip term="speculative_opening" />.
 */
export const GLOSSARY = {
  speculative_opening: {
    term: 'Speculative opening',
    body:
      'A role the system thinks a company is likely to need or able to create in the next 6–12 months, based on its finances, hiring signals, department structure and what its competitors are hiring for. No such vacancy has been advertised. Emails for these are written as spontaneous applications and never claim a vacancy exists.',
  },
  plausibility: {
    term: 'Plausibility',
    body:
      'How likely a speculative opening is to be real, given the company\'s capacity and recent behaviour. It has no meaning for an advertised vacancy.',
  },
  dream_job_fit: {
    term: 'Dream-job fit',
    body:
      'How well a role matches what you described on the Dream job screen — as opposed to how well it matches your skills. Shown separately from the overall score because a role can suit your CV perfectly and still not be what you want.',
  },
  ability_to_pay: {
    term: 'Ability to pay',
    body:
      'A 0–100 score derived from five years of filed accounts: margins, equity, cash, and personnel cost per employee. It estimates whether a company can afford to pay well, not whether it will.',
  },
  investment_capacity: {
    term: 'Investment capacity',
    body:
      'A 0–100 score for whether a company has the financial room to start something new that needs hiring — cash, debt load and recent capital expenditure.',
  },
  reachability: {
    term: 'Reachability',
    body:
      'Whether there is a validated contact address or a warm introduction route. A perfect role you cannot reach scores lower than a good one you can.',
  },
  composite_profile: {
    term: 'Composite profile',
    body:
      'The merged, evidence-traced version of you: your documents plus anything verified online. It is what the AI reads. Every statement in it links back to where it came from.',
  },
  identity_match: {
    term: 'Identity match',
    body:
      'How confident the system is that an online finding is about you and not a namesake. It combines name variants, shared employers, location, timeline consistency, cross-links and photo similarity. Only "confirmed" merges without asking you.',
  },
  discretion_mode: {
    term: 'Discretion mode',
    body:
      'For searching while employed. Your current employer, its group companies and any company you flag are excluded from everything, as are contacts likely to expose the search — current colleagues and your employer\'s recruiters. No action that signals job hunting is taken on LinkedIn.',
  },
  directive: {
    term: 'Directive',
    body:
      'A structured constraint that bounds the search — "remote only", "scale-ups of 50–500 people in Flanders". Directives decide where the system looks; your dream-job statement decides what it looks for.',
  },
  staleness: {
    term: 'Freshness',
    body:
      'How old collected data may be before it is re-fetched. Vacancies go stale in days, company websites in months, filed accounts in a year. Anything still fresh is reused instead of re-collected.',
  },
  provenance: {
    term: 'Provenance',
    body:
      'The specific source a fact came from, kept per field. Hover a source marker to see the page and the confidence. Low-confidence fields are flagged rather than quietly presented as certain.',
  },
  catch_all: {
    term: 'Catch-all domain',
    body:
      'A mail server that accepts any address at a domain, so a successful check proves nothing about whether the mailbox exists. Such addresses are marked "risky" rather than "valid".',
  },
  role_address: {
    term: 'Role address',
    body:
      'A shared mailbox such as info@ or jobs@ rather than a person. Usable, but a named hiring manager gets a better response rate, so these rank lower.',
  },
  token_budget: {
    term: 'Token budget',
    body:
      'The cap on AI usage for a campaign. As it runs low the system sheds optional work first — speculative openings for low-ranked companies — rather than stopping mid-campaign.',
  },
  cdp: {
    term: 'Browser automation',
    body:
      'The system attaches to a browser window you opened and logged into yourself, using Chrome\'s remote debugging interface. It never asks for, stores or reuses your password, cookies or session tokens.',
  },
  do_not_disclose: {
    term: 'Do not disclose',
    body:
      'Fields you have marked are excluded from every generated CV, briefing and email, and are stripped before anything is sent to the AI provider. Use it for a date of birth, a photo, a current salary.',
  },
  segment: {
    term: 'Segment',
    body:
      'A group of your applications sharing one attribute — a kind of work, a seniority, a company size band. Rates are compared between segments to find where you are being answered and where you are not.',
  },
  sample_size: {
    term: 'Sample size (n)',
    body:
      'How many resolved applications a percentage rests on. A 50% reply rate from 2 applications and one from 40 are the same number and completely different evidence. The plausible range shown next to each rate is how wide the uncertainty is.',
  },
  resolved: {
    term: 'Resolved',
    body:
      'An application that has either received a reply or gone long enough without one to count as silence. Applications sent recently are excluded from the rates — counting them as "no reply" would make every recent choice look bad.',
  },
  redirection_advice: {
    term: 'Redirection advice',
    body:
      'A proposed change to your search directives, derived from your own outcome rates. The figures are computed first and the AI is asked to turn them into advice — it is never asked to find the pattern itself. Nothing is applied without you accepting it.',
  },
  stepping_stone: {
    term: 'Stepping stone',
    body:
      'A role that is not the job you want but plausibly leads to it, offered when nothing currently on the market clears your dream-job threshold.',
  },
  next_action: {
    term: 'Suggested next action',
    body:
      'The earliest stage in the process that is ready to move or already running. It is a suggestion, not an instruction — you can work any unblocked stage in any order, and nothing happens because the suggestion says so.',
  },
  journey_state: {
    term: 'Stage marks',
    body:
      'Each stage on the map carries its own state: a green tick is finished, a pulsing dot is running now, a dashed outline is ready to start, and a dotted, dimmed stage is waiting on an earlier one — it names what it needs instead of just being unavailable.',
  },
  // --- Browser session (FR-201..208, CR-401) -------------------------------
  target_allowlist: {
    term: 'Target list',
    body:
      'The closed list of pages a browser run is allowed to open, produced by the campaign plan. The automation visits those and nothing else: it never follows a link it found on a page it read, so a run cannot quietly turn into a crawl.',
  },
  duration_estimate: {
    term: 'Duration estimate',
    body:
      'How long the run is expected to take, announced before anything opens and confirmed by you. It starts from the pacing model and is replaced by measurements of your own machine as the run proceeds, so the figure you watch gets more honest, not less.',
  },
  human_pace: {
    term: 'Human pace',
    body:
      'Browser automation is deliberately slow — seconds per page, one session at a time, with pauses and scrolling between actions. It is what keeps a run indistinguishable from ordinary use, and it is why campaigns assume tens to low hundreds of pages rather than thousands.',
  },
  challenge_page: {
    term: 'Challenge page',
    body:
      'A captcha, identity check or rate-limit page served instead of the content. The run stops at the first one and does not retry or work around it. Complete it by hand in the automation window; repeated challenges mean the target list is too long.',
  },
  online_finding: {
    term: 'Online finding',
    body:
      'One public page that the enrichment search believes is about you, kept with the facts read from it and the identity signals that were scored. Findings are classified confirmed, probable or doubtful; only confirmed ones are merged without asking, and rejecting one records the URL so no future run proposes it again.',
  },
  unsupported_statement: {
    term: 'Unsupported statement',
    body:
      'A line in the composite profile the system could not trace back to a document you supplied or a page it actually fetched. It is kept and flagged rather than deleted, because it may still be right — but nothing generated from your profile should lean on it until you confirm it.',
  },
  role_family: {
    term: 'Role family',
    body:
      'A grouping of job titles that mean roughly the same work — "platform engineering" covers SRE, infrastructure engineer and DevOps lead. Searching by family as well as by exact title is what stops a good role being missed because the company named it something unusual.',
  },
  deal_breaker: {
    term: 'Deal-breaker',
    body:
      'Something that rules a role out however good the rest of it is. A hard deal-breaker vetoes an opportunity outright before it is ever scored, so it filters far more decisively than a preference — and a wrong one silently costs you roles you would have wanted to see.',
  },
  implicit_preference: {
    term: 'Implicit preference',
    body:
      'Something the extraction inferred from how you wrote rather than from what you said outright. Each one is shown with the wording it was read from and how confident the reading was, so you can disown it — an inference you never made is worse than a gap.',
  },
  model_drift: {
    term: 'Model out of date',
    body:
      'The structured dream-job model records the exact statement it was built from. When your statement has since changed, the model is stale: planning, discovery and scoring read the model rather than your text, so until you rebuild it the search is still chasing what you used to want.',
  },
  native_query: {
    term: 'Native query',
    body:
      'The query as the source itself understands it — keywords and filters for a job board, a board slug for an applicant tracking system, a sector code for a company registry, crawl seeds for a website. Your directives are translated into one of these per source, and the result is shown before anything is fetched so you can see exactly what will be asked.',
  },
  knowledge_base_reuse: {
    term: 'Knowledge-base reuse',
    body:
      'Before collecting, the plan is compared with everything already in the shared knowledge base. Records still inside their freshness window are reused rather than fetched again, and the time and money that saves is reported per record type. A source whose contribution is already covered is skipped entirely; a partly covered one keeps a smaller page budget.',
  },
  extraction_rate: {
    term: 'Extraction success rate',
    body:
      'The rolling share of pages an adapter managed to read fields out of. A sharp drop usually means the site changed shape rather than that it has nothing to offer, so it is reported instead of quietly returning fewer results.',
  },
  stage_rerun: {
    term: 'Stage re-run',
    body:
      'Each pipeline stage stores what it produced, so one stage can be run again from the previous stage\'s output — re-score without re-collecting, re-synthesise opportunities without re-crawling. A re-run replaces that stage\'s own artefacts and can spend tokens from the campaign budget.',
  },
  // --- Profile intake (FR-101..109, FR-441, FR-442) ------------------------
  merge_conflict: {
    term: 'Merge conflict',
    body:
      'A field where your LinkedIn export and your CV disagree \u2014 a start date, an employer name, a role title. The merge records both values and asks you rather than choosing, because a silently picked date can end up in a CV sent to an employer. Until you settle it the field is carried as contested.',
  },
  profile_version: {
    term: 'Profile version',
    body:
      'Every save appends a numbered version instead of overwriting the last one. Campaigns record the version they ran against, so a ranking made months ago can still be explained, and you can view or restore any earlier version without losing the current one.',
  },
  normalised_skill: {
    term: 'Normalised skill',
    body:
      'The taxonomy label a skill is matched to, kept alongside the raw words your documents used. Matching and scoring run on the normalised label, so "React.js" and "ReactJS" count as the same skill \u2014 and a skill that failed to normalise is invisible to matching rather than merely untidy.',
  },
  evidence_item: {
    term: 'Evidence item',
    body:
      'A repository, publication, talk, case study, article, reference or certificate, linked to the skills it backs. A tailored CV cites the link when it leans on that skill, which is the difference between a claim and a demonstration.',
  },
  persona: {
    term: 'Persona',
    body:
      'A slant on the same profile \u2014 its own emphasis, dream-job statement and directive defaults \u2014 for a career that supports more than one honest story. Exactly one persona is the default; campaigns run under it unless you choose another.',
  },
  title_synonyms: {
    term: 'Title synonyms',
    body:
      'Alternative names for a role, taken from the bundled title catalogue when you pick a title. They let a source be searched more widely without inventing a job title nobody advertises, and they are kept apart from the titles you actually chose so the two can never be confused.',
  },
  size_band: {
    term: 'Headcount band',
    body:
      'Company size as a band rather than a number, because that is the granularity registries and job boards actually publish. A collected company is mapped onto a band from its filed headcount; selecting nothing means every size is acceptable.',
  },
  collection_estimate: {
    term: 'Collection estimate',
    body:
      'How many sources, queries and pages the current directives imply, with the time and cost that follows — shown before you launch rather than after. It is an order of magnitude computed from your settings, not a promise: the campaign planner refines it once it knows what can be reused from the shared knowledge base.',
  },
  group_entity: {
    term: 'Group entity',
    body:
      'A company related to one you excluded — a parent, a subsidiary, a sister company — recognised by a shared name root or a shared internet domain, with legal forms such as NV, BV or GmbH ignored. It is why excluding one employer usually excludes its whole group, and why a group company trading under an unrelated name still has to be named yourself.',
  },
  compensation_disclosure: {
    term: 'Disclosing compensation',
    body:
      'Your salary expectation is used to filter and score opportunities and is left out of every generated CV, motivation document and e-mail unless you explicitly opt in. The flag defaults to off because naming a figure in a first approach settles the negotiation before it has started.',
  },

  // --- Ranked list of opportunities (FR-283, FR-284, FR-402, FR-264) -------
  manual_order: {
    term: 'My order',
    body:
      'A position you set by dragging a row. It overrides the computed ranking and it survives recalculation: a scoring pass rewrites the numbers and never touches the order, the pins, the tags or the shortlist. Positions are numbered within one campaign, which is why hand-ordering asks you to pick one first. "Clear my order" hands the ranking back to the score.',
  },
  timing_window: {
    term: 'Favourable timing',
    body:
      'A flag raised when something has just happened at the company that usually precedes hiring — a funding round, a reorganisation, a new office, a run of related vacancies. It says the moment is good, not that the role is; it never changes the score.',
  },
  compensation_estimate: {
    term: 'Compensation estimate',
    body:
      'A range built from what the advertisement stated, from national salary surveys, and from Glassdoor- or Levels-style observations for the same function, seniority and country. Each source is listed with the range it contributed and the weight it carried, and the confidence says how well they agreed. Where the employer stated a figure, that figure leads and the market sources are kept beside it for negotiation.',
  },

  // --- Responses received (extends FR-326, FR-422, FR-425) -----------------
  response_channel: {
    term: 'Channel',
    body:
      'How the answer actually reached you — e-mail, a phone call, a LinkedIn message, an ATS portal, in person. It is recorded rather than inferred, because it is the one thing the system cannot see for itself: mail sent through Resend has no inbox to poll, and a call leaves no trace at all.',
  },
  stated_outcome: {
    term: 'What you said happened',
    body:
      'Your own reading of a response — interest, a request for information, an interview invitation, a rejection, a referral, an automatic reply. It is kept apart from the model’s reading of the same text and always overrides it: you were there. A rejection carries exactly the same weight as any other outcome, and leaving it out is what makes a reply rate look better than it is.',
  },
  detected_reply: {
    term: 'Detected reply',
    body:
      'A reply the system found by itself in a connected mailbox, classified without anyone stating an outcome. It is listed beside the responses you entered by hand and counts identically, but it has no hand-entered record behind it, so its outcome is corrected on the pipeline board rather than on this screen.',
  },
  classification_confidence: {
    term: 'Reading confidence',
    body:
      'How sure the model is of its own reading of a reply. It says nothing about whether the reading is right — a confidently misread rejection is exactly the failure this screen exists to catch — so a response you stated yourself is recorded as certain and the model’s figure is shown only beside it.',
  },

  // --- Company knowledge base (FR-221..226, FR-241..246, FR-341..345) ------
  company_stage: {
    term: 'Stage',
    body:
      'Where a company is in its life: startup, scale-up, established, listed, public sector or non-profit. It says more about how a hire is decided — who signs off, how fast, against what budget — than the headcount does.',
  },
  trajectory: {
    term: 'Trajectory',
    body:
      'The direction the five filed years point in: growing, stable, declining, restructuring or volatile. It is read from revenue and headcount together, so a company growing revenue while shedding staff reads as restructuring rather than growing.',
  },
  estimated_figure: {
    term: 'Estimated figure',
    body:
      'A financial number the system derived rather than read off a filing — a missing year interpolated, a figure converted, a headcount inferred from personnel costs. Estimated years are marked wherever they appear, and every score computed from them rests on weaker evidence than a filed year.',
  },
  reconciliation_flag: {
    term: 'Reconciliation flag',
    body:
      'A note that a figure did not add up against the rest of the filing — a total that does not match its parts, a restated prior year, a currency or reporting-standard change. The figure is kept and flagged rather than dropped, because the discrepancy is often the interesting part.',
  },
  departmental_map: {
    term: 'Departmental map',
    body:
      'The business units and cross-company functions the site describes, with the head of each where it names one. It is what makes a speculative opening addressable: a role is proposed into a named unit, and the letter goes to the person who runs it rather than to a general mailbox.',
  },
  competitor_basis: {
    term: 'Competitor basis',
    body:
      'Why a peer was proposed: the same sector, shared customers, being named together in the press, a comparable product, or a directory listing. Bases converge — three weak reasons that agree count for more than one strong one, which is what the strength bar shows.',
  },
  application_window: {
    term: 'Application window',
    body:
      'When a company is most likely to be receptive, derived from its dated signals. Funding, a new office or a burst of postings each open a window of their own length; a signal whose window has not opened yet turns "they raised money last week" into "write at the end of the month".',
  },
  values_match: {
    term: 'Values match',
    body:
      'A comparison between what you said you want from an employer and what this company says about itself. A mismatch is reported only where the company\'s own material contradicts you; silence is reported as unknown — a question for the interview, not a warning.',
  },
  application_package: {
    term: 'Application package',
    body:
      'The four documents written for one opportunity, kept together and versioned together: a tailored CV, a company and job briefing, a motivation and fit document, and the introduction email. Regenerating the CV or the email withdraws any approval the package had, because what you approved is no longer what would be sent.',
  },
  factual_consistency: {
    term: 'Factual-consistency check',
    body:
      'Every structural claim in the generated documents \u2014 employers, job titles, schools, degrees, dates, skills and figures \u2014 matched against your profile before anything can be approved. The test is a set membership test, not a judgement: a claim the profile does not contain fails, and a failure blocks dispatch until you correct it or record a reason for overriding it.',
  },
  leak_scan: {
    term: 'Leak scan',
    body:
      'The opposite question to the consistency check: is everything in these documents traceable to material you are entitled to? Your profile, your composite, your accepted findings, and the company and vacancy this application is for. An email address, a domain or a proper noun from outside that set \u2014 another job seeker\u2019s data, an unrelated scraped page \u2014 is reported here, and no leak can be overridden.',
  },
  seeker_only_document: {
    term: 'For you only',
    body:
      'The briefing and the motivation document are prepared for you to read and are never attached to anything. Only the tailored CV is ever sent. The rule is enforced in the dispatch code rather than by convention: the function that builds the attachment list can return CV files and nothing else.',
  },
  approval_override: {
    term: 'Overriding a failed check',
    body:
      'A consistency finding can be overridden if you can say why the claim is defensible \u2014 the checker is deliberately strict and will flag a phrasing your profile supports in substance. The reason is stored with your name and the time. A leak, an objection from the recipient, or a missing document cannot be overridden at all.',
  },
  cv_template: {
    term: 'CV template',
    body:
      'The layout the same facts are rendered in. Changing it re-renders the stored document and costs nothing: no model is called, no wording changes, and the consistency verdict is unaffected. Both templates place your photograph beside the name block when your profile has one and you have not suppressed it.',
  },
  regeneration_instruction: {
    term: 'Regeneration instruction',
    body:
      'A sentence telling the generator what to change \u2014 \u201cshorter\u201d, \u201clead with the platform work\u201d, \u201cless formal\u201d. It steers emphasis, length and tone within the facts your profile already contains; it cannot introduce a claim, and anything it does introduce is caught by the consistency check before approval.',
  },
  bulk_approval_summary: {
    term: 'Approval summary',
    body:
      'Approving more than one application at a time is refused without a written statement of what is going to whom. The table you read and the sentence recorded describe the same thing \u2014 recipient, company, role and whether the opening was advertised or speculative \u2014 and the sentence is kept in the audit trail against your name.',
  },
  // --- Outcome patterns and redirection advice (FR-285, FR-425, CR-408) ---
  plausible_range: {
    term: 'Plausible range',
    body:
      'The band a rate could reasonably be, given how few applications it rests on \u2014 a Wilson interval, drawn behind each rate rather than only printed. Two out of four is 50%, but its plausible range runs from about 15% to 85%: wide enough that the segment tells you almost nothing yet. A band that stops short of your overall rate is the only case where a difference is worth acting on.',
  },
  evidence_label: {
    term: 'Evidence label',
    body:
      '\u201cThin\u201d means fewer than six resolved applications: shown for completeness, dimmed, and excluded from advice. \u201cIndicative\u201d means enough to notice but the plausible range still overlaps your overall rate. \u201cSuggestive\u201d means the range sits clear of it \u2014 the strongest thing your own data can say, and still not proof of a cause.',
  },
  baseline_rate: {
    term: 'Your overall rate',
    body:
      'The same outcome across every resolved application, whatever it was for. Each segment is reported as a difference from it in percentage points, because a 30% reply rate means something quite different when your overall rate is 10% than when it is 45%.',
  },
  expected_effect: {
    term: 'Expected effect',
    body:
      'The gap in percentage points between the two segments, recomputed from the stored figures rather than taken from the model\u2019s arithmetic. It is what has already been observed, not a forecast: moving your search does not transfer one segment\u2019s rate onto another.',
  },
  directive_set_version: {
    term: 'New directive-set version',
    body:
      'Accepting a proposal never edits your directives in place. It writes a new numbered version with the one change applied and a note saying where the change came from. The version you were using is untouched, so a campaign that ran under it stays explainable and reverting is a matter of restoring it on the directives screen.',
  },
  // --- Application pipeline (FR-421..425, FR-444) --------------------------
  pipeline_stage: {
    term: 'Pipeline stage',
    body:
      'Where one application currently stands: sent, replied, interview, offer or closed. The stage is a state, not a history \u2014 a card sitting in "offer" still records the day it reached "replied", which is what makes "how far do my applications usually get?" answerable later.',
  },
  automatic_transition: {
    term: 'Automatic and manual moves',
    body:
      'A reply moves a card only where it says something unambiguous about the stage: an interview invitation or a rejection does, "thank you, we will be in touch" does not. Automatic moves never go backwards. Dragging a card is your override and may go in any direction, and the card\u2019s history records which of the two moved it every time.',
  },
  pipeline_outcome: {
    term: 'Outcome',
    body:
      'How an application ended \u2014 accepted, rejected, withdrawn or no response \u2014 recorded only when the card closes. It is the only thing the "What works" analysis has to learn from, so a recorded rejection is worth exactly as much as a recorded acceptance and an unrecorded one is worth nothing.',
  },
  next_action_due: {
    term: 'Next action and its due date',
    body:
      'What you decided to do next about this application, and when. A default is set each time the card changes stage \u2014 answer the reply within two days, prepare an interview within three \u2014 and you can overrule both. A due date that has passed puts the card in "Due now" and marks it overdue on the board.',
  },
  silent_application: {
    term: 'Gone quiet',
    body:
      'A sent application that has had no answer for three weeks or more. It is reported and never acted on: nothing closes a card for you, because "they are slow" and "they are not interested" look identical from outside and only you can decide which this is.',
  },
  reply_classification: {
    term: 'Reply classification',
    body:
      'What an incoming reply was read as \u2014 an interview invitation, interest, a request for information, a rejection, a referral, an automatic reply or something else \u2014 with the confidence behind that reading. It decides whether the card moves on its own and what tone the drafted answer takes, so a misread reply is worth correcting.',
  },
  reply_draft: {
    term: 'Drafted answer',
    body:
      'The reply written for you to review, kept as its own object with the threading headers that put it back in the same conversation. It has a life of its own \u2014 written, edited, approved, sent \u2014 and nothing sends from the "draft" state. Approving it hands it to the mail screen; it is still only text in a database until you send it there.',
  },
  mock_interview: {
    term: 'Mock interview',
    body:
      'A rehearsal against this particular role, using the vacancy text, the company briefing and your own profile. One question at a time, feedback after each answer, and a closing list of what to rehearse. Sessions are numbered by round and stored, and a repeat round opens on the weak spots the previous one found.',
  },
  rehearsal_feedback: {
    term: 'How the answer was judged',
    body:
      'Feedback marked "llm" judges what your answer actually said. Feedback marked "structural" is what is available without the language model: it checks only the shape \u2014 whether there is a concrete situation, a figure and an outcome \u2014 and says so rather than pretending to have read the content.',
  },
  weak_spot: {
    term: 'Weak spot',
    body:
      'One topic your answers were thinnest on, with why it was thin and a concrete way to rehearse it. The closing list is drawn from the answers that scored lowest in the session, so it is about this rehearsal rather than generic interview advice.',
  },
  negotiation_brief: {
    term: 'Negotiation brief',
    body:
      'The case for a salary figure, prepared from the interview stage onwards. The numbers are computed \u2014 ability to pay and personnel cost per head from filed accounts, the comparable range from collected vacancies, the floor from your own compensation directives \u2014 and the language model is only asked to argue them, never to choose them. Every argument shows the evidence it rests on.',
  },
  walk_away: {
    term: 'Walk-away figure',
    body:
      'The number below which the role is not worth taking, derived from the minimum in your compensation directives rather than from the market. It exists so the decision is made before the conversation rather than in it, and it is never shown to an employer.',
  },
  // --- Networking and export (FR-461..463) ---------------------------------
  introduction_route: {
    term: 'Introduction route',
    body:
      'A way into a target company through somebody you already know, rather than a cold email. The routes are built per company rather than per vacancy, because one company has one network around it however many roles it has open. Nothing is sent: the person in the middle is never contacted by the system.',
  },
  intermediary: {
    term: 'Intermediary',
    body:
      'The person in your own network who would make the introduction, and the one the drafted message is written to. They are not the hiring contact — that message lives on the Applications screen — and asking them costs them a favour, so the draft says how you know each other and nothing it cannot support.',
  },
  route_strength: {
    term: 'Strength and relevance',
    body:
      'Two different questions, ranked together. Strength is whether the relationship is close enough that they would help at all: a former colleague scores above a direct connection, which scores above a fellow alumnus. Relevance is how close they sit to the decision, read from their seniority and their team. The order weights strength higher, because a willing introduction from one step away beats a reluctant one from the next desk.',
  },
  travel_tolerance: {
    term: 'Travel tolerance',
    body:
      'The distance you are willing to travel for an event, taken from the work-arrangement group of your search directives and turned into a kilometre ceiling. Events beyond it are left out of the radar unless you ask for them; an online event has no distance at all.',
  },
  target_attendee: {
    term: 'Target-company attendee',
    body:
      'Somebody named on an event\'s own public page — a speaker, a panellist, a listed attendee — who works for a company in your campaign. Only public pages are read, and only in a professional capacity. Who is going matters more to the ranking than what the event is about.',
  },
  campaign_package: {
    term: 'Campaign package',
    body:
      'One file holding everything about a single campaign: a PDF bundle written to be read by a person, the complete JSON record, the documents already generated, and a manifest naming what is inside and what was deliberately left out. It is meant to be handed to a career coach or kept as your own record.',
  },
  market_range: {
    term: 'Comparable range',
    body:
      'What comparable roles pay, with the method it came from and how many data points sat behind it. A range built from three stated salaries and one built from forty are shown the same way and mean very different things, so read the sample and the method before the numbers.',
  },
  // --- Monitoring: watchlist, notifications, digest (FR-401..403) ---------
  watched_company: {
    term: 'Watched company',
    body:
      'A standing instruction to recheck one company every few days on five channels \u2014 its careers page, the applicant-tracking board behind it, its newsroom, the hiring signals that news produces, and newly filed accounts. Anything new raises one notification, and a matching vacancy is put straight into the ranked list of the campaign the watch names, filtered by that campaign\u2019s own directives.',
  },
  check_channel: {
    term: 'Check channel',
    body:
      'One of the five places a recheck looks. They are independent: a company with no newsroom, no ATS and no filed accounts still gets its careers page read, and a channel that fails never stops the others. Each pass records what it found and what it could not reach, so a company that is quietly not hiring can be told apart from a watch that is quietly broken.',
  },
  digest_action: {
    term: 'The one recommended action',
    body:
      'The single thing the weekly digest suggests doing next, chosen by a fixed ladder rather than by a model: an unanswered interview invitation first, then a drafted reply waiting for you, an overdue follow-up, an offer without a negotiation brief, an upcoming interview, applications that never got an answer, and finally the best new opportunity. The ladder never changes, so the ranking is predictable, and every rung states why it beat the one below. It is a suggestion with a link \u2014 nothing acts on it.',
  },
  // --- Administration: models, sources, retention, audit (FR-361..364) ----
  effective_setting: {
    term: 'Effective value',
    body:
      'What is actually in force: the value from the installation\u2019s .env file, with an administrator override on top of it where one has been set. The two are shown side by side so a surprising figure can be traced to whoever typed it, and an override can be dropped to fall back to the default.',
  },
  task_routing: {
    term: 'Per-task model',
    body:
      'Which model handles one kind of AI call. Every call carries a task id such as extract.vacancy or generate.cv; unrouted tasks use the strong model, and naming a cheaper one for the bulk work \u2014 page summaries, skill normalisation \u2014 is where most of the cost is saved. A per-task choice overrides the cheap/strong split entirely.',
  },
  local_routing: {
    term: 'Local-model routing',
    body:
      'Sending the steps that read your personal profile to a model running on your own machine instead of to the provider. It applies to exactly three steps \u2014 the composite profile, the tailored CV and the motivation document \u2014 and only when a local endpoint is configured. Name any other task and nothing changes; leave the endpoint blank and the tasks you named still go to the provider.',
  },
  budget_degrade: {
    term: 'Degrade threshold',
    body:
      'The share of the token budget at which a campaign starts shedding optional work rather than stopping. Below it everything runs; above it the speculative openings for low-ranked companies are dropped first.',
  },
  terms_status: {
    term: 'Terms status',
    body:
      'What the source\u2019s own terms of service say about automated access: permitted, restricted, or prohibited. A prohibited source is disabled and cannot be enabled until an administrator acknowledges the risk by name \u2014 the acknowledgement is a timestamped entry in the audit trail, not a checkbox.',
  },
  access_method: {
    term: 'Access method',
    body:
      'How the adapter reaches the source: a documented API, plain HTTP fetching of public pages, or a browser session you opened and logged into yourself. Browser sources are slow by design and carry the terms risk that an API does not.',
  },
  source_cap: {
    term: 'Collection cap',
    body:
      'A ceiling on what one source may do in a single run \u2014 pages, records or seconds \u2014 set by the operator rather than declared by the adapter. It bounds the cost and the pace of a campaign independently of what the adapter would be willing to fetch.',
  },
  log_retention: {
    term: 'Retention and redaction',
    body:
      'How long the text of a prompt and its response is kept before being nulled. The sweep leaves the counters, the model and the record it was about \u2014 the cost reports need those and they are not personal data \u2014 and destroys the text itself. It cannot be undone.',
  },
  audit_trail: {
    term: 'Audit trail',
    body:
      'The append-only record of every act that mattered: who approved an application and who sent it, which profile version and which company snapshot went with it, which source terms were acknowledged, and every change to the model configuration. Nothing in the product updates or deletes a trail entry.',
  },
  right_to_erasure: {
    term: 'Erasure',
    body:
      'Deleting a job seeker and every private row behind them \u2014 profile, campaigns, opportunities, documents, contacts, messages and uploaded files. The shared company knowledge base is kept, because it describes companies rather than people. An anonymous marker is left so the erasure itself stays provable.',
  },
  // --- Mail setup (FR-325..327, NFR-204) -----------------------------------
  oauth_scope: {
    term: 'Granted access',
    body:
      'Exactly what Google was asked to allow, listed so it can be read rather than trusted. Dream Job requests two things: permission to send mail as you, and permission to read your mail \u2014 the second only so that replies and bounces to your applications can be found. Nothing else is requested, the token is stored encrypted, and it can be revoked from this screen at any time.',
  },
  token_expiry: {
    term: 'Token expiry',
    body:
      'When the short-lived access token stops working. It is not a deadline for you: a refresh token renews it silently before the next send. An expiry in the past therefore means the mailbox is idle, not broken. Revoking is what actually ends the access.',
  },
  domain_verification: {
    term: 'Domain verification',
    body:
      'Resend will only send from a domain whose DNS records prove you control it. Until stepvda.com is verified there, every send is refused by the provider rather than by Dream Job \u2014 and this screen cannot report the state, because asking Resend requires the API key that does not exist yet.',
  },
  webhook_signing_secret: {
    term: 'Webhook signing secret',
    body:
      'The shared secret Resend signs each delivery, bounce and complaint event with. The webhook has no session and no login, so the signature over the raw body is the whole of its authentication: without the secret every event is rejected, and no bounce sent through Resend is ever recorded.',
  },
  send_window: {
    term: 'Send window',
    body:
      'The hours during which an application may leave, applied in the RECIPIENT\u2019s time zone \u2014 worked out from the company\u2019s country and locations, not from yours. A message outside the window is queued with the moment it may go, not refused. It exists because a job application timestamped at three in the morning reads as a machine.',
  },
  send_pacing: {
    term: 'Pace',
    body:
      'The minimum gap between two messages. Applications leaving seconds apart are the clearest possible signal of bulk mail; a gap of a minute or two costs nothing and is the difference between a person writing letters and a campaign.',
  },
  daily_cap: {
    term: 'Daily cap',
    body:
      'The most applications that may be sent in one day. A fresh sending address has no reputation, and the volume it can carry grows slowly \u2014 exceeding the cap does not send faster, it gets the whole address filtered.',
  },
  message_id: {
    term: 'Message id',
    body:
      'The RFC 5322 identifier put on the message before it left. It is generated here rather than by the mail provider, which is why a reply can still be matched to its application when a send timed out after the provider had already accepted it.',
  },
  queued_dispatch: {
    term: 'Queued',
    body:
      'Approved, composed and waiting \u2014 for the pace, the daily cap or the recipient\u2019s send window. A queued message carries the moment it may go and leaves on its own; it is not an error, and nothing about it needs re-approving.',
  },
  bounce: {
    term: 'Bounce',
    body:
      'A delivery failure reported back by the receiving mail server. Gmail puts it in your inbox, where it is read on the next check; Resend pushes it to the webhook. Bounces matter beyond the one application: a run of them is what makes a provider start filtering everything you send.',
  },
  follow_up: {
    term: 'Follow-up',
    body:
      'One short reminder, sent at most once per application, after an interval of silence with no reply and no bounce. It is threaded onto the original message so it arrives in the same conversation, carries no second copy of the CV, and is never sent without you reading it first.',
  },
  // --- Dream-job intelligence (FR-381..384, FR-443) ------------------------
  gap_dimension: {
    term: 'Gap dimensions',
    body:
      'The six things a gap can be about: a skill, a certification, a language, experience or seniority, the leadership scope you can evidence, and how visible your work is in public. They are the dimensions the collected postings actually ask about, so a gap only appears where the market asked for something your profile does not show.',
  },
  decisive_opportunity: {
    term: 'Where a gap was decisive',
    body:
      'The opportunities that measurably lost points because of this gap, with the number of profile-fit points it cost each one. The list is computed from the same arithmetic that produced the ranking, not asserted \u2014 so \u201cdecisive\u201d means the score moved, not that the gap sounds important.',
  },
  closing_effort: {
    term: 'Estimated effort',
    body:
      'How long closing the gap would plausibly take, in weeks, months, quarters or years, with the months behind that word. It is an estimate from the kind of gap it is \u2014 a credential has a known length, a skill needs a course and something showable \u2014 and not a promise about you.',
  },
  dream_fit_threshold: {
    term: 'Dream-job fit threshold',
    body:
      'The dream-job fit score above which a role counts as somewhere you actually want to end up. It is yours to set, and it is the switch behind the stepping-stone paths: when nothing in the ranked list clears it, the useful answer stops being a better sort and becomes a route.',
  },
  log_level: {
    term: 'Log level',
    body:
      'How serious the line is: DEBUG and INFO are the installation narrating itself, WARNING is something that coped, ERROR is something that did not, and CRITICAL is something that stopped. The counts over a window are the fastest honest answer to \u201cis anything wrong?\u201d \u2014 a handful of warnings an hour is ordinary; a jump in them is the thing to look at.',
  },
  log_tail: {
    term: 'Tail',
    body:
      'The last N lines of one log file, read from the end backwards. Reading 200 lines of a 10 MB file touches the end of it rather than the whole thing, which is why the tail is instant and the window it can scan is bounded. \u201cOlder lines not read\u201d means the file continues above what is shown.',
  },
  correlation_id: {
    term: 'Correlation id',
    body:
      'Eight characters attached to everything one request causes \u2014 the request line, the application lines, the database statements, and the browser\u2019s own report of it. Filtering every file by that one id reassembles a single click from the six places it was written down.',
  },
  log_channel: {
    term: 'Channel',
    body:
      'Which slice of the application wrote the line \u2014 request, database, egress, mail, audit \u2014 taken from the logger name rather than the module, because that is the level at which somebody scanning a file wants to filter. Requests, database statements, browser reports and the audit trail each also have a file of their own.',
  },
  corpus_keyword: {
    term: 'Corpus keyword',
    body:
      'A term counted across the vacancies this campaign collected, recommended when it appears in at least 15% of them. It is what recruiters in your market are typing, not generic profile advice \u2014 which is why the list is empty rather than invented when no vacancies have been collected yet.',
  },

  // --- The Apply Browser's send guard (RK-05, FR-325) ---------------------
  dry_run: {
    term: 'Dry run',
    body:
      'The state this installation is in while no message may leave it. Everything up to the finished e-mail runs for real \u2014 the approval rule, the consistency gate, the guard rails in the recipient\u2019s time zone, the message composed with your tailored CV attached \u2014 and the assembled message is then written to disk as an .eml file instead of being handed to a mailbox. It is on by default, and pressing Send while it is on tells you exactly what would have gone out.',
  },
  send_guard: {
    term: 'Where the guard lives',
    body:
      'In the mail layer on the server, not in this interface. While the dry run is on, every shipped mail backend refuses to carry a message before the provider is touched, so no screen, no API client and no future code path in this process can send one. That is why the send buttons are left enabled: the button is not what is holding the message back, and pretending otherwise would teach you the wrong thing about your own installation.',
  },

  // --- Who is actually hiring (FR-143, FR-341, NFR-402) -------------------
  interim_agency: {
    term: 'Interim or staffing agency',
    body:
      'A staffing, interim or recruitment agency advertises a real vacancy on behalf of an employer it does not name in the advert. About one posting in five here is one, and a third of the Belgian public-service rows. Everything we normally say about a company — its accounts, its trajectory, its values, what it is likely to hire for next — is marked “cannot assess” on these postings rather than computed against the agency, because the agency is not the company you would work for. A consultancy whose staff work at client sites is not an agency: it employs them itself.',
  },
  employer_not_disclosed: {
    term: 'Employer not disclosed',
    body:
      'What an agency posting leaves out. Four rows in five name no end client anywhere, and the description they do give — “an international machine-builder near Roeselare” — does not identify one company: the eight richest such descriptions here matched between zero and twenty registered companies each. So the employer is shown as not disclosed, the company-related parts of the score are left uncomputed rather than estimated, and nothing generated for you ever names a guessed employer. Asking the recruiter who the client is settles it — then the briefing can be regenerated against the real company.',
  },
  resolution_rung: {
    term: 'Resolution rung',
    body:
      'Which step of the ladder answered “is this organisation the employer?”. Cheapest and most certain first: what is already on record, the pattern of the employer’s own postings, the company register (a Belgian NACE 78.2 is a licensed temporary-employment activity and settles it), how EURES files the employer, and last the company’s own website, read and quoted. A rung either answers or hands down with a reason, and the rung travels with the verdict because a registered activity code and a sentence on a home page are not the same kind of evidence.',
  },

  // --- What a source ended on, on the live dashboard (FR-185, FR-361) ------
  collection_outcome: {
    term: 'What each source did',
    body:
      'Every source in the plan ends on one of six answers, and only one of them is a defect. This screen used to add them up into a single “errors” figure, and that figure was useless: of 538, some 308 were job boards that no longer exist, 192 were sources this product declined to read on principle, and about a dozen were things somebody had to fix. Separating them is what makes the dozen findable. The rule is worth stating plainly, because the opposite mistake was made here first: a source that fetched nothing used to be recorded as finished with no errors, and that hid a total collection failure. Nothing is quietened here — an answer nobody recognises counts as a failure, not as a success.',
  },
  outcome_succeeded: {
    term: 'Collected',
    body:
      'The source was read and records were written to the knowledge base. This is the only state that means data arrived. A source that answered correctly and simply holds nothing for your query is counted under “skipped” instead, because it produced no record — saying it succeeded would make an empty result look like a full one.',
  },
  outcome_blocked: {
    term: 'Blocked',
    body:
      'We declined to read the source, and we were right to. Its robots.txt disallows the path (FR-182), it answered a 403 bot wall, or its terms of service prohibit automated access and no administrator has acknowledged an exception (IR-101). This is not a failure and it is not something to fix: it is a decision, and it is listed source by source with the reason so that it can be defended to anyone who asks how this product collects. Where a source is one that needs an administrator’s acknowledgement, whether that has been given is shown beside it.',
  },
  outcome_gone: {
    term: 'Gone',
    body:
      'The target is no longer there: the board answered 404 or 410 for a slug the registry believed was live. Expect a steady number of these. The board registry is harvested from Common Crawl, the Wayback Machine and Hacker News, and liveness was measured at 87.5% for fresh crawl entries and 27.5% for Wayback-only ones — so a dead slug is a fact about the world, not a bug, and the registry learns it once and stops offering it. One thing here is not decay: if an adapter comes back gone for every slug it tries, its URL shape has probably broken, and that is called out separately (NFR-403).',
  },
  outcome_failed: {
    term: 'Failed',
    body:
      'Something actually went wrong: a 5xx from the server, a transport error, a parse crash, an adapter exception, or a fetch that produced pages but no record at all. This is the only number on the dashboard that asks anything of you, which is why it is the only one shown in red and the only one given the top of the block. Everything else — declined sources, dead boards, work the page budget has not reached — is an expected outcome and is deliberately kept quiet so that this figure stays legible.',
  },
  outcome_skipped: {
    term: 'Skipped',
    body:
      'There was nothing to do. Either you excluded the source when you reviewed the plan (FR-163), or the source was read successfully and stated that it holds nothing matching this query. The second case is a real answer and not an empty one: a partitioned sweep asks narrow questions on purpose, and some of them have no answer. It is kept out of “collected” because no record was written, and out of “failed” because nothing went wrong.',
  },
  outcome_capped: {
    term: 'Not started: page budget',
    body:
      'Not an outcome at all, which is why it sits on its own below the rule. These plan items were never asked anything: the run reached its page cap first (FR-186) and stopped, leaving them runnable. Raising the cap and resuming continues them from the checkpoint rather than starting over. A large number here is normal on a broad plan and means only that the campaign is bounded — it says nothing about whether the sources work.',
  },
  // --- Telling one plan item from another (FR-162, FR-163) ----------------
  source_target: {
    term: 'Target',
    body:
      'Which particular thing one row asked for. A source appears once per ' +
      'target, so a plan can hold 2,417 Personio rows that all read "Personio" ' +
      'until the target is named: the company board, the region and sector, the ' +
      'website. Two rows with the same target read the identical thing, and are ' +
      'shown as one row that says how many plan items name it.',
  },
  // --- The activity log on the live dashboard (FR-361) --------------------
  activity_log: {
    term: 'Activity',
    body:
      'The last thing each source did, newest first, with the time it happened. ' +
      'It refreshes every few seconds while collection is running and stops when ' +
      'the run does. Each source appears once, showing its most recent action, ' +
      'so a run with thousands of sources in it stays readable.',
  },
}


export function pageHelp(pathname) {
  const base = '/' + (pathname || '').split('/')[1]
  return PAGE_HELP[base] || null
}
