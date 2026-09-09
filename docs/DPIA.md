# Dream Job — Data Protection Impact Assessment

| | |
|---|---|
| Document | Data Protection Impact Assessment (NFR-304) |
| Version | 1.0 — **draft for legal review** |
| Date | 9 September 2026 |
| Author | Stephane van der Aa |
| Status | Must be reviewed and signed off **before production use** |
| Legal basis for the assessment | GDPR Article 35 |

> **This is a technical DPIA prepared by the builder of the system, not legal
> advice.** It documents what the software actually does with personal data,
> which is the part an engineer can state with authority. The residual-risk
> judgements and the Article 36 question in §9 need a data-protection
> professional before the system processes real hiring contacts at scale.

---

## 1. Why a DPIA is required

Article 35(3) requires one where processing involves systematic and extensive
evaluation of personal aspects based on automated processing, or large-scale
processing. Dream Job meets the threshold on three counts:

1. **Systematic profiling** of the job seeker, combining supplied documents with
   web-sourced material into an evaluative composite.
2. **Processing personal data of third parties** — hiring contacts — who have no
   relationship with the controller and have not been asked.
3. **AI in an employment context**, which Annex III of the AI Act treats as
   high-risk for the deploying employer. Dream Job acts for the *candidate*, not
   the employer, so it falls outside that classification — but the reasoning
   should be documented rather than assumed.

---

## 2. The processing, in plain terms

| | |
|---|---|
| **Controller** | The job seeker, in a single-tenant installation. In a shared installation, whoever operates it. |
| **Purpose** | Finding suitable employment for the job seeker, and making a professional approach to the people who could offer it. |
| **Scale** | Tens of thousands of company records; hundreds to low thousands of contacts per installation. |
| **Duration** | For as long as the job seeker is searching, plus the retention periods in §6. |

### Categories of data subject

**The job seeker** — the primary user, who supplies their own data.

**Hiring contacts** — named individuals at target companies, whose professional
contact details are collected so that an application can reach a person rather
than a mailbox. *They did not ask to be in this system.* Their interests are the
central concern of this assessment.

**Third parties incidentally present** — people named on a company's team page,
in press coverage, or in a filing. Collected as part of a company profile, not
as individuals of interest.

### Categories of personal data

| Subject | Data | Source |
|---|---|---|
| Job seeker | Identity, contact details, full employment and education history, skills, photo, publications, free-text aspirations, compensation expectations, application history and outcomes | Supplied directly, plus identity-verified web enrichment |
| Hiring contact | Name, role, department, employer, professional email address, LinkedIn URL, how the address was obtained | Company websites, press pages, pattern inference, browser-automated collection |
| Third parties | Name and role where published on a company page | Company website crawl |

**Special categories (Article 9) are excluded by design.** The system does not
seek them, and filters them out of enrichment findings before storage (FR-127).

> **A design correction worth recording.** The first implementation of that
> filter matched bare keywords — *health, medical, political, ethnic, race* —
> and so deleted "healthcare data company", "medical devices", "Political
> Science" and "human rights platform" from ordinary professional histories.
> It protected nobody and damaged exactly the data subjects whose careers most
> needed representing. Detection now separates the occupational sense from an
> assertion about a person. Recorded here because over-redaction is a data
> quality failure, not a safe default.

---

## 3. Necessity and proportionality

**Is the processing necessary for the purpose?** For the job seeker's own data,
plainly yes — it is their application. For hiring contacts, the question is
whether a *named person's* professional address is necessary when a generic
mailbox exists. The system's position: a named, relevant contact is what makes
an application reach a human, and the alternative (spraying `info@`) is worse for
everyone including the recipient. The contact hierarchy — hiring manager, then
talent acquisition, then generic mailbox (FR-301) — reflects that.

**Is it proportionate?** The controls that make it so:

- **Only professional contact details are stored** (FR-306). No personal
  addresses, no phone numbers, no anything the purpose does not need.
- **Collection is bounded by directives**, not open-ended. The system
  deliberately "collects what the directives justify rather than downloading the
  world".
- **Volume is capped** per campaign — pages, companies, people, duration — and
  an operator's cap cannot be raised by a campaign.
- **Sending is capped** — a daily maximum, a minimum interval, and send windows
  in the recipient's own time zone.

---

## 4. Lawful basis

