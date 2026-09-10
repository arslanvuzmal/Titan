"""How a message is written for the business that receives it.

The composer used to write one email and substitute a domain into it. Every
industry got the same four sentences, so a solicitor and a gym owner read
identical prose about "visitors following a navigation link", and the offer
underneath was whichever one the playbook happened to list first.

Two rules replace that, and both come from what the recipient is actually doing
when they hit the defect.

**A finding has a kind, and the kind decides the shape of the message.** A
broken booking button is a person who had already decided and could not finish;
a missing alt attribute is a quality problem nobody is bleeding money over.
Selling both with the same urgency is how a reader learns the sender did not
really look. Three engines, in :class:`Engine`.

**A business has a vocabulary, and it is not this system's vocabulary.** A law
firm has enquiries, consultations and prospective clients; a dental practice has
treatments, bookings and new patients; a gym has trials, members and first
sessions. Writing "conversion path" at any of them is writing at them. Every
sentence below that touches the recipient's business is drawn from
:class:`Vernacular` and never from a generic pool.

The strict rule underneath all of it: **never sell a service unrelated to the
evidence hook.** A broken booking page offers a booking fix. Accessibility
offers accessibility remediation. The wider pitch -- automation, AI, routing,
dashboards -- belongs after a reply, when there is a conversation to have it in,
and ``reply_drafter`` is where it lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from titan.db.enums import Industry

# ---------------------------------------------------------------- the engines


class Engine(StrEnum):
    """Which of three messages this finding justifies.

    Not a severity score. A score would let a large number of small quality
    problems outrank one broken booking button, which is precisely backwards:
    the button is a person who tried to buy and could not.
    """

    #: Somebody with intent could not finish. The whole message is about that.
    CONVERSION = "conversion"
    #: A real, measured quality defect that is not costing anybody a booking.
    #: Said plainly and without pretending it is an emergency.
    QUALITY = "quality"
    #: No website defect at all -- an operational gap the crawl could measure,
    #: such as a business with no way at all to book outside office hours.
    AUTOMATION = "automation"


#: Defect types that are always a conversion problem, wherever they are found.
_ALWAYS_CONVERSION: frozenset[str] = frozenset(
    {
        "broken_primary_cta",
        "high_friction_contact_form",
        "no_visible_phone_number",
    }
)

#: The one finding that is an operational fact rather than a defect.
#:
#: "There is no booking link or enquiry form anywhere on the site" describes a
#: business that takes its bookings by telephone, during office hours, from
#: whoever is at the desk. Nothing is broken. Writing to them about a broken
#: step would be wrong on the facts, and writing to them about conversion
#: misses what is actually being sold -- which is the system they do not have.
#:
#: This is also the only automation pitch that carries an evidence row. Every
#: other operational gap worth naming (enquiries not routed, trials not
#: followed up) is invisible to a crawler, and a claim about how a business
#: works internally that nothing observed is a claim this system does not make.
_OPERATIONAL: frozenset[str] = frozenset({"no_booking_or_enquiry_path"})

#: Path fragments that mean the visitor had already decided.
#:
#: This is the whole reason ``broken_internal_link`` is not one engine. It is
#: the second-largest finding type in the database and it covers both "your
#: /book page is down" and "a link in your footer points at a dead press
#: release". Sending the first as an emergency is right; sending the second as
#: one is the thing that makes a reader stop reading.
#:
#: The pages a visitor chooses *what* to buy on are here too, not only the ones
#: they buy on. A broken /treatments page at a day spa was being called "a
#: modest issue" -- true of a dead press release and false of the page standing
#: between somebody and a booking. Every trade in the catalogue sells from one
#: of these, so none of them is a footer link.
_MONEY_PATH_WORDS: frozenset[str] = frozenset(
    {
        "appointment",
        "appointments",
        "apply",
        "basket",
        "book",
        "booking",
        "bookings",
        "callout",
        "cart",
        "checkout",
        "consult",
        "consultation",
        "contact",
        "contact-us",
        "enquire",
        "enquiries",
        "enquiry",
        "estimate",
        "free-trial",
        "get-a-quote",
        "inquiry",
        "instruct",
        "join",
        "member",
        "membership",
        "menu",
        "new-patient",
        "new-patients",
        "packages",
        "prices",
        "pricing",
        "quote",
        "referral",
        "register",
        "request",
        "reserve",
        "schedule",
        "service",
        "services",
        "signup",
        "sign-up",
        "treatment",
        "treatments",
        "trial",
        "valuation",
        "viewing",
    }
)


def on_a_money_path(page_url: str | None) -> bool:
    """Whether this URL is a step somebody with intent was taking.

    Matched on whole path segments rather than substrings. ``in`` would read
    "/about-our-book-club" as a booking page and, worse, "/contact" out of
    "/no-contact-order" -- and the resulting message tells a solicitor their
    booking flow is broken, which is both wrong and unrecoverable.
    """
    url = (page_url or "").strip().lower()
    if not url:
        return False
    path = url.split("://", 1)[-1]
    path = path[path.find("/") :] if "/" in path else ""
    path = path.split("?", 1)[0].split("#", 1)[0]
    segments = [seg for seg in path.replace("_", "-").split("/") if seg]
    if not segments:
        return False
    # The final segment often carries a file extension: /book.php, /contact.html
    segments[-1] = segments[-1].rsplit(".", 1)[0]
    return any(seg in _MONEY_PATH_WORDS for seg in segments)


def engine_for(issue_type: str, page_url: str | None = None) -> Engine:
    """Which message this finding gets."""
    if issue_type in _OPERATIONAL:
        return Engine.AUTOMATION
    if issue_type in _ALWAYS_CONVERSION:
        return Engine.CONVERSION
    if issue_type == "broken_internal_link" and on_a_money_path(page_url):
        return Engine.CONVERSION
    return Engine.QUALITY


# ------------------------------------------------------------ the vocabularies


@dataclass(frozen=True, slots=True)
class Vernacular:
    """The words one industry uses about its own customers.

    Every field is a fragment of a sentence rather than a whole one, so the
    composer keeps control of the structure and this file keeps control of the
    language. Fragments are written to slot in without further inflection: what
    is here is what is sent.
    """

    industry: Industry
    #: What the business is called in a subject line. "clinic", "firm", "gym".
    noun: str
    #: The page a visitor with intent is most often on, named the way the owner
    #: names it. Used for the subject line when the defect is on that path.
    money_page: str
    #: Engine A, line 2. Why *this* business in particular should care, written
    #: in the language of its own funnel. A complete sentence.
    conversion_consequence: str
    #: Engine A, line 3. What the sender does about this exact problem.
    conversion_capability: str
    #: Engine B, line 2. Honest about the size of it.
    quality_consequence: str
    #: Engine B, line 3.
    quality_capability: str
    #: Engine C, lines 2 and 3, used only against a measured operational gap.
    automation_consequence: str
    automation_capability: str
    #: What the workflow is called, for an Engine C subject line.
    automation_workflow: str


_LAW_FIRM = Vernacular(
    industry=Industry.LAW_FIRM,
    noun="firm",
    money_page="consultation page",
    conversion_consequence=(
        "Somebody reaching that page is likely already considering making an "
        "enquiry, so it sits very close to the point of instruction."
    ),
    conversion_capability=(
        "I work on website and intake flows like this and can show you exactly "
        "where it is failing and how I would fix it."
    ),
    quality_consequence=(
        "It is a relatively small technical issue, but it affects how some "
        "visitors navigate the site and removes useful context from those pages."
    ),
    quality_capability=(
        "I can put together a short list of the affected areas and the cleanest "
        "way to correct them."
    ),
    automation_consequence=(
        "Enquiries that arrive outside office hours wait until somebody opens "
        "the inbox, and a legal enquiry is rarely repeated once another firm "
        "has answered."
    ),
    automation_capability=(
        "I build intake flows that acknowledge an enquiry immediately, route it "
        "by practice area and flag anything left unanswered."
    ),
    automation_workflow="enquiry intake",
)

_DENTIST = Vernacular(
    industry=Industry.DENTIST,
    noun="practice",
    money_page="booking page",
    conversion_consequence=(
        "That is worth fixing because someone clicking through there has "
        "already moved past browsing treatments and is actively trying to book."
    ),
    conversion_capability=(
        "I build and repair patient-booking flows, so I can send you the exact "
        "issue and the simplest way I would correct it."
    ),
    quality_consequence=(
        "It can make parts of the site harder to use for some patients, and it "
        "takes context away from the pages it affects."
    ),
    quality_capability=(
        "It is something I can map out quickly -- I can send you the affected "
        "elements and what I would change."
    ),
    automation_consequence=(
        "A new patient who decides to book in the evening has to wait for "
        "reception to open, and most of them ring the next practice instead."
    ),
    automation_capability=(
        "I set up booking that confirms the appointment on the spot and reminds "
        "the patient before it."
    ),
    automation_workflow="new-patient booking",
)

_GYM_FITNESS = Vernacular(
    industry=Industry.GYM_FITNESS,
    noun="gym",
    money_page="free-trial page",
    conversion_consequence=(
        "That is a fairly important point in the journey, since anyone clicking "
        "it is already showing intent to try the place or join it."
    ),
    conversion_capability=(
        "I work on membership and trial-conversion flows and can show you where "
        "the break is and how I would repair it."
    ),
    quality_consequence=(
        "It is a small thing, but it makes parts of the site harder to use and "
        "costs those pages some of their context."
    ),
    quality_capability=(
        "I can list the affected pages and the quickest way to put them right."
    ),
    automation_consequence=(
        "A trial enquiry that is not followed up before the first session "
        "rarely turns into a membership, and nothing on the site appears to do "
        "that following up."
    ),
    automation_capability=(
        "I build the sequence that confirms the trial, reminds the person "
        "before it and flags anyone who does not turn up."
    ),
    automation_workflow="trial enquiries",
)

_MED_SPA = Vernacular(
    industry=Industry.MED_SPA,
    noun="clinic",
    money_page="booking page",
    conversion_consequence=(
        "Since that page sits directly between someone choosing a treatment and "
        "actually booking it, it is one of the things I would fix first."
    ),
    conversion_capability=(
        "I build and repair booking flows for treatment businesses and can send "
        "you exactly what I found and how I would approach the fix."
    ),
    quality_consequence=(
        "It is a modest issue, but it affects how some visitors move through "
        "the treatment pages and strips context from them."
    ),
    quality_capability=(
        "I can send over the affected areas and the cleanest way to correct them."
    ),
    automation_consequence=(
        "Someone deciding on a treatment at nine in the evening has no way to "
        "hold a slot, and by the morning the decision has usually cooled."
    ),
    automation_capability=(
        "I set up booking that takes the appointment while the interest is "
        "there and reminds the client before it."
    ),
    automation_workflow="treatment bookings",
)

_REAL_ESTATE = Vernacular(
    industry=Industry.REAL_ESTATE,
    noun="agency",
    money_page="enquiry page",
    conversion_consequence=(
        "Somebody on that page has already found a property they want to ask "
        "about rather than browsing, so the break sits right at the enquiry."
    ),
    conversion_capability=(
        "I build enquiry flows that route new leads to the right agent, and can "
        "send you the exact problem plus the cleanest fix."
    ),
    quality_consequence=(
        "It is a small technical issue, but it affects how some visitors work "
        "through the listings and removes context from those pages."
    ),
    quality_capability=(
        "I can put together the list of affected pages and what I would change on each."
    ),
    automation_consequence=(
        "A property enquiry that lands in a shared inbox waits for whoever "
        "opens it, and a buyer who has enquired on one house has usually "
        "enquired on three."
    ),
    automation_capability=(
        "I build enquiry flows that route new leads to the right agent and make "
        "sure nothing gets missed."
    ),
    automation_workflow="property enquiries",
)

_HVAC = Vernacular(
    industry=Industry.HVAC_HOME_SERVICES,
    noun="business",
    money_page="quote page",
    conversion_consequence=(
        "Anyone on that page is usually trying to get a quote or book a callout "
        "rather than read about the company, so it is the step I would fix "
        "first."
    ),
    conversion_capability=(
        "I work on quote and callout request flows and can show you where it "
        "breaks and how I would fix it."
    ),
    quality_consequence=(
        "It is a small issue, but it makes parts of the site harder to use and "
        "takes context away from those pages."
    ),
    quality_capability=(
        "I can send you the affected pages and the quickest way to correct them."
    ),
    automation_consequence=(
        "A callout request that comes in after hours sits until somebody checks, "
        "and the customer with no heating has usually rung two other numbers by "
        "then."
    ),
    automation_capability=(
        "I build request flows that acknowledge the job straight away and chase "
        "anything that has not been answered."
    ),
    automation_workflow="callout requests",
)

_RESTAURANT = Vernacular(
    industry=Industry.RESTAURANT,
    noun="restaurant",
    money_page="booking page",
    conversion_consequence=(
        "Someone on that page is usually trying to book a table rather than "
        "read the menu, so the break sits right at the reservation."
    ),
    conversion_capability=(
        "I build and repair table-booking flows and can send you the exact "
        "issue and how I would fix it."
    ),
    quality_consequence=(
        "It is a small issue, but it makes parts of the site harder to use and "
        "removes context from those pages."
    ),
    quality_capability=(
        "I can send you the affected pages and the cleanest way to correct each of them."
    ),
    automation_consequence=(
        "A booking request made after service has finished waits until somebody "
        "opens the inbox, by which point the table has often gone."
    ),
    automation_capability=(
        "I set up booking that confirms the table straight away and reminds the "
        "party beforehand."
    ),
    automation_workflow="table bookings",
)

_VETERINARY = Vernacular(
    industry=Industry.VETERINARY,
    noun="practice",
    money_page="appointment page",
    conversion_consequence=(
        "An owner on that page is usually worried about an animal rather than "
        "browsing, and they will ring the next practice on the list instead."
    ),
    conversion_capability=(
        "I build and repair appointment and registration flows, and can send "
        "you the exact issue and how I would fix it."
    ),
    quality_consequence=(
        "It is a small technical issue, but it affects how some visitors move "
        "around the site and removes context from those pages."
    ),
    quality_capability=(
        "I can send you the affected pages and the cleanest way to correct them."
    ),
    automation_consequence=(
        "An owner who decides at nine in the evening that the dog needs seeing "
        "has to wait for the phone line to open, and by then somebody else has "
        "usually taken the slot."
    ),
    automation_capability=(
        "I set up appointment requests that reach the practice out of hours and "
        "confirm back to the owner straight away."
    ),
    automation_workflow="appointment requests",
)

_ACCOUNTANT = Vernacular(
    industry=Industry.ACCOUNTANT,
    noun="firm",
    money_page="enquiry page",
    conversion_consequence=(
        "Accountancy enquiries cluster around filing deadlines and go to "
        "whoever answers first, so anything in the way there costs a client "
        "rather than a visit."
    ),
    conversion_capability=(
        "I work on enquiry and onboarding flows like this and can show you "
        "exactly where it is failing and how I would fix it."
    ),
    quality_consequence=(
        "It is a relatively small technical issue, but it affects how some "
        "visitors navigate the site and removes useful context from those pages."
    ),
    quality_capability=(
        "I can put together a short list of the affected areas and the cleanest "
        "way to correct them."
    ),
    automation_consequence=(
        "An enquiry that arrives in the evening waits until somebody opens the "
        "inbox, and a business shopping for an accountant has usually written "
        "to three."
    ),
    automation_capability=(
        "I build intake flows that acknowledge an enquiry immediately and flag "
        "anything left unanswered."
    ),
    automation_workflow="enquiry intake",
)

_OPTICIAN = Vernacular(
    industry=Industry.OPTICIAN,
    noun="practice",
    money_page="booking page",
    conversion_consequence=(
        "Anyone on that page is trying to book an eye test rather than read "
        "about frames, so it is the step I would fix first."
    ),
    conversion_capability=(
        "I build and repair appointment booking flows, so I can send you the "
        "exact issue and the simplest way I would correct it."
    ),
    quality_consequence=(
        "It is a small technical issue, but it affects how some visitors use "
        "the site and takes context away from those pages."
    ),
    quality_capability=(
        "I can send you the affected pages and the quickest way to correct them."
    ),
    automation_consequence=(
        "Somebody who realises in the evening that their test is overdue has to "
        "remember again in the morning, and most of them do not."
    ),
    automation_capability=(
        "I set up booking that takes the appointment there and then, and a "
        "recall that goes out when the next test is due."
    ),
    automation_workflow="eye-test booking",
)

_PHYSIOTHERAPY = Vernacular(
    industry=Industry.PHYSIOTHERAPY,
    noun="clinic",
    money_page="booking page",
    conversion_consequence=(
        "Somebody on that page is usually in pain and looking for the first "
        "clinic that will see them, so the break sits right at the decision."
    ),
    conversion_capability=(
        "I build and repair appointment booking flows and can send you exactly "
        "what I found and how I would approach the fix."
    ),
    quality_consequence=(
        "It is a small technical issue, but it affects how some visitors move "
        "through the treatment pages and strips context from them."
    ),
    quality_capability=(
        "I can send over the affected areas and the cleanest way to correct them."
    ),
    automation_consequence=(
        "A patient who does not book the next session before leaving often does "
        "not book it at all, and nothing on the site appears to prompt them."
    ),
    automation_capability=(
        "I set up booking that takes the next appointment on the spot and "
        "reminds the patient before it."
    ),
    automation_workflow="appointment booking",
)

_SALON_BARBER = Vernacular(
    industry=Industry.SALON_BARBER,
    noun="salon",
    money_page="booking page",
    conversion_consequence=(
        "Anyone clicking there has already chosen a service and a rough time, "
        "so the break costs an appointment rather than a browse."
    ),
    conversion_capability=(
        "I build and repair booking flows for appointment businesses and can "
        "send you the exact issue and how I would fix it."
    ),
    quality_consequence=(
        "It is a small thing, but it makes parts of the site harder to use and "
        "costs those pages some of their context."
    ),
    quality_capability=(
        "I can list the affected pages and the quickest way to put them right."
    ),
    automation_consequence=(
        "Somebody deciding on a Sunday evening cannot book until the salon "
        "opens, and a chair empty on Tuesday afternoon is not revenue you can "
        "get back."
    ),
    automation_capability=(
        "I set up booking that fills the quiet hours and reminds people of the "
        "appointment they made."
    ),
    automation_workflow="appointment booking",
)

_CLINIC = Vernacular(
    industry=Industry.CLINIC,
    noun="clinic",
    money_page="appointments page",
    conversion_consequence=(
        "Anyone who got that far had decided to book, so the break costs an "
        "appointment rather than a browse."
    ),
    conversion_capability=(
        "I build and repair booking flows for appointment businesses and can "
        "send you the exact issue and how I would fix it."
    ),
    quality_consequence=(
        "It is a small thing, but it makes parts of the site harder to use and "
        "costs those pages some of their context."
    ),
    quality_capability=(
        "I can list the affected pages and the quickest way to put them right."
    ),
    automation_consequence=(
        "Somebody deciding at nine in the evening has to wait for the desk to "
        "open, and the ones who do not wait ring whoever answers first."
    ),
    automation_capability=(
        "I set up booking and an out-of-hours responder that captures the "
        "enquiry instead of losing it to the clock."
    ),
    automation_workflow="appointment booking",
)

_PRIVATE_HOSPITAL = Vernacular(
    industry=Industry.PRIVATE_HOSPITAL,
    noun="hospital",
    money_page="appointments page",
    conversion_consequence=(
        "Enquiries here come from patients, referring GPs and consultants "
        "alike, so a break in the path holds up more than one kind of booking."
    ),
    conversion_capability=(
        "I build and repair enquiry and booking paths and can send you the "
        "exact issue and how I would fix it."
    ),
    quality_consequence=(
        "It is a small thing, but it makes parts of the site harder to use and "
        "costs those pages some of their context."
    ),
    quality_capability=(
        "I can list the affected pages and the quickest way to put them right."
    ),
    automation_consequence=(
        "Every enquiry arrives at the same switchboard and is sorted by hand, "
        "so the ones that come in after hours wait until somebody is back."
    ),
    automation_capability=(
        "I set up routing that sends each enquiry to the right desk and covers "
        "the hours the switchboard does not."
    ),
    automation_workflow="enquiry routing",
)

_INSURANCE = Vernacular(
    industry=Industry.INSURANCE,
    noun="brokerage",
    money_page="quote page",
    conversion_consequence=(
        "Anyone who reached that point was asking for a quote, so the break "
        "costs an enquiry that had already chosen you."
    ),
    conversion_capability=(
        "I build and repair quote and enquiry forms and can send you the exact "
        "issue and how I would fix it."
    ),
    quality_consequence=(
        "It is a small thing, but it makes parts of the site harder to use and "
        "costs those pages some of their context."
    ),
    quality_capability=(
        "I can list the affected pages and the quickest way to put them right."
    ),
    automation_consequence=(
        "A quote asked for on a Saturday sits until Monday, and a renewal "
        "nobody chases is a policy that quietly moves elsewhere."
    ),
    automation_capability=(
        "I set up enquiry capture that works outside office hours and renewal "
        "follow-up that runs without anybody remembering it."
    ),
    automation_workflow="enquiry capture and renewal follow-up",
)


#: The fallback, and deliberately not a weaker version of the others.
#:
#: It is what a message uses when the business type is genuinely unknown, and an
#: unknown business still deserves a sentence that is true about it. What it
#: gives up is the industry noun, not the specificity: the finding, the page and
#: the fix are as exact here as anywhere else.
_GENERAL = Vernacular(
    industry=Industry.GENERAL,
    noun="business",
    money_page="contact page",
    conversion_consequence=(
        "Because it affects getting in touch, it is likely to be interrupting "
        "visitors at a fairly high-intent point rather than while they browse."
    ),
    conversion_capability=(
        "I work on website and customer-flow problems like this and can send "
        "you a short breakdown of what I found and what I would change."
    ),
    quality_consequence=(
        "It is a small technical issue, but it affects how some visitors use "
        "the site and removes useful context from those pages."
    ),
    quality_capability=(
        "I can put together a short list of the affected areas and the cleanest "
        "way to correct them."
    ),
    automation_consequence=(
        "An enquiry that arrives outside working hours waits until somebody "
        "opens the inbox, and most people who enquire have enquired elsewhere "
        "too."
    ),
    automation_capability=(
        "I build enquiry flows that acknowledge a message straight away and "
        "chase anything that goes unanswered."
    ),
    automation_workflow="enquiries",
)

VERNACULARS: dict[Industry, Vernacular] = {
    Industry.LAW_FIRM: _LAW_FIRM,
    Industry.DENTIST: _DENTIST,
    Industry.GYM_FITNESS: _GYM_FITNESS,
    Industry.MED_SPA: _MED_SPA,
    Industry.REAL_ESTATE: _REAL_ESTATE,
    Industry.HVAC_HOME_SERVICES: _HVAC,
    Industry.RESTAURANT: _RESTAURANT,
    Industry.GENERAL: _GENERAL,
    Industry.VETERINARY: _VETERINARY,
    Industry.ACCOUNTANT: _ACCOUNTANT,
    Industry.OPTICIAN: _OPTICIAN,
    Industry.PHYSIOTHERAPY: _PHYSIOTHERAPY,
    Industry.SALON_BARBER: _SALON_BARBER,
    Industry.CLINIC: _CLINIC,
    Industry.PRIVATE_HOSPITAL: _PRIVATE_HOSPITAL,
    Industry.INSURANCE: _INSURANCE,
}


#: Two quality families whose consequence does not vary by industry, and whose
#: industry sentence was actively wrong.
#:
#: The generic quality line opens "It is a small technical issue", which is true
#: of a missing alt attribute and false of a homepage that takes twenty-four
#: seconds to appear. Sending "it is a small issue" about that is the kind of
#: mismatch that tells a reader the sentence was not written about them.
#:
#: Neither claims a number. "You are losing 40% of mobile visitors" is a
#: fabricated metric; that a slow page is waited through before anything is seen
#: is a fact about the web, not a measurement of their business.
_FAMILY_CONSEQUENCES: dict[str, str] = {
    "speed": (
        "That is slow enough to matter on a phone, where the wait happens "
        "before anyone has seen what you offer."
    ),
    "search": (
        "Search engines then write their own summary of the page, and it is "
        "rarely the one you would have chosen."
    ),
}


#: And the matching fix sentence, for the same reason.
#:
#: "I can send you the affected pages and the quickest way to correct them" is
#: right about alt text and wrong about a single slow home page -- there is one
#: page and the fix is not a list. A capability sentence that does not fit the
#: observation above it is the tell that neither was written for this reader.
_FAMILY_CAPABILITIES: dict[str, str] = {
    "speed": (
        "I can show you what is holding that page back and the shortest route "
        "to bringing it down."
    ),
    "search": (
        "I can send you the summary I would write for it, along with any other "
        "pages in the same state."
    ),
}


def family_consequence(family: str) -> str | None:
    """The consequence for a quality family, when the industry one understates."""
    return _FAMILY_CONSEQUENCES.get(family)


def family_capability(family: str) -> str | None:
    """The fix sentence for a quality family, when the industry one misfits."""
    return _FAMILY_CAPABILITIES.get(family)


def vernacular_for(industry: Industry | str | None) -> Vernacular:
    """Always returns one. An unknown industry gets the general voice."""
    if industry is None:
        return _GENERAL
    try:
        key = Industry(industry)
    except ValueError:
        return _GENERAL
    return VERNACULARS.get(key, _GENERAL)


# -------------------------------------------------------------------- the asks


#: One ask, and never a meeting.
#:
#: A stranger asking for ten minutes is asking for something before giving
#: anything, and "Worth a short call next week?" was the most common closing
#: line this system had. Every ask here offers the recipient something instead,
#: costs them a single word to answer, and is worth having whether or not they
#: ever reply. The meeting request belongs after a positive reply, and
#: ``reply_drafter`` is where it lives.
_CONVERSION_ASKS: tuple[str, ...] = (
    "Want me to send you the exact fix?",
    "Want me to send the short breakdown?",
    "Want me to show you how I would approach it?",
    "Would a quick screenshot of the issue help?",
)

_QUALITY_ASKS: tuple[str, ...] = (
    "Useful if I send the affected pages over?",
    "Should I send the notes?",
    "I can send a three-point breakdown if that helps -- want it?",
    "Want me to send over the list?",
)

_AUTOMATION_ASKS: tuple[str, ...] = (
    "Worth sending you the workflow I would use?",
    "Want me to sketch how I would set it up?",
    "Shall I send a simple diagram of it?",
    "Want me to write up how that would work?",
)

_ASKS: dict[Engine, tuple[str, ...]] = {
    Engine.CONVERSION: _CONVERSION_ASKS,
    Engine.QUALITY: _QUALITY_ASKS,
    Engine.AUTOMATION: _AUTOMATION_ASKS,
}


def ask_for(engine: Engine, index: int) -> str:
    pool = _ASKS[engine]
    return pool[index % len(pool)]


def consequence_for(vernacular: Vernacular, engine: Engine) -> str:
    if engine is Engine.CONVERSION:
        return vernacular.conversion_consequence
    if engine is Engine.AUTOMATION:
        return vernacular.automation_consequence
    return vernacular.quality_consequence


def capability_for(vernacular: Vernacular, engine: Engine) -> str:
    if engine is Engine.CONVERSION:
        return vernacular.conversion_capability
    if engine is Engine.AUTOMATION:
        return vernacular.automation_capability
    return vernacular.quality_capability


__all__ = [
    "VERNACULARS",
    "Engine",
    "Vernacular",
    "ask_for",
    "capability_for",
    "consequence_for",
    "engine_for",
    "family_capability",
    "family_consequence",
    "on_a_money_path",
    "vernacular_for",
]
