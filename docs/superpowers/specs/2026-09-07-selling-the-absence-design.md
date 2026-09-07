# Selling the absence: clinics, hospitals, insurance, and the businesses whose sites are fine

**Status:** design, awaiting review
**Date:** 7 September 2026

## The problem, stated from the code

Titan has fourteen finding types and every one of them is a website defect:

```
broken_internal_link          missing_meta_description
broken_primary_cta            missing_mobile_viewport
failed_network_requests       missing_security_headers
high_friction_contact_form    no_booking_or_enquiry_path
images_missing_alt_text       no_structured_data
javascript_console_errors     no_visible_phone_number
serious_accessibility_violations
slow_largest_contentful_paint
```

The composer writes about findings. `select_offers` is keyed on finding types.
Scoring sets `services_deliverable=bool(offers)`. So **a business with a clean
website produces nothing to say and cannot be written to at all** — however far
behind it is operationally.

That is the wrong filter for an AI services business. A dental practice running
an AI receptionist and one taking every booking by telephone are, to Titan
today, distinguishable only by whether their CSS is tidy.

### What already exists

`titan/intelligence/modernisation.py` was written for exactly this and is
careful about the hard part. It reads six capabilities — conversational,
self-service booking, marketing automation, reputation automation, analytics,
modern site — by matching vendor tokens against technologies and script URLs
rather than generic words, because "chat" appears in a thousand class names.

Crucially it already refuses to over-claim:

- a cookie wall makes every capability `NOT_MEASURED`, not `ABSENT`
- an unreadable page contributes nothing rather than an absence
- `MIN_MEASURED_CAPABILITIES = 4` — below four of six, no gap score is produced
  at all, because the number would describe how much of the site we managed to
  read rather than how the business runs

**And its output goes nowhere except scoring.** `modernisation_gap` is one
float in `ScoringInput`. It moves a lead up the list and never becomes a
sentence.

This design closes that gap, and adds the three verticals where the gap is
widest.

## Scope

1. Three new industries: `CLINIC`, `PRIVATE_HOSPITAL`, `INSURANCE`.
2. Measured absence becomes a claim the composer may make.
3. Aggregate peer comparison, as a claim about our own measurement.
4. Offers and scoring that follow from a capability gap, not only a defect.

Out of scope, deliberately: anything promising handling of patient records or
insurance claims. That is special-category data under UK GDPR and FCA territory
respectively, and it is a different conversation to open cold. The offer set
here is front-desk: booking, enquiry routing, after-hours answering, follow-up
and review automation, plus the existing website work.

## 1. Three verticals

Each follows the thirteen existing playbooks. Per vertical: priority crawl
paths, a priority list naming what to look for, an offer set with evidence
gates, and vernacular so a practice manager and a broker do not receive
identical prose.

| Vertical | Priority paths | What the pitch turns on |
|---|---|---|
| `CLINIC` | `/appointments`, `/book`, `/patients`, `/new-patients`, `/contact`, `/fees`, `/services` | Booking outside office hours; new-patient registration; recall |
| `PRIVATE_HOSPITAL` | `/appointments`, `/consultants`, `/referrals`, `/patients`, `/visiting`, `/contact` | Referral routing; consultant enquiry handling; switchboard load |
| `INSURANCE` | `/quote`, `/claims`, `/contact`, `/policies`, `/renewals`, `/about` | Quote request friction; renewal chasing; out-of-hours enquiry capture |

Insurance is the one that differs in kind: the conversion event is a *quote
request*, not an appointment, so its offer set leads with enquiry capture and
renewal follow-up rather than booking.

## 2. Absence as a claim

### New finding types

Sourced from `ModernisationProfile`, not from the DOM defect detectors:

| Issue type | Raised when |
|---|---|
| `no_conversational_capability` | `CONVERSATIONAL` is `ABSENT` and measured |
| `no_self_service_booking` | `SELF_SERVICE_BOOKING` is `ABSENT` and measured |
| `no_follow_up_automation` | `MARKETING_AUTOMATION` is `ABSENT` and measured |
| `no_review_automation` | `REPUTATION_AUTOMATION` is `ABSENT` and measured |

`no_self_service_booking` and the existing `no_booking_or_enquiry_path` are
different claims and must not collapse into one. The existing type means *there
is no way to make contact at all*. The new one means *contact exists and it is a
telephone number* — a business that is reachable but not bookable. Only one is
ever raised for a given lead; the existing defect wins, because "we cannot find
any way to contact you" is the more urgent thing to say.

### The evidence rule for an absence

A defect is positive evidence: we saw a 404. An absence is not, and the failure
mode is worse — telling a clinic that just spent £20,000 on an AI phone system
that they have no AI loses that recipient permanently and deservedly.

Two guards:

**The claim is about what we read, not about what they have.** The rendered
sentence is *"I went through your site and couldn't find a way to book without
calling"* — not *"you have no online booking"*. The first stays true when we
missed something and invites a correction; the second is an assertion about
their business that we cannot actually support from eighteen pages.