| Processing | Basis | Notes |
|---|---|---|
| Job seeker's own data | Contract / consent | They are the user and the controller |
| Transfer of profile data to DeepSeek | **Explicit consent** (Art. 49(1)(a)) | Recorded before any transfer; see §7 |
| Web enrichment about the job seeker | Consent | Separately switchable; off means no request leaves the machine |
| Hiring contacts' professional data | **Legitimate interest** (Art. 6(1)(f)) | Balancing test below |
| Browser-automated collection | Legitimate interest, narrowed | Additional retention limits, see §6 |

### Legitimate-interest balancing test — hiring contacts

**The interest.** A job seeker approaching a relevant person at an employer about
work. This is a recognised and ordinary purpose; it is what a job application is.

**Necessity.** The alternative — applying only through portals and generic
mailboxes — measurably reduces the chance of reaching a decision-maker, and the
whole premise of the product is reaching unadvertised opportunity, which has no
portal.

**The data subject's interests.** A recruiter or hiring manager can reasonably
expect to receive applications; that is their function, and their professional
address is usually published for exactly that. The intrusion is one email to a
work address about work.

The balance tips against the controller if the message is irrelevant to the
recipient's function, if there is no way to object, or if volume turns it into
spam. The system addresses all three:

- **Relevance is structural.** Contacts are found *per opportunity*, ranked by
  departmental relevance. An off-target email is a product failure, not just a
  legal one.
- **Every introduction email carries an objection sentence** (NFR-302).
- **An objection is permanent and installation-wide**, enforced in the query the
  generator uses rather than only in the interface. Not a preference — a block.
- **Volume controls** as in §3.

**Conclusion.** Legitimate interest is available for B2B introduction emails to
professional addresses, consistent with the ePrivacy B2B position in Belgium
(CR-403), *provided the controls above remain enforced*. If the caps are raised
substantially or the relevance filter is weakened, the balance should be
reassessed.

---

## 5. Data subject rights

| Right | How it is served |
|---|---|
| Information (Art. 13–14) | The job seeker is informed in-product before any transfer. **Hiring contacts are informed at the point of first contact** — the email itself is the Art. 14 notice, and it identifies the sender and offers objection |
| Access / portability | `GET /api/auth/me/export` returns every private row as JSON |
| Rectification | Every profile field is editable; composite-profile statements are individually correctable |
| Erasure | `DELETE /api/auth/me` removes every private row **and every file on disk**. The table list is derived from the live schema, so a table added by a later migration cannot be missed. Shared market data — which carries no link to any job seeker — survives |
| Objection | Permanent block per address, honoured everywhere |
| Not to be subject to automated decisions (Art. 22) | **No automated decision produces a legal or similarly significant effect.** Scoring is advisory; a human approves every application (NFR-305) |

**Gap.** There is no self-service route for a *hiring contact* to exercise access
or erasure — they must contact the controller, who then acts through the
objection mechanism and manual deletion. For a single-tenant installation this is
proportionate. For a hosted service it is not, and would need a published contact
point and a documented procedure.

---

## 6. Retention

| Data | Retention | Mechanism |
|---|---|---|
| Job seeker profile and versions | Until erasure requested | User-controlled |
| Company, vacancy and financial records | Indefinite, refreshed on staleness | Not personal data |
| Contacts (public sources) | While the campaign is active, then reviewed | Retention sweep |
| **Contacts (browser automation)** | **Campaign duration + grace period**, then deleted | Automatic sweep; never shared between job seekers (NFR-303) |
| Raw fetched documents | Cache TTL, then re-fetched | Content-hash store |
| AI prompts and responses | Retention period, then redacted in place | Scheduled sweep (FR-364) |
| Audit trail | Retained — it is the accountability record | Append-only |

---

## 7. International transfer

**DeepSeek processes data outside the EU.** This is the most significant transfer
risk in the system and it is a fixed technology constraint (CR-409).

Controls:

- The job seeker is **informed before any profile data is sent**, and consent is
  recorded with the exact text they were shown (CR-410).
- **Do-not-disclose fields and special-category data are stripped before the
  prompt is built**, not filtered afterwards.
- **Every call is logged** with its task, model, prompt and response, so what was
  transferred is auditable rather than assumed.
- **A local model can serve the privacy-sensitive steps** — composite profile,
  tailored CV, motivation document — with no code change, removing the transfer
  entirely for the most sensitive processing (NFR-306).

> **Open question for legal review.** Consent under Art. 49(1)(a) is a derogation
> intended for occasional transfers, not a basis for routine bulk processing.
> A production deployment should either (a) place the privacy-sensitive steps on
> a local model and keep the cloud provider for non-personal extraction work, or
> (b) obtain a data-processing agreement with appropriate safeguards. **The
> architecture supports (a) today** and it is the recommended posture.

---

## 8. Security measures

Detailed in the [Technical Architecture](Technical_Architecture.md), §4.
Summarised against Article 32:

| Measure | Implementation |
|---|---|
| Encryption at rest | AES-256-GCM, keys derived per job seeker. Erasing a job seeker makes their ciphertext unreadable even if a copy survives |
| Authentication | Argon2id with a password policy; TOTP MFA; sessions bound to the client |
| Third-party credentials | **Never stored.** Browser automation attaches to a session the user owns and controls |
| Mail tokens | Encrypted, scoped to send and bounce-read only, revocable from the interface |
| Injection resistance | Scraped content is fenced as untrusted data and never concatenated into instructions |
| Cross-contamination | Generated documents are scanned for content not attributable to that job seeker's own sources |
| Accountability | Immutable audit trail of who approved and sent what, with the data versions used |

**Known weaknesses, stated rather than glossed:**

1. **The SQLite file is not itself encrypted.** Credentials and secrets are
   encrypted at field level, but whole-database confidentiality relies on
   disk-level encryption. On a laptop with FileVault this is reasonable; on a
   shared host it is not. SQLCipher is the fix.
2. **Passkeys are not implemented.** Password + optional TOTP is what exists, and
   MFA is opt-in, so a fresh account is password-only until enrolled.
3. **An administrator can read other job seekers' AI call logs**, including
   prompt text. Harmless when the operator is the sole data controller;
   unacceptable in a shared installation. **Must be resolved before any
   multi-tenant deployment.**

---

## 9. Risk assessment

| # | Risk | Likelihood | Impact | Mitigation | Residual |
|---|---|---|---|---|---|
| 1 | A namesake's data enters the job seeker's profile and reaches an employer | Medium | High | Multi-signal identity matching; only *confirmed* merges automatically; rejection is permanent; consistency check before dispatch | **Low** |
| 2 | Generated CV asserts something untrue | Medium | High | Grounding on profile facts only; deterministic date/employer/title checks; mandatory human preview | **Low** |
| 3 | Contact data leaks between job seekers | Low | High | Query-level isolation; browser-collected contacts never shared; export tested to contain no other job seeker's data | **Low** |
| 4 | Recipient objection not honoured | Low | High | Block enforced in the generator's own query, not the interface | **Low** |
| 5 | Volume turns legitimate interest into spam | Medium | Medium | Daily caps, pacing, recipient-timezone send windows, validated addresses only | **Low–Medium** |
| 6 | Profile data transferred outside the EU beyond what consent covers | Medium | Medium | Pre-transfer redaction, recorded consent, full call log, local-model option | **Medium** — see §7 |
| 7 | Job seeker's LinkedIn account restricted | Medium | High *(to the job seeker)* | Tight scope caps, human pacing, stop-on-challenge, explicit warning and recorded acknowledgement | **Medium** — accepted knowingly by the user |
| 8 | Prompt injection from a scraped page redirects the model | Medium | Medium | Content isolation, instruction/data separation, output validation, flagged instructions surfaced | **Low–Medium** |
| 9 | Administrator over-reach in a shared installation | Low | High | *Unmitigated* — see §8.3 | **High for multi-tenant; N/A single-tenant** |

**Article 36 prior consultation** is not required if the residual risks above are
accepted as low. Risk 9 must be closed before a multi-tenant deployment, at which
point the assessment should be re-run rather than amended.

---

## 10. Conclusion and conditions

The system as built is **suitable for single-tenant production use** subject to:

1. **Legal review of §7** — the transfer basis. Recommended posture: route the
   privacy-sensitive steps to a local model.
2. **Disk-level encryption** on the host, until the database is encrypted at rest.
3. **MFA enrolled** on any account holding real contact data.
4. **The caps and objection controls left in force.** They are what makes the
   legitimate-interest balance hold; weakening them invalidates §4.

**Not suitable for multi-tenant deployment** until the administrator-access issue
(§8.3) is resolved and the balancing test in §4 is re-examined for a controller
who is not also the data subject.

### Review

To be reviewed on any of: a change to the lawful basis or transfer arrangement;
a substantial increase in sending volume or contact retention; a move to
multi-tenant; a new category of personal data; or annually.

| | Name | Date | Signature |
|---|---|---|---|
| Prepared by | Stephane van der Aa | 9 September 2026 | |
| Reviewed by *(data protection)* | | | |
| Approved for production | | | |

---

*Companion documents: [Functional Design](Functional_Design.md) ·
[Technical Architecture](Technical_Architecture.md) · [README](../README.md)*