**The claim map carries the pages.** An absence finding's evidence is the list
of page URLs that were read and found not to contain it, which is exactly what
the validator needs to trace the sentence to an observation. An absence with
fewer than `MIN_MEASURED_CAPABILITIES` behind it is not raised at all.

## 3. Peer comparison

The persuasive core, and the part with a new legal shape: a statement about
businesses other than the recipient, in a cold commercial email.

**It is framed as a claim about our own measurement, because that is the only
version we can defend.**

> Six of the ten dental practices I looked at in Leeds take bookings online.

Not "six of ten practices in Leeds" — we have not surveyed Leeds, and the
stronger sentence is the one we would have to retract. The weaker one is also
harder to argue with.

Gates:

- **Cohort floor of 8.** Below that, "three of four" is noise dressed as a
  finding.
- **Freshness bound.** Crawls older than 30 days are excluded; the reserve is
  already sized at five days for the same reason.
- **Same industry and same market**, taken from the campaign, so the comparison
  is to businesses the recipient would recognise as peers.
- **Nobody is ever named.** Naming a competitor is a factual claim about a third
  party made for commercial gain; the aggregate is not.
- **Only raised alongside a real finding.** The comparison creates urgency; the
  finding is what establishes we actually looked. Comparison alone reads as a
  cold statistic and is refused.

## 4. What the message becomes

The existing four-part shape is unchanged — greeting by their clock, the
observation, what changes if they fix it, who I am — with the comparison sitting
inside the observation.

**Today, for a clinic with a clean site:** no message is possible.

**Under this design:**

> Good morning,
>
> I went through northgateclinic.co.uk this morning and couldn't find a way to
> book an appointment without calling — the contact page lists a number and an
> address, and the new-patient page asks people to ring during opening hours.
> Six of the ten private clinics I looked at in Manchester take bookings on the
> site itself.
>
> That gap is the one that costs after six o'clock: an enquiry that arrives when
> nobody is at the desk either waits until tomorrow or goes to whoever answers
> first. Putting a booking path and an out-of-hours responder on the site keeps
> those enquiries instead of losing them to the clock.
>
> I build these systems — VoxCircuit answers enquiry calls and books the
> appointment, and there's a case study on it below.

Three things are doing work there and all three are checkable: a real
observation, a measured comparison, and a mechanical consequence.

### What stays refused

The validator's existing rules are unchanged and they are what makes the rest
credible:

- **No invented outcomes.** "This would win you 30% more bookings" is
  unknowable. It is also weaker than the truth: a clinic manager can verify that
  six of ten peers book online and cannot verify a percentage, so the true
  sentence is the frightening one.
- **No claims about their revenue, patient numbers, or what they are losing.**
  We do not know.
- **No named competitor.**
- **No fabricated urgency.** "Your competitors are taking your patients" is not
  an observation.

The pressure comes from the comparison being true, not from adjectives.

## 5. Scoring and offers

`select_offers(industry, evidenced_types)` gains capability gaps as an evidence
source, so an offer can be gated on `no_self_service_booking` the way it is
today on `broken_primary_cta`. Without this the whole design is inert:
`services_deliverable` stays false for a clean-site business and it never
becomes sendable.

`modernisation_gap` keeps its scoring role. It is now both a ranking signal and
an evidence source, which is the correct relationship — how far behind a
business is should decide both how much we want them and what we say.

## Testing

Every new behaviour gets a planted violation checked in both directions. The
ones that matter:

- An unmeasured capability is never pitched as absent. Plant the removal of the
  `NOT_MEASURED` guard and a cookie-walled site must start claiming absences.
- A cohort of seven produces no comparison; a cohort of eight does.
- A stale cohort produces no comparison.
- The comparison never renders a business name — asserted against a cohort built
  from named organisations.
- `no_booking_or_enquiry_path` and `no_self_service_booking` never both appear.
- A message with a comparison and no finding is refused by the composer.
- The four new claim shapes each pass the *real* validator, not a mock. The
  27 August work log records why: composing all fourteen defect types through
  the real validator caught two failures a happy-path example missed.

## Rollout

1. The three playbooks alone, behind the existing offer gates. Nothing changes
   in what may be said; it only makes three verticals workable. Low risk.
2. Absence findings, raised but not yet pitched — scored and stored so the
   population can be measured before a word goes out.
3. Comparison, on one campaign first.
4. Widen once the reply rate on (3) is measurable.

Step 2 exists because the honest question — *how many leads does this actually
make sendable?* — has a number, and it should be known before the message
changes.

## Open questions for review

1. **The `PRIVATE_HOSPITAL` vertical may be too small to be a vertical.** Small
   private hospitals in the target markets number in the dozens per city, not
   the hundreds. It may belong inside `CLINIC` with a different offer set rather
   than as its own campaign.
2. **Insurance brokers are heavily solicited.** Their inboxes are the most
   competitive of the three, and the reply rate should be watched separately
   rather than pooled with the medical verticals.
3. **Should the comparison ever name the market when the cohort is thin?**
   "Six of the ten I looked at in Manchester" is specific; if only eight were
   crawled in that city, naming the city implies a survey we did not do. Options
   are to drop the city or to raise the floor when the city is named.
