"""Writing the message.

What replaced a single hardcoded template. Every lead used to receive the same
four sentences with a domain substituted in, which is two problems wearing one
coat: it reads as a mail merge, and a hundred near-identical bodies leaving one
domain in a day is the shape a filter is built to catch.

**Persuasion here is specificity, not enthusiasm.** The validator already
rejects the levers cold email usually reaches for -- manufactured deadlines,
fear framing, flattery, fabricated metrics, "I hope this finds you well". That
is not a constraint working against good copy; it is most of the definition of
it. What is left to be good at is naming precisely what is wrong, on which page,
and why it costs them something. A stranger who reads one sentence that could
only have been written about their business will read the second one.

Four lines, in this order, and never more:

1. an observation, drawn from a finding that carries evidence rows;
2. what it means commercially, in the recipient's own vocabulary;
3. one sentence about fixing *this* problem;
4. an ask small enough to answer with a word.

Two things decide the wording, and both live in :mod:`titan.intelligence.
vernacular`. The **engine** -- is this a conversion problem, a quality problem
or an operational gap -- sets the shape and the size of the claim. The
**vernacular** supplies every sentence that touches the recipient's business,
because a solicitor has enquiries and consultations, a gym has trials and
members, and writing "conversion path" at either of them is writing at them.

So the variation this module produces is **structural, never factual**. Four
registers exist for each line; the *evidence* they are built from is identical
in each, and every factual sentence is emitted together with its claim-map entry
so the two cannot drift apart. A register is chosen by hashing the lead id,
which means the same lead always gets the same message -- an activity retry, a
workflow replay, or a second look a week later all produce byte-identical
output, and the outbox dedupe key keeps meaning what it says.

Deliberately still deterministic rather than model-generated. The model gateway
is wired and available, and the moment its output can be held to the same
claim-map contract this becomes its post-processor rather than its replacement.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from titan.db.enums import Industry
from titan.intelligence.case_studies import CaseStudy
from titan.intelligence.message_validator import (
    PITCH_MAX_WORDS,
    PITCH_MIN_WORDS,
    sentences,
)
from titan.intelligence.references import Reference, references_for
from titan.intelligence.vernacular import (
    Engine,
    Vernacular,
    capability_for,
    consequence_for,
    engine_for,
    family_capability,
    family_consequence,
    vernacular_for,
)

#: How the greeting is written when nobody's name was published. "Hi there" is
#: the least-bad of a bad set: "Dear Sir/Madam" is a form letter, "Hey!" is
#: presumptuous, and inventing a name is invariant 6 territory.
FALLBACK_GREETING = "Hi there"

#: The band the message has to land in, before the signature.
#:
#: Imported from the validator rather than declared here, and re-exported below
#: so callers reading ``composer.PITCH_MAX_WORDS`` keep working. Both modules
#: used to declare it with a comment asking a human to keep them in step; the
#: reasoning, and the reason the duplication is gone, is in
#: ``titan.intelligence.message_validator``.


class FindingLike(Protocol):
    """The fields the composer reads. Kept narrow so tests need no ORM row."""

    issue_type: str
    title: str
    page_url: str | None
    observed_value: str | None


@dataclass(frozen=True, slots=True)
class ComposerContext:
    org_domain: str
    finding: FindingLike
    evidence_ids: list[str]
    owner_name: str
    portfolio_url: str
    mailing_address: str
    #: Retained on the contract though the body no longer renders it.
    #:
    #: The visible unsubscribe link was replaced by ``OPT_OUT_LINE``. The
    #: machine-readable one-click target is unaffected and is built elsewhere,
    #: in ``activities/pipeline.py``, from the *sender identity's* own
    #: template -- so the ``List-Unsubscribe`` header does not depend on this
    #: field and never did. Kept because callers pass it and because a
    #: composer that is handed the unsubscribe target is one edit away from
    #: being able to render it again if the visible form is ever wanted back.
    unsubscribe_url: str
    #: Which offer the *headline* finding justified.
    #:
    #: Required, and never written into the prose. It used to be the offer's
    #: prose blurb, interpolated into a sentence about what the sender does --
    #: which is how a message about a broken navigation link ended up pitching
    #: "follow-up for enquiries that do not book immediately". The offer was
    #: real; it was justified by some *other* finding on the same site, and the
    #: reader saw a pitch that had nothing to do with the paragraph above it.
    #:
    #: The sentence about the fix now comes from the vernacular and is about the
    #: finding that was actually cited. This field stays required so a caller
    #: still cannot compose without having found an offer the evidence supports,
    #: and it is stamped into ``template_key`` so reply rates are attributable
    #: to an engine and an offer rather than to folklore.
    offer_key: str
    #: The business's own name, for the subject line. Falls back to the domain,
    #: which is never wrong, only less warm.
    business_name: str | None = None
    #: Decides the vocabulary. ``None`` is honest and gets the general voice --
    #: it does not get a weaker message, only one without an industry noun in it.
    industry: Industry | str | None = None
    #: Published first name, when one was actually found. Never inferred from an
    #: email local part: "sam@" might be Samantha, Samuel, or the sales team.
    contact_first_name: str | None = None
    #: What the register hash is taken over. The lead id, so a retry composes
    #: the same message.
    variant_seed: str = ""
    #: Zero for the first message; above that the opener acknowledges the
    #: earlier one instead of opening cold.
    step_number: int = 0
    #: Set only when the campaign manager has promoted a register on measured
    #: evidence, in which case every lead gets it rather than the one its id
    #: happened to select.
    promoted_variant: int | None = None
    #: A one-page summary of the approach, cited in the references block.
    #: None renders no line -- see ``Settings.one_pager_url`` for why this is a
    #: link and not an attachment.
    one_pager_url: str | None = None
    #: A real previous job to cite instead of the generic credential sentence.
    #:
    #: Selected by the caller from the operator-maintained registry, because
    #: the composer is a pure function and reading a file is not its job. None
    #: is the shipped state and keeps the sentence that was always there --
    #: see ``titan.intelligence.case_studies`` for why the fallback is a
    #: weaker true sentence rather than a stronger invented one.
    case_study: CaseStudy | None = None


@dataclass(frozen=True, slots=True)
class ComposedMessage:
    subject: str
    #: The plain-text part. Still the authoritative body: it is what the
    #: validator reads, what the claim map is checked against, and what a
    #: text-only client renders. Every link in it appears as a phrase followed
    #: by its bare URL once.
    body: str
    #: The HTML part, where the same links are anchors over their phrase. Empty
    #: only if something upstream declined to build it -- a message with no HTML
    #: part still sends, as text.
    body_html: str = ""
    #: One entry per factual sentence. Built beside the sentence, never after.
    claim_map: list[dict[str, Any]] = field(default_factory=list)
    #: Which registers were used. Recorded so a reply-rate difference between
    #: variants is attributable rather than folklore.
    variant: str = ""
    #: Which engine wrote it. The three are different products in the same
    #: envelope and their reply rates have no business being averaged together.
    engine: Engine = Engine.QUALITY
    #: ``engine:offer_key``, stored on the draft row. What makes "conversion
    #: messages reply at four times the rate of quality ones" a query rather
    #: than an opinion.
    template_key: str = ""
    #: Words before the signature. Carried out so the caller can assert the
    #: band rather than counting the body again with a different rule.
    #:
    #: The references block is not counted here, and does not need to be: it
    #: renders below the signature, which is exactly where the validator's own
    #: ``pitch_of`` stops counting. The two measurements agree by construction
    #: rather than by coincidence.
    pitch_words: int = 0
    #: Every URL in the references block, in the order it appears. Recorded so
    #: a link checker and a test can assert what a recipient was actually
    #: pointed at without re-parsing the body.
    reference_urls: list[str] = field(default_factory=list)
    #: Which case study was shown, if any. Stamped so reply rates are
    #: attributable to the previous job cited rather than to the offer alone.
    case_study_reference: str = ""


# --------------------------------------------------------------------------
# Registers
#
# Each entry is a *phrasing* of the same evidenced fact. Swapping between them
# changes the sentence and not the claim, which is what keeps the claim map
# honest while stopping every recipient getting the same paragraph.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Anchor:
    """One link, carried as text and href separately until render time.

    The point of the whole exercise: a reader sees "your booking page", not
    ``https://example.com/book?utm=...``. Plain text cannot hold an anchor, so
    the two renderings differ and the pair has to survive until each one is
    built. A single pre-formatted string could not serve both.
    """

    text: str
    href: str

    def html(self) -> str:
        return f'<a href="{_attr(self.href)}">{_esc(self.text)}</a>'

    def plain(self) -> str:
        """Text fallback: the phrase, then the bare URL once, in brackets.

        Not the phrase alone -- a text-only reader would be told to look at
        something with no way to reach it.
        """
        return f"{self.text} ({self.href})"


def _esc(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(value: str) -> str:
    """Escape for an attribute. Quotes matter here and not in text."""
    return _esc(value).replace('"', "&quot;")


#: What is mechanically going on, per issue type, in plain words.
#:
#: The observation says *what* was found; this says *why it happens*, which
#: is the difference between a reader who believes the finding and one who
#: assumes a tool spat out a warning. Every line describes the defect and
#: nothing else -- none asserts anything about the recipient's revenue,
#: traffic or customers, because Titan measures pages and nothing else.
_PROBLEM_DETAIL: dict[str, str] = {
    "no_website_listed": (
        "Everyone who finds you on Google and wants to know more -- your "
        "prices, whether you take their insurance, what the place looks "
        "like -- has nowhere to go and nothing to read. The listing is "
        "doing the whole job of a website, and it can only show a name, a "
        "map pin and whatever other people have said about you."
    ),
    "no_opening_hours_listed": (
        "Where Google would normally say open or closed, your listing says "
        "nothing. Somebody deciding at eight in the evening whether to ring "
        "in the morning has no answer, and the results either side of yours "
        "do have one."
    ),
    "listing_has_almost_no_photos": (
        "The listing is the first thing most people see of you and there is "
        "almost nothing on it to look at. Next to competitors showing their "
        "rooms and their staff, an empty gallery reads as a business nobody "
        "has looked after for a while -- which is usually not true, and is "
        "the impression it gives anyway."
    ),
    "reviews_go_unanswered": (
        "People read the replies as much as the reviews, because the reply "
        "is the only part of it you write. An unanswered complaint is "
        "currently the last thing a prospective customer reads about you, "
        "and a happy review that gets no acknowledgement reads as one "
        "nobody noticed."
    ),
    "listing_has_no_description": (
        "Google fills the gap with whatever it can infer from your category "
        "and your reviews, so the one paragraph describing your business is "
        "the only copy about you that you did not write."
    ),
    "no_conversational_capability": (
        "An enquiry that arrives outside opening hours waits until somebody "
        "is back at the desk. Most of them do wait. The ones that do not are "
        "the people who contacted three businesses at once and went with "
        "whoever answered first, and you never see those at all."
    ),
    "no_self_service_booking": (
        "Every booking has to pass through somebody answering the phone, so "
        "the number you can take is capped by the hours that desk is "
        "staffed. Evenings and weekends convert at whatever the answerphone "
        "converts at, which is usually close to nothing."
    ),
    "no_follow_up_automation": (
        "Following up depends on somebody having the time that week, so it "
        "is the first thing dropped when the business is busy -- which is "
        "exactly when there is most to follow up on."
    ),
    "no_review_automation": (
        "Reviews arrive only from people motivated enough to leave one "
        "unprompted. That is a different population from your customers, and "
        "it skews towards the ones who had something to complain about."
    ),
    "broken_primary_cta": (
        "The button itself is fine -- it is the address behind it that no "
        "longer exists, so the server answers with a not-found page instead "
        "of the form. That usually happens after a page is renamed or moved "
        "and the old link is left pointing at where it used to be."
    ),
    "broken_internal_link": (
        "The link is still on the page but the address behind it no longer "
        "resolves, so the server returns an error page instead. It is the "
        "normal after-effect of a page being renamed, moved or removed "
        "while the things pointing at it stayed as they were."
    ),
    "high_friction_contact_form": (
        "Every extra field is another decision before somebody can finish, "
        "and forms lose people steadily as the list grows. Most of what is "
        "being asked up front is information you could just as easily "
        "collect in the first reply, once they are already talking to you."
    ),
    "no_visible_phone_number": (
        "There is no number anywhere a visitor can see without hunting for "
        "it, so somebody who would rather call than type has nothing to act "
        "on. On a phone that matters more than on a desktop, because "
        "tapping a number is the fastest thing they can possibly do."
    ),
    "no_booking_or_enquiry_path": (
        "There is no booking link and no enquiry form anywhere on the pages "
        "I went through, so a visitor who has decided to get in touch has "
        "no obvious next step. Whatever they do next, they have to work out "
        "for themselves."
    ),
    "slow_largest_contentful_paint": (
        "The main content is arriving well after the point most people stop "
        "waiting. It is usually a handful of oversized images and scripts "
        "that load before anything visible does, so the page sits blank "
        "while they finish."
    ),
    "images_missing_alt_text": (
        "Those images carry no text description, so a screen reader "
        "announces nothing at all for them and a search engine cannot tell "
        "what they show. Anyone relying on either is missing that part of "
        "the page completely."
    ),
    "serious_accessibility_violations": (
        "These are the failures that stop somebody using the page rather "
        "than merely making it awkward -- text without enough contrast to "
        "read, or controls a keyboard cannot reach. They are also the "
        "category that carries the most legal weight."
    ),
    "javascript_console_errors": (
        "Scripts are failing while the page loads. Whatever they were meant "
        "to do -- a form, a menu, a booking widget -- stops at that point, "
        "and it is silent: the visitor sees a page that looks finished and "
        "simply does not respond."
    ),
    "failed_network_requests": (
        "Files the page asks for are not coming back. Depending on what "
        "they were, that shows up as missing images, unstyled sections, or "
        "a feature that quietly does nothing -- and none of it announces "
        "itself as an error to whoever is looking."
    ),
    "missing_meta_description": (
        "The page has no description tag, so nothing is telling a search "
        "engine what to put under your name in the results. It falls back "
        "to whichever text it happens to find near the top of the page, "
        "which is rarely the sentence you would have picked."
    ),
    "no_structured_data": (
        "There is no structured data on the page, which is the "
        "machine-readable version of your opening hours, location and "
        "services. Without it, search engines and map listings have to "
        "guess at details you could be stating outright."
    ),
    "missing_mobile_viewport": (
        "The page has no instruction telling a phone how to size itself, so "
        "the browser falls back to rendering it at desktop width and "
        "shrinking the lot. Everything is technically there; it is just too "
        "small to read without pinching."
    ),
    "missing_security_headers": (
        "Standard response headers that tell a browser how to protect the "
        "page are absent. They are a one-line-each server setting, and "
        "their absence is one of the first things an automated scanner "
        "reports."
    ),
}


#: What fixing it actually involves. Scoped and concrete -- "I would do X",
#: not "I can help with that". A reader who cannot picture the work has no
#: way to judge whether twenty minutes of their time is worth spending.
_SOLUTION_DETAIL: dict[str, str] = {
    "no_website_listed": (
        "I would build a small site that answers the questions the phone "
        "currently answers -- what you do, what it costs, when you are open, "
        "how to book -- and put an assistant on it that handles the rest. "
        "Then point the Google listing at it, so the people already finding "
        "you have somewhere to land."
    ),
    "no_opening_hours_listed": (
        "The hours themselves take five minutes to add. The part worth doing "
        "properly is what happens outside them: somewhere to book or ask a "
        "question at eight in the evening, so the answer is not simply that "
        "you are shut."
    ),
    "listing_has_almost_no_photos": (
        "A dozen photographs of the place, the people and the work, and "
        "something that keeps adding to them rather than relying on somebody "
        "remembering. The listing is the shop window for everyone who has "
        "not been yet."
    ),
    "reviews_go_unanswered": (
        "I would set up review requests that go out after a visit without "
        "anybody remembering, and drafted replies you approve rather than "
        "write -- so answering becomes a minute a week instead of a job "
        "nobody owns."
    ),
    "listing_has_no_description": (
        "A written description that says what you actually do and who for, "
        "and a site it can point at, so the first paragraph anybody reads "
        "about you is one you chose."
    ),
    "no_conversational_capability": (
        "An assistant on the site that answers the questions you get asked "
        "most, and captures the enquiry with a name and a number when it "
        "cannot. It does not need to be clever -- it needs to be there at "
        "nine in the evening."
    ),
    "no_self_service_booking": (
        "Self-service booking on the site, taking from your real "
        "availability so nothing is double-booked, with the confirmations "
        "and reminders going out on their own."
    ),
    "no_follow_up_automation": (
        "A follow-up sequence that runs after an enquiry or a visit without "
        "anybody starting it -- a message the next day, another the "
        "following week, stopping the moment they reply."
    ),
    "no_review_automation": (
        "A request that goes out automatically a day or two after a visit, "
        "to everyone rather than to whoever somebody remembered to ask."
    ),
    "broken_primary_cta": (
        "The repair is to point the button at the page that exists now and "
        "put a redirect on the old address so anything still linking to it "
        "keeps working. Then I would walk the rest of the site for the same "
        "pattern, because a rename rarely breaks only one link."
    ),
    "broken_internal_link": (
        "I would repoint the link at the page that exists now and add a "
        "redirect from the old address so nothing else pointing there "
        "breaks. Then a sweep of the rest of the site for the same pattern, "
        "since a rename rarely leaves only one broken link behind."
    ),
    "high_friction_contact_form": (
        "I would cut the form to the few things you genuinely need in order "
        "to reply -- usually a name, a way to reach them, and what it is "
        "about -- and collect the rest in the conversation that follows. "
        "Everything removed is one fewer reason to abandon it."
    ),
    "no_visible_phone_number": (
        "The fix is to put a tappable number in the header on every page, "
        "and again wherever somebody is likely to decide, so it is never "
        "more than a glance away and never needs copying out by hand."
    ),
    "no_booking_or_enquiry_path": (
        "I would add one clear route to get in touch and repeat it wherever "
        "somebody is likely to have made up their mind, so there is always "
        "a next step in front of them rather than one they have to go "
        "looking for."
    ),
    "slow_largest_contentful_paint": (
        "Most of this comes back with compressed and correctly sized "
        "images, and by letting the scripts that are not needed for the "
        "first view load after it rather than before. That is usually the "
        "bulk of the wait, without redesigning anything."
    ),
    "images_missing_alt_text": (
        "It is a short piece of work: writing a real description for each "
        "image that carries meaning, and marking the purely decorative ones "
        "so screen readers skip them properly instead of reading out a "
        "filename."
    ),
    "serious_accessibility_violations": (
        "I would go through each one, correct the contrast and the keyboard "
        "paths, and re-test against the same standard, so you end up with "
        "something that shows the page passes rather than an assurance that "
        "it was looked at."
    ),
    "javascript_console_errors": (
        "The work is to trace each error to the script that raised it and "
        "fix or remove it, then re-check the pages that depend on those "
        "scripts to confirm the features they drive are actually working "
        "again."
    ),
    "failed_network_requests": (
        "I would track down what each missing file was for, restore or "
        "replace it, and remove the requests for things that no longer need "
        "to exist -- which usually makes the page faster as well as whole."
    ),
    "missing_meta_description": (
        "I would write a description for the pages that matter, so the "
        "sentence under your name in the search results is one you chose "
        "rather than whatever text happened to be scraped first."
    ),
    "no_structured_data": (
        "I would add the markup that states your hours, location, services "
        "and reviews in the form search engines read directly, which is "
        "what lets those details show in the listing itself."
    ),
    "missing_mobile_viewport": (
        "This one is a single line in the page head, then checking the "
        "layout actually holds at phone width once the browser stops "
        "shrinking it -- usually a small amount of tidying rather than a "
        "rebuild."
    ),
    "missing_security_headers": (
        "Adding them is a server configuration change rather than a code "
        "one, and I would set them so they protect the page without "
        "breaking anything already embedded in it."
    ),
}


#: What the business gets back once the repair lands.
#:
#: The message already said what is broken and what it costs. This is the other
#: half of that sentence, and without it the reader is left holding a problem
#: and a stranger -- which is a message about their failure rather than about
#: their opportunity. People act on the second one.
#:
#: The discipline is the same as everywhere else in this file and it is worth
#: restating because this is the paragraph most likely to attract a number:
#: **no quantities, no percentages, no revenue.** "This would win you 30% more
#: bookings" is unknowable, unsourceable, and precisely the fabricated claim
#: the validator refuses. Each of these instead states the mechanical
#: consequence of the fix -- the thing that starts working that is not working
#: now -- which is entailed by the finding and therefore carries the same
#: evidence. It is a conditional about their site, so it is a claim, so it goes
#: in the claim map beside the finding that justifies it.
#: What the sender does about this exact absence, overriding the industry line.
#:
#: `automation_capability` is one sentence per industry, written when the only
#: operational finding was `no_booking_or_enquiry_path`. With nine of them it
#: mismatches: a message that opened on a missing chat assistant closed by
#: talking about booking confirmations -- both true absences on that business,
#: and the wrong pair. The paragraph answers a different question from the one
#: the message asked, which a careful reader notices and a careless one feels.
#:
#: Same shape as `family_capability` for QUALITY: an override when there is a
#: better sentence, the industry line when there is not.
_CAPABILITY_BY_ISSUE: dict[str, str] = {
    "no_conversational_capability": (
        "I build assistants that sit on the site, answer what gets asked most "
        "and take the enquiry when they cannot."
    ),
    "no_self_service_booking": (
        "I build booking that works off your real availability, confirms on "
        "the spot and sends the reminders itself."
    ),
    "no_follow_up_automation": (
        "I build follow-up sequences that run on their own after an enquiry "
        "and stop the moment somebody replies."
    ),
    "no_review_automation": (
        "I set up review requests that go out automatically after a visit, to "
        "everyone rather than to whoever somebody remembered to ask."
    ),
    "no_website_listed": (
        "I build small sites for businesses that have been running on a phone "
        "number and a Google listing, and put something on them that answers "
        "out of hours."
    ),
    "no_opening_hours_listed": (
        "I look after the Google listing alongside the site, so the hours and "
        "the booking link stay right without anybody maintaining them."
    ),
    "listing_has_almost_no_photos": (
        "I keep listings current -- photographs, hours, description -- as part "
        "of the same work as the site."
    ),
    "reviews_go_unanswered": (
        "I set up review requests that go out on their own and draft the "
        "replies for you to approve rather than write."
    ),
    "listing_has_no_description": (
        "I write the listing copy and build the site it points at, so the "
        "first thing anybody reads about you is yours."
    ),
}

_UPSIDE_DETAIL: dict[str, str] = {
    "no_website_listed": (
        "The people finding you on Google already are the ones this reaches "
        "first -- they are searching for what you do, in your town, and "
        "currently arriving at a listing that cannot answer them. Nothing "
        "about your marketing has to change for that traffic to start "
        "landing somewhere."
    ),
    "no_opening_hours_listed": (
        "Google starts showing you as open when you are, which is what turns "
        "a listing from a map pin into a result somebody acts on."
    ),
    "listing_has_almost_no_photos": (
        "Listings with photographs get looked at for longer and clicked "
        "through more often, and the comparison being made is with the two "
        "results either side of yours rather than with some ideal."
    ),
    "reviews_go_unanswered": (
        "Replies are the part of your reputation you control. A complaint "
        "with a straight answer under it reads completely differently from "
        "the same complaint sitting on its own."
    ),
    "listing_has_no_description": (
        "The first paragraph anybody reads about you becomes one you wrote, "
        "saying what you actually do rather than what a category label "
        "implies."
    ),
    "no_conversational_capability": (
        "The enquiries that arrive when the desk is closed stop going to "
        "whoever answers first. Those are not extra visitors -- they are the "
        "ones already coming to you at the wrong hour."
    ),
    "no_self_service_booking": (
        "Bookings stop being capped by the hours somebody can answer the "
        "phone, and the evening and weekend traffic you already have starts "
        "converting instead of waiting."
    ),
    "no_follow_up_automation": (
        "The follow-up happens in the busy weeks too, which are the weeks it "
        "was always being dropped in."
    ),
    "no_review_automation": (
        "Reviews start arriving from your ordinary satisfied customers "
        "rather than only from the people with a reason to write one "
        "unprompted, which moves both the rating and how many there are."
    ),
    "broken_primary_cta": (
        "Once the button points somewhere that exists, the visitors who "
        "were already convinced enough to click it reach the form instead "
        "of an error. Those are the warmest people on the site -- they had "
        "decided -- and they are the ones it is currently losing."
    ),
    "broken_internal_link": (
        "With the link repointed, people following it land on the page you "
        "meant them to read rather than on an error, and the pages behind "
        "it stop being effectively invisible to anyone who arrives that "
        "way."
    ),
    "high_friction_contact_form": (
        # The outcome only. This used to reopen with "Cutting the form back
        # to what you genuinely need", which is the solution paragraph's own
        # first clause, and then repeat its second -- so the most-sent claim in
        # the estate (648 of 1,766) made its recommendation twice in a row and
        # read as padding.
        "The change shows up as a higher share of the people who start the "
        "form finishing it: the same traffic, fewer of them lost between the "
        "first field and the last."
    ),
    "no_visible_phone_number": (
        "A number in the header, tappable on a phone, gives the callers "
        "among your visitors something to act on the moment they decide to. "
        "It costs them one tap instead of a hunt, and it captures the "
        "people who were never going to type out a form."
    ),
    "no_booking_or_enquiry_path": (
        "One clear way to get in touch, in the same place on every page, "
        "means a visitor who has decided to contact you does not have to "
        "work out how. The decision to enquire and the ability to enquire "
        "stop being two separate problems for them to solve."
    ),
    "slow_largest_contentful_paint": (
        "Getting the main content in front of people inside a couple of "
        "seconds keeps the ones who are currently leaving before they see "
        "anything at all. It is also the measurement search engines "
        "publish a threshold for, so the same work pays twice."
    ),
    "images_missing_alt_text": (
        "Described images mean a screen reader can convey what is on the "
        "page and a search engine can index it. The same edit serves the "
        "visitors who cannot see the pictures and the search listings that "
        "bring people to them."
    ),
    "serious_accessibility_violations": (
        "Clearing these opens the site to people who currently cannot use "
        "it at all, and it moves you off the list an automated audit "
        "flags. Those are the failures that carry legal exposure, so the "
        "repair reduces risk at the same time as it widens the audience."
    ),
    "javascript_console_errors": (
        # "the form, the menu, the booking widget" is already listed in the
        # problem paragraph above; naming it again is the same redundancy as
        # the contact-form upside, milder.
        "The features those scripts drive start responding again, and the "
        "visitors who currently click and get nothing stop being lost "
        "silently -- which is the part nobody can currently see."
    ),
    "failed_network_requests": (
        "When the files come back, the pieces that depend on them render "
        "as designed instead of quietly missing. The site stops looking "
        "half-finished to a share of visitors who have no way of knowing "
        "that is not how it is meant to look."
    ),
    "missing_meta_description": (
        "A written description puts a sentence you chose under your name in "
        "the search results, rather than whatever text happens to sit near "
        "the top of the page. It is the first thing a searcher reads about "
        "you, and it becomes yours to write."
    ),
    "no_structured_data": (
        "Stating your hours, location and services in the machine-readable "
        "form lets search engines and map listings show them directly "
        "rather than infer them. Details you already publish start "
        "appearing where people are actually looking for them."
    ),
    "missing_mobile_viewport": (
        "One line telling a phone how to size the page turns a shrunken "
        "desktop layout into a readable one. Everything is already there -- "
        "the visitors on phones simply start being able to read it without "
        "pinching."
    ),
    "missing_security_headers": (
        "With the headers set, the browser enforces the protections it is "
        "already capable of, and the site stops appearing on the first page "
        "of any automated scan somebody runs against it."
    ),
}

#: Why the consequence bites harder now than it used to. Deliberately free of
#: statistics: an invented "68% of customers" is exactly the fabricated claim
#: the validator exists to refuse, and a number nobody can source is worse than
#: no number. These are statements about how people behave, not about this
#: recipient's revenue -- which is the line that keeps them supportable.
_CONTEXT_REGISTERS: tuple[str, ...] = (
    # "where they form their first impression" reads to the claim validator as
    # an assertion about a form on the recipient's site: the marker pattern
    # matches the noun, not the verb. Reworded rather than exempted.
    "Most people now look a business up online before they ever pick up the "
    "phone, so first impressions are made here -- and if it fails at that "
    "point, they move on to the next result instead of calling.",
    "Nobody rings to report a problem like this. They just go back to the "
    "search results and pick whoever comes next, which is what makes it "
    "expensive without ever looking like it cost anything.",
    "Almost everyone checks a business online before contacting it now, so a "
    "step that fails here is where an enquiry quietly stops -- with nothing "
    "left behind to show that it ever started.",
    "In 2026 this is doing the work a receptionist used to do, and a failure "
    "at this point costs you the enquiry outright. It does it invisibly: "
    "nobody complains, they simply leave.",
)

#: The one place a portfolio link is allowed, and it is placed here because a
#: link only earns a click once the reader knows why it is relevant. Anchor
#: text is a phrase; the href never appears as prose. See ``_anchor``.
_RELEVANCE_REGISTERS: tuple[str, ...] = (
    "You can see how I approach this kind of work on {link}.",
    "There is a write-up of how I go about it on {link}.",
    "{link} has examples of the same work, if it helps to see it first.",
    "I keep examples of this on {link} if you want a look before replying.",
)

#: The close. A named, bounded meeting -- twenty minutes, with the subject of
#: the meeting being the problem already named above, not a general chat.
_MEETING_REGISTERS: tuple[str, ...] = (
    "If that sounds worth doing, we could book twenty minutes and I will take "
    "you through it properly.",
    "Happy to put twenty minutes in the diary and go through it with you, if "
    "that would help.",
    "If it is useful, twenty minutes on a call is enough to cover it properly "
    "and agree what is worth doing first.",
    "If you want to take it further, twenty minutes on a call would cover it "
    "and you can decide from there.",
)

#: Openers for a finding read from a Google listing.
#:
#: They name the business rather than a domain, because the population this is
#: for may not have one -- and "I came across example.com" written to somebody
#: whose listing has no website on it is false in the first six words, in the
#: same message that goes on to point out they have no website.
_LISTING_OBSERVATION_REGISTERS: tuple[str, ...] = (
    "I was looking at how {business} comes up on Google and noticed {description}.",
    "I came across {business} on Google and noticed {description}.",
    "I was looking through your Google listing for {business} and noticed {description}.",
    "Quick note on how {business} shows up on Google: I noticed {description}.",
)

_OBSERVATION_REGISTERS: tuple[str, ...] = (
    "I was looking through {domain} and noticed {description}.",
    "I came across {domain} and noticed {description}.",
    "I was reviewing {domain} and noticed {description}.",
    "Quick note on {domain}: I noticed {description}.",
)

#: Follow-ups open by acknowledging the earlier message. Re-sending the same
#: observation to somebody who did not reply is the single most common way an
#: automated sequence announces that nobody is reading the replies.
_FOLLOWUP_OPENERS: tuple[str, ...] = (
    "Following up on the note I sent about {domain} -- ",
    "Circling back on this one. ",
    "I wrote last week about {domain}. In case it got buried: ",
)


# --------------------------------------------------------------------------
# Subjects
#
# The old set was four shapes applied to every finding in the database, and two
# of them were "A broken step on {domain}" and "Something looks broken" --
# subject lines that could have been sent to any business on the internet by
# somebody who looked at nothing.
#
# A subject now names the thing that is wrong, in the words the owner uses for
# it. No fake Re:, no manufactured urgency, no invented percentage.
# --------------------------------------------------------------------------

#: What a conversion defect is called to the person who owns it. ``None`` means
#: "read it off the page that is broken", and only if that fails does the
#: industry's own name for its money page stand in.
#:
#: Reading it off the page matters more than it looks. A gym whose ``/join``
#: page was down got the subject "Quick note about kingstreetgym.co.uk's
#: free-trial page" while the body named ``/join``; a clinic whose ``/pricing``
#: page was down was told about its "booking page". Neither is false and both
#: are a mismatch the reader meets in the first two seconds, between the line
#: that made them open it and the line underneath.
_CONVERSION_TOPICS: dict[str, str | None] = {
    "broken_primary_cta": None,
    "broken_internal_link": None,
    "high_friction_contact_form": "enquiry form",
    "no_visible_phone_number": "phone number",
}

#: The page segment, as its owner says it out loud. Keyed on the same words the
#: money-path test matches, so a path that routes to the conversion engine can
#: always be named -- anything unlisted falls back to the trade's money page.
_PAGE_NOUNS: dict[str, str] = {
    "appointment": "appointment page",
    "appointments": "appointment page",
    "apply": "application page",
    "basket": "checkout",
    "book": "booking page",
    "booking": "booking page",
    "bookings": "booking page",
    "callout": "callout request page",
    "cart": "checkout",
    "checkout": "checkout",
    "consult": "consultation page",
    "consultation": "consultation page",
    "contact": "contact page",
    "contact-us": "contact page",
    "enquire": "enquiry page",
    "enquiries": "enquiry page",
    "enquiry": "enquiry page",
    "estimate": "estimate page",
    "free-trial": "free-trial page",
    "get-a-quote": "quote page",
    "inquiry": "enquiry page",
    "join": "membership page",
    "member": "membership page",
    "membership": "membership page",
    "menu": "menu page",
    "new-patient": "new-patient page",
    "new-patients": "new-patient page",
    "packages": "packages page",
    "prices": "pricing page",
    "pricing": "pricing page",
    "quote": "quote page",
    "referral": "referral page",
    "register": "registration page",
    "registration": "registration page",
    "reserve": "reservation page",
    "schedule": "scheduling page",
    "service": "services page",
    "services": "services page",
    "sign-up": "sign-up page",
    "signup": "sign-up page",
    "treatment": "treatments page",
    "treatments": "treatments page",
    "trial": "free-trial page",
    "valuation": "valuation page",
    "viewing": "viewing page",
}


def _page_noun(page_url: str | None) -> str | None:
    """What the broken page is called, from the path itself."""
    url = (page_url or "").strip().lower()
    if not url:
        return None
    path = url.split("://", 1)[-1]
    path = path[path.find("/") :] if "/" in path else ""
    path = path.split("?", 1)[0].split("#", 1)[0]
    segments = [seg for seg in path.replace("_", "-").split("/") if seg]
    if not segments:
        return None
    segments[-1] = segments[-1].rsplit(".", 1)[0]
    for segment in reversed(segments):
        noun = _PAGE_NOUNS.get(segment)
        if noun:
            return noun
    # "/book-appointment" is not a key, though "book" and "appointment" both
    # are. Sites spell the same page a dozen ways and enumerating them is a
    # losing game, so a compound segment falls back to its parts -- last part
    # first, since "book-appointment" and "appointment-booking" are both about
    # the head noun a reader would use.
    for segment in reversed(segments):
        for word in reversed(segment.split("-")):
            noun = _PAGE_NOUNS.get(word)
            if noun:
                return noun
    return None


_CONVERSION_SUBJECTS: tuple[str, ...] = (
    "{business} -- {topic} issue",
    "Quick note about {business_possessive} {topic}",
    "{business_possessive} {topic}",
    "{topic} on {domain}",
)

#: Quality findings sort into four families, and each gets its own subjects.
#: "Small accessibility issue" is honest about the size of an alt-text problem;
#: applying it to a twenty-four-second load time would not be.
_QUALITY_FAMILIES: dict[str, str] = {
    "images_missing_alt_text": "accessibility",
    "serious_accessibility_violations": "accessibility",
    "slow_largest_contentful_paint": "speed",
    "missing_meta_description": "search",
    "no_structured_data": "search",
    "javascript_console_errors": "technical",
    "failed_network_requests": "technical",
    "missing_mobile_viewport": "technical",
    "missing_security_headers": "technical",
}

_QUALITY_SUBJECTS: dict[str, tuple[str, ...]] = {
    "accessibility": (
        "Small accessibility issue on {domain}",
        "Quick website note for {business}",
        "Accessibility note for {business}",
        "A website note for {business}",
    ),
    "speed": (
        "{business} -- page speed note",
        "Quick note about how {domain} loads",
        "Load time on {domain}",
        "{business_possessive} homepage load time",
    ),
    "search": (
        "Quick note about how {business} shows in search",
        "{business} in search results",
        "A search listing note for {business}",
        "How {domain} appears in search",
    ),
    "technical": (
        "Quick website note for {business}",
        "Small technical note on {domain}",
        "{business} -- a small site issue",
        "A note on {domain}",
    ),
}

_AUTOMATION_SUBJECTS: tuple[str, ...] = (
    "Idea for {business_possessive} {workflow}",
    "Small idea for {business_possessive} {workflow}",
    "A thought on {business_possessive} {workflow}",
    "{workflow} at {business}",
)

#: Longer than this and the client truncates it mid-word, so the recipient sees
#: a sentence that stops. Shorter than the validator's own ceiling on purpose.
MAX_SUBJECT_CHARS = 78
#: A business name long enough to eat the whole subject line is cut back to the
#: domain instead, which is short and always true.
_MAX_BUSINESS_NAME_CHARS = 34


# --------------------------------------------------------------------------
# Descriptions
#
# One clause per finding type, in the present tense, starting lower case so it
# slots into any of the observation registers. The fallback is the finding's own
# title, which is always populated and was written by the detector that saw the
# page.
# --------------------------------------------------------------------------

_LEADING_COUNT = re.compile(r"^\s*(\d+)")
_MILLISECONDS = re.compile(r"(\d+)\s*ms", re.IGNORECASE)
_RATIO = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


#: Findings read from a Google listing rather than from a website.
#:
#: Kept as a set rather than inferred from the URL, because the URL is data and
#: this decides what the message is allowed to claim. A finding added here
#: without copy that names the listing would produce a sentence about a website
#: that may not exist.
LISTING_ISSUES: frozenset[str] = frozenset(
    {
        "no_website_listed",
        "no_opening_hours_listed",
        "listing_has_almost_no_photos",
        "reviews_go_unanswered",
        "listing_has_no_description",
    }
)


def _is_listing(url: str) -> bool:
    host = (url or "").split("://")[-1].split("/")[0].lower()
    return host.endswith("google.com") or host.endswith("goo.gl")


def _page_name(finding: FindingLike) -> str:
    """How a page is named to somebody who owns it.

    The finding has always carried the exact URL and the message threw it away:
    "a navigation link points at a page that returns HTTP 404" was written to
    the owner of a site whose ``/book`` page was down. It is the single most
    checkable fact available and the one that makes the difference between a
    stranger's generic warning and something that could only have been written
    about this business.

    Rendered as the path rather than the whole URL, because "your /book page"
    reads as English and "your https://example.com/book page" does not.
    """
    url = (finding.page_url or "").strip()
    # A listing is not a page of theirs, and naming it as one is the kind of
    # error that ends the reader's trust in the first sentence: they click it,
    # land on Google, and know the message was assembled rather than written.
    if _is_listing(url):
        return "your Google listing"
    if not url:
        return "the page"
    path = url.split("://", 1)[-1]
    path = path[path.find("/") :] if "/" in path else "/"
    path = path.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not path:
        return "your home page"
    return f"your {path} page"


def _count(finding: FindingLike, default: int = 0) -> int:
    """The integer the detector put at the front of its own title.

    Titles read "4 internal page(s) return an error" and "50 resource(s) failed
    to load". The count is the specific part and the ``(s)`` is not something to
    send to a stranger, so the number is taken and the noun is written here.
    """
    match = _LEADING_COUNT.match(finding.title or "")
    return int(match.group(1)) if match else default


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {plural}"


def _describe(finding: FindingLike) -> str:
    issue = finding.issue_type
    observed = (finding.observed_value or "").strip()
    page = _page_name(finding)

    if issue == "broken_internal_link":
        return f"{page} currently returns {observed or 'an error'}"
    if issue == "broken_primary_cta":
        return f"the main button on {page} returns {observed or 'an error'}"
    if issue == "high_friction_contact_form":
        return f"the enquiry form on {page} asks for {observed or 'a lot of fields'}"
    if issue == "no_booking_or_enquiry_path":
        return "there is no booking link or enquiry form anywhere on the site"

    # Listing findings. These name Google rather than the site, because that is
    # where they were read -- and because for most of this population there is
    # no site to name. Saying "your website" here would be the one thing this
    # system refuses: a claim the recipient cannot check against the thing we
    # actually looked at.
    if issue == "no_website_listed":
        return "your Google listing does not have a website on it"
    if issue == "no_opening_hours_listed":
        return "your Google listing does not show any opening hours"
    if issue == "listing_has_almost_no_photos":
        return f"your Google listing has {observed or 'almost no photographs'}"
    if issue == "reviews_go_unanswered":
        return f"on your Google listing, {observed or 'the reviews have no replies'}"
    if issue == "listing_has_no_description":
        return "your Google listing has no description written on it"

    # Absences read from the site itself. Phrased as what was looked for and
    # not found, never as what the business "has" -- a crawl that missed a
    # widget is a fact about the crawl.
    if issue == "no_conversational_capability":
        return "I could not find anything on the site that answers a visitor without a person"
    if issue == "no_self_service_booking":
        return "I could not find a way to book on the site without telephoning"
    if issue == "no_follow_up_automation":
        return "I could not find anything that follows up on an enquiry automatically"
    if issue == "no_review_automation":
        return "I could not find anything that asks customers for a review automatically"
    if issue == "no_visible_phone_number":
        return "there is no phone number on any page I looked at"
    if issue == "images_missing_alt_text":
        ratio = _RATIO.match(observed)
        counted = (
            f"{ratio.group(1)} of {ratio.group(2)} images"
            if ratio
            else _plural(_count(finding, 1), "image", "images")
        )
        return f"{counted} on {page} are missing alternative text"
    if issue == "serious_accessibility_violations":
        rule = observed.split("(", 1)[0].strip().replace("-", " ")
        if rule:
            return f"{page} fails an accessibility check for {rule}"
        return (
            f"{page} has "
            f"{_plural(_count(finding, 1), 'serious accessibility issue', 'serious accessibility issues')}"
        )
    if issue == "slow_largest_contentful_paint":
        ms = _MILLISECONDS.search(observed)
        if ms:
            return f"{page} takes {int(ms.group(1)) / 1000:.1f} seconds to show its main content"
        return f"{page} is slow to show its main content"
    if issue == "javascript_console_errors":
        return (
            f"{page} raises "
            f"{_plural(_count(finding, 1), 'script error', 'script errors')} as it loads"
        )
    if issue == "failed_network_requests":
        return (
            f"{_plural(_count(finding, 1), 'file', 'files')} on {page} "
            "fail to load with the rest of it"
        )
    if issue == "missing_mobile_viewport":
        return (
            "the site has no mobile viewport tag, so it renders at desktop "
            "width on phones"
        )
    if issue == "missing_meta_description":
        # No trailing "so search engines write their own summary" here: the
        # consequence line says exactly that, and the two together read as the
        # same sentence twice.
        return f"{page} carries no meta description"
    if issue == "no_structured_data":
        return "the site carries no structured data for search engines to read"
    if issue == "missing_security_headers":
        return (
            f"{_plural(_count(finding, 1), 'standard security header is', 'standard security headers are')} "
            f"missing from {page}"
        )
    # Always populated, and written by the detector that saw the page.
    return (finding.title or "something looks wrong").rstrip(".")[0].lower() + (
        finding.title or "something looks wrong"
    ).rstrip(".")[1:]


def compose(ctx: ComposerContext) -> ComposedMessage:
    """Build a message and its claim map together.

    The two are produced in the same expression for each factual sentence
    because building them separately is how they drift: someone edits a
    template, the claim map still describes the old wording, and the validator
    passes a sentence nothing actually supports.
    """
    if ctx.promoted_variant is not None and (
        0 <= ctx.promoted_variant < len(_OBSERVATION_REGISTERS)
    ):
        index = ctx.promoted_variant
    else:
        index = _register_index(
            ctx.variant_seed or ctx.org_domain, len(_OBSERVATION_REGISTERS)
        )
    finding = ctx.finding
    vern = vernacular_for(ctx.industry)
    engine = engine_for(finding.issue_type, finding.page_url)
    family = _QUALITY_FAMILIES.get(finding.issue_type, "technical")

    # 1. The observation. The only sentence that asserts a fact about the site,
    #    and the one the claim map is anchored on.
    description = _describe(finding)
    if finding.issue_type in LISTING_ISSUES:
        # Named, not domained. See _LISTING_OBSERVATION_REGISTERS.
        subject_name = (ctx.business_name or "").strip() or ctx.org_domain
        observation = _LISTING_OBSERVATION_REGISTERS[
            index % len(_LISTING_OBSERVATION_REGISTERS)
        ].format(business=subject_name, description=description)
    else:
        observation = _OBSERVATION_REGISTERS[index].format(
            domain=ctx.org_domain, description=description
        )
    if ctx.step_number > 0:
        opener = _FOLLOWUP_OPENERS[index % len(_FOLLOWUP_OPENERS)].format(
            domain=ctx.org_domain
        )
        # Lowercased join: "In case it got buried: I was looking at..." reads as
        # two sentences colliding.
        observation = opener + observation[0].lower() + observation[1:]

    # 2. What it means to this business, in this business's words -- unless the
    #    industry sentence would understate it, which is true of load time and
    #    of search listings in every industry alike.
    consequence = consequence_for(vern, engine)
    if engine is Engine.QUALITY:
        consequence = family_consequence(family) or consequence
    # 3. What the sender does about this exact problem, and nothing else.
    capability = capability_for(vern, engine)
    if engine is Engine.QUALITY:
        capability = family_capability(family) or capability
    # The industry sentence is a fallback, not the answer, once one engine
    # covers nine different absences. See _CAPABILITY_BY_ISSUE.
    capability = _CAPABILITY_BY_ISSUE.get(finding.issue_type, capability)
    # 4. What the sender does about this class of problem. A claim about the
    #    sender only -- the moment it becomes a claim about the recipient's
    #    peers it is "businesses like yours" again, which is refused.
    experience = _credential(capability)
    # 4a. ...unless there is a real previous job or a system actually built
    #     that matches this reader, in which case it replaces the category
    #     sentence. "I built VoxCircuit, a platform that answers enquiry calls
    #     and books the appointment" is checkable in one click; "I work on
    #     conversion issues" is what everyone says.
    study_anchor: Anchor | None = None
    if ctx.case_study is not None:
        experience = ctx.case_study.sentence()
        link_text = ctx.case_study.link_text()
        if link_text and ctx.case_study.url:
            study_anchor = Anchor(text=link_text, href=ctx.case_study.url)
    # 2b. What is mechanically wrong. Sits directly under the observation
    #     because it is the same fact explained, not a new one.
    problem = _PROBLEM_DETAIL.get(finding.issue_type, "")
    # 5b. What the repair actually is. Named work, not an offer of help.
    solution = _SOLUTION_DETAIL.get(finding.issue_type, "")
    # 5c. What they get once it is done. The other half of the consequence
    #     sentence: without it the message hands the reader a problem and a
    #     stranger, and asks them to feel bad enough to reply. This is a
    #     conditional about their own site, so it is a claim and it is mapped.
    upside = _UPSIDE_DETAIL.get(finding.issue_type, "")

    # 4b, dropped on 10 September. Worth saying why rather than deleting it
    # quietly, because the register text is still here and still good.
    #
    # It was a sentence about how people behave in general -- "in 2026 this is
    # doing the work a receptionist used to do, and a failure at this point
    # costs you the enquiry outright". True, and nothing a reader can check,
    # act on, or disagree with. It is the one paragraph in the message that
    # carried no claim-map entry, and that is the tell rather than a technicality:
    # it asserted nothing about this business because there was nothing about
    # this business in it. 32 words of throat-clearing between the consequence
    # and the repair, on a message measured at 292.
    #
    # The upside above was considered for the same cut and kept. It restates
    # the consequence with the sign flipped, which is the argument for removing
    # it, but it is also the only paragraph that tells the reader what they get
    # rather than what they have lost -- a deliberate earlier decision, guarded
    # by its own tests, and there is no evidence in 417 sends and one reply that
    # would justify overturning it on taste. Length alone is not that evidence.
    #
    # ``_CONTEXT_REGISTERS`` is kept rather than deleted: the decision is that
    # the paragraph does not earn its space, not that the copy is bad, and a
    # future test of that decision needs the text to test with.
    context = ""
    # 5. Why that is relevant, carrying the one link the rules permit. It sits
    #    here rather than in the signature because a link is only worth a click
    #    once the reader has been told why it is relevant to them.
    relevance_anchor = Anchor(text="my portfolio", href=ctx.portfolio_url)
    relevance = _RELEVANCE_REGISTERS[index % len(_RELEVANCE_REGISTERS)]
    # 6. The close: a bounded meeting about the problem already named above.
    meeting = _MEETING_REGISTERS[index % len(_MEETING_REGISTERS)]

    greeting = (
        f"Hi {ctx.contact_first_name}" if ctx.contact_first_name else FALLBACK_GREETING
    )

    # The evidence link, present only when the finding names a page. Linking a
    # page Titan did not actually open would make the observation a lie, so an
    # absent page_url produces no link rather than a guessed one.
    evidence = _evidence_anchor(finding, ctx.org_domain)
    observation_text, observation_html = _link_observation(observation, evidence)

    # Order: what I found, what is going on, what it costs, why that bites
    # now, how it gets fixed, who I am, and the ask. A detail paragraph is
    # dropped rather than faked when the issue type has no entry -- an empty
    # string here would render as a blank paragraph in the HTML.
    ordered = (
        (observation_text, observation_html),
        (problem, _esc(problem)),
        (consequence, _esc(consequence)),
        (context, _esc(context)),
        (solution, _esc(solution)),
        (upside, _esc(upside)),
        # One "who I am" paragraph rather than two: the credential and the
        # link belong to the same thought, and split across paragraphs they
        # read as two separate attempts to establish the same thing.
        _credibility(
            experience=experience,
            study_anchor=study_anchor,
            relevance=relevance,
            relevance_anchor=relevance_anchor,
            portfolio_url=ctx.portfolio_url,
        ),
        (meeting, _esc(meeting)),
    )
    present = [pair for pair in ordered if pair[0]]
    parts_text = tuple(text for text, _ in present)
    parts_html = tuple(html for _, html in present)

    pitch = greeting + ",\n\n" + "\n\n".join(parts_text)
    # The citation list. Sits between the pitch and the signature, which is
    # where a reader looks for it and where it does not interrupt the argument.
    # ``evidence`` is reused rather than re-derived so the page named here is
    # provably the page the observation was made on.
    references = _reference_block(
        finding.issue_type, evidence, one_pager_url=ctx.one_pager_url
    )
    references_text, references_html = _render_references(references)
    # The signature carries no portfolio link: it was already given above, in
    # the one place it means something. Two would be two links.
    body = (
        pitch
        + "\n\n"
        + ctx.owner_name
        + "\n"
        + ctx.mailing_address
        # Citations sit below the signature, which is both where a reader looks
        # for them and what keeps them out of the pitch budget: the validator
        # measures the pitch by splitting the body at the sender's name, so a
        # reference list above that line would be charged against
        # PITCH_MAX_WORDS and the composer would end up dropping a paragraph of
        # substance to make room for a URL.
        + references_text
        # Always a blank line: the opt-out is its own thought, and running it
        # into a citation list reads as a fourth citation.
        + "\n\n"
        + OPT_OUT_LINE
        + "\n"
    )
    body_html = _render_html(
        greeting=greeting,
        paragraphs=parts_html,
        references_html=references_html,
        owner_name=ctx.owner_name,
        mailing_address=ctx.mailing_address,
    )

    subject = _subject(ctx, vern, engine, family, index)

    # The observation is a claim about the recipient's site. The consequence is
    # Titan's characterisation of that same finding, so it is a claim about
    # their business and belongs here too -- omitting it is what the validator
    # correctly rejected the first version of this for.
    #
    # The capability sentence is a statement about the *sender* and carries no
    # claim-map entry, which is exactly why it may not name anything on the
    # recipient's site. "I noticed a few other things while I was there" used to
    # sit in that slot: an assertion about their business, unevidenced, made in
    # the one sentence nothing checks.
    claim_map = [
        {
            # The rendered sentence, not the pre-link one. The validator
            # matches claim-map entries against the body it is given, so
            # recording the version before the anchor was woven in leaves every
            # observation looking unsupported -- the claim map drifting from
            # the body is exactly what building them together is meant to stop.
            "sentence": observation_text,
            "claim": finding.issue_type,
            "finding_id": _finding_id(finding),
            "evidence_ids": list(ctx.evidence_ids),
            "source_url": finding.page_url,
        },
        {
            "sentence": consequence,
            "claim": f"{finding.issue_type}:business_impact",
            "finding_id": _finding_id(finding),
            "evidence_ids": list(ctx.evidence_ids),
            "source_url": finding.page_url,
        },
    ]
    # The mechanism paragraph explains the finding and the repair paragraph
    # scopes the work to it. Both talk about the recipient's own pages, so both
    # are claims -- and a claim that is not in the map is an unevidenced
    # assertion, however true it happens to be. They carry the same finding and
    # the same evidence because that is exactly what justifies them.
    for paragraph, kind in (
        (problem, "mechanism"),
        (solution, "remediation"),
        # The upside is a conditional about their site -- "once the button
        # points somewhere that exists, the visitors who clicked it reach the
        # form" -- and an unmapped conditional is still an unevidenced
        # assertion. It carries the finding that entails it.
        (upside, "upside"),
    ):
        # Per sentence, not per paragraph: the validator matches what its own
        # splitter produces, so a two-sentence paragraph stored as one entry
        # matches neither half of itself.
        for sentence in sentences(paragraph):
            claim_map.append(
                {
                    "sentence": sentence,
                    "claim": f"{finding.issue_type}:{kind}",
                    "finding_id": _finding_id(finding),
                    "evidence_ids": list(ctx.evidence_ids),
                    "source_url": finding.page_url,
                }
            )
    return ComposedMessage(
        subject=subject,
        body=body,
        body_html=body_html,
        claim_map=claim_map,
        variant=f"v{index}" + (f":step{ctx.step_number}" if ctx.step_number else ""),
        engine=engine,
        template_key=f"{engine.value}:{ctx.offer_key}"[:60],
        pitch_words=len(pitch.split()),
        reference_urls=[reference.url for reference in references],
        case_study_reference=(
            ctx.case_study.reference if ctx.case_study is not None else ""
        ),
    )


def family_for(issue_type: str) -> str:
    """Which quality family an issue type belongs to.

    Public because the caller has to pick a case study *before* composing, and
    relevance is judged per family. Reading the same table the composer reads
    is the point -- a second mapping maintained beside this one would drift on
    the first new issue type.
    """
    return _QUALITY_FAMILIES.get(issue_type, "technical")


def _possessive(name: str) -> str:
    """ "Hartley & Co Solicitors's consultation page" is not English.

    A name already ending in s takes the bare apostrophe. Worth the four lines:
    the subject line is the one string every recipient reads, and a doubled
    possessive in it is the most visible possible sign that nobody proofread.
    """
    name = name.rstrip()
    return f"{name}'" if name.endswith(("s", "S")) else f"{name}'s"


#: Clauses that turn a credential into an offer. The vernacular capability
#: lines end with one -- "...and can show you where the break is" -- which was
#: the right shape when nothing else in the message explained the repair. The
#: solution paragraph now does, in detail, so the promise is cut and only the
#: credential is kept.
_OFFER_TAILS = (
    ", and can ",
    " and can ",
    ", so I can ",
    " so I can ",
    ", and could ",
    " and could ",
)


def _credential(capability: str) -> str:
    """The part of a capability line that says what the sender works on."""
    for tail in _OFFER_TAILS:
        index = capability.find(tail)
        if index > 0:
            return capability[:index].rstrip(" ,") + "."
    return capability


def _link_observation(observation: str, evidence: Anchor | None) -> tuple[str, str]:
    """Put the link *on* the page reference rather than after it.

    The description already names the page -- "the main button on your
    /free-trial page returns 404". Appending the link to that produces "...
    returns 404 -- your free-trial page (https://...)", which says the page
    twice and reads like a footnote. Anchoring the phrase already in the
    sentence is the whole point of a named link.

    Falls back to appending when the sentence does not name the page, which
    happens for site-wide findings that still carry a representative URL.
    """
    if evidence is None:
        return observation, _esc(observation)

    path = urlsplit(evidence.href).path.rstrip("/")
    # The anchor's own phrase first -- "your booking page", "your home page" --
    # since that is what _describe already wrote for most findings.
    #
    # The path-shaped probes are the fallback for descriptions that quote the
    # slug. They are skipped entirely when the path is empty, which is every
    # root-page finding: " page" would otherwise match inside "your home page"
    # and swallow the noun, leaving "your home<link>".
    probes = [evidence.text]
    if path:
        probes += [f"your {path} page", f"the {path} page", f"{path} page", path]

    for probe in probes:
        if probe and probe in observation:
            # The anchor takes the page's trade noun -- "your booking page" --
            # not the raw slug it was matched on. "/book" is how the URL is
            # spelled; "booking page" is what the reader calls it, and the
            # subject line already says the latter.
            inline = Anchor(text=evidence.text, href=evidence.href)
            return (
                observation.replace(probe, inline.plain(), 1),
                _esc(observation).replace(_esc(probe), inline.html(), 1),
            )

    stem = observation.rstrip(".")
    return (
        f"{stem} -- {evidence.plain()}.",
        f"{_esc(stem)} &mdash; {evidence.html()}.",
    )


#: Query parameters that describe how somebody arrived, not which page they
#: arrived at. Carrying them into a link would put a stranger's own tracking
#: codes in front of them, and they are never part of what the evidence names.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "gclsrc",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "ref",
        "twclid",
        "yclid",
    }
)


def _clean_href(page_url: str | None) -> str | None:
    """The link target: the page itself, without the tracking that found it.

    Query parameters are not dropped wholesale -- some pages genuinely need
    one to resolve, and a link that 404s is worse than an untidy one. Only
    parameters that describe the arrival rather than the page are removed,
    plus any fragment, which no server ever sees.
    """
    url = (page_url or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    parts = urlsplit(url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def _evidence_anchor(finding: FindingLike, org_domain: str) -> Anchor | None:
    """A named link to the page the finding is actually about.

    Anchor text is the page's own role -- "your booking page", "your contact
    page" -- because that is what the reader recognises. The href is the URL
    Titan crawled, verbatim: a shortened or prettified href would point
    somewhere the evidence does not.

    None when the finding names no page. Half the issue types are site-wide.
    """
    url = _clean_href(finding.page_url)
    if url is None:
        return None
    if _is_listing(url):
        # Google's URL, not one of theirs. The root branch below would call it
        # "your home page", which is the one phrase it certainly is not.
        return Anchor(text="your Google listing", href=url)
    noun = _page_noun(url)
    if noun is None and not urlsplit(url).path.strip("/"):
        # The root. _page_noun has no segment to read and returns None, but the
        # description calls it "your home page" -- so the anchor has to agree,
        # or it links a phrase that is not in the sentence.
        noun = "home page"
    # The fallback must be the phrase _describe already wrote, not a second
    # way of saying the same thing. "the page on {domain}" was substituted into
    # a sentence that already read "the main button on your /book page", giving
    # "the main button on the page on example.com" -- the link text and the
    # sentence naming the page twice, in different words, three words apart.
    text = f"your {noun}" if noun else _page_name(finding)
    return Anchor(text=text, href=url)


#: How the examined page is labelled in the citation list.
#:
#: Not "the page I looked at". The claim validator treats any sentence pairing
#: "the"/"your" with a site noun as an assertion about the recipient that must
#: appear in the claim map, and the whole reference block unwraps into a single
#: sentence -- so one careless article in a label would drag every citation
#: into the claim map with it. The bare noun says the same thing and asserts
#: nothing. ``test_composer_four_part.py`` holds this by composing every issue
#: type through the real validator.
EVIDENCE_LABEL = "Page examined"

#: The same, for a finding read from a Google listing rather than a page.
#:
#: Same constraint as EVIDENCE_LABEL: a bare noun, no article paired with a
#: site word, or the claim validator reads the citation block as an assertion
#: about the recipient.
LISTING_EVIDENCE_LABEL = "Listing examined"

REFERENCES_HEADING = "References"

#: The visible opt-out.
#:
#: This replaced ``Unsubscribe (https://titan.example/u/<64 chars of signature>)``,
#: which is what a bare unsubscribe URL actually looks like in a plain-text
#: part and is fairly described as unprofessional.
#:
#: What did **not** change is the ``List-Unsubscribe`` /
#: ``List-Unsubscribe-Post`` header pair, which is what Gmail and Yahoo read to
#: render their own one-click unsubscribe control beside the sender's name.
#: That is a BLOCK-severity gate in ``delivery/deliverability.py`` and it is
#: the thing that protects the domain: a recipient who wants out and cannot
#: find a button presses "report spam", which costs far more than a link ever
#: did. The header is invisible in the body, so removing the visible line costs
#: nothing there.
#:
#: A reply-to opt-out is also a lawful mechanism under CAN-SPAM ("a functioning
#: return email address or other Internet-based mechanism"), and PECR and GDPR
#: ask only that opting out be easy. It is honoured for real: the inbound
#: classifier suppresses on an opt-out reply -- see ``intelligence/replies.py``,
#: where the patterns were widened to match the way people actually word this.
OPT_OUT_LINE = (
    "If you would rather not hear from me again, reply to this message "
    "and I will take you off the list."
)


def _credibility(
    *,
    experience: str,
    study_anchor: Anchor | None,
    relevance: str,
    relevance_anchor: Anchor,
    portfolio_url: str,
) -> tuple[str, str]:
    """The "who I am" paragraph as ``(text, html)``.

    With a linkable project the anchor goes on the system's own name, inside
    the sentence, rather than being appended as a bare URL after the full stop.

    The generic portfolio sentence is then dropped -- but only when the project
    page is on the portfolio's own domain, which is both the case that makes it
    redundant and the case the validator's portfolio check still passes on (it
    asserts the portfolio URL appears in the body, and a URL beneath it
    contains it). If a project is ever hosted somewhere else, both sentences
    render, so the check keeps passing for a reason rather than by luck.
    """
    if study_anchor is None:
        return (
            f"{experience} {relevance.format(link=relevance_anchor.plain())}",
            f"{_esc(experience)} "
            + _esc(relevance).replace("{link}", relevance_anchor.html()),
        )
    text = experience.replace(study_anchor.text, study_anchor.plain(), 1)
    html = _esc(experience).replace(_esc(study_anchor.text), study_anchor.html(), 1)
    if not study_anchor.href.startswith(portfolio_url):
        text += " " + relevance.format(link=relevance_anchor.plain())
        html += " " + _esc(relevance).replace("{link}", relevance_anchor.html())
    return text, html


#: How the one-page summary is labelled in the citation list.
ONE_PAGER_LABEL = "One-page summary of how I would approach this"


def _reference_block(
    issue_type: str,
    evidence: Anchor | None,
    *,
    one_pager_url: str | None = None,
) -> tuple[Reference, ...]:
    """The citations for a message: the page examined, the standards, the brief.

    The examined page goes first because it is the only line in the block that
    is about *them*. Standards bodies come after, and only from the curated
    table -- an issue type with no entry contributes nothing rather than a
    plausible-looking URL. The one-pager goes last: it is the sender's own
    material and has not earned the top of a list of primary sources.
    """
    block: list[Reference] = []
    if evidence is not None:
        # "Page examined" pointing at maps.google.com invites exactly the
        # question the citation exists to close.
        label = (
            LISTING_EVIDENCE_LABEL if _is_listing(evidence.href) else EVIDENCE_LABEL
        )
        block.append(Reference(publisher="", title=label, url=evidence.href))
    block.extend(references_for(issue_type))
    if one_pager_url:
        block.append(
            Reference(publisher="", title=ONE_PAGER_LABEL, url=one_pager_url)
        )
    return tuple(block)


def _reference_label(reference: Reference) -> str:
    """One citation as a reader reads it, without its URL."""
    if not reference.publisher:
        return reference.title
    return f"{reference.title}, {reference.publisher}"


def _render_references(references: tuple[Reference, ...]) -> tuple[str, str]:
    """The block as ``(text, html)``. Empty strings when there is nothing to cite.

    Hyphen bullets rather than "1." numbering: the validator's sentence
    splitter breaks after any full stop followed by a space, so a numbered list
    fragments into "1.", "2." and the citations either side of them. A hyphen
    carries the same list semantics and splits nothing.
    """
    if not references:
        return "", ""
    lines = [
        f"- {_reference_label(reference)}: {reference.url}" for reference in references
    ]
    text = "\n\n" + REFERENCES_HEADING + "\n" + "\n".join(lines)
    items = "\n".join(
        f'      <li style="margin:0 0 4px;">{_esc(_reference_label(reference))}: '
        f'<a href="{_attr(reference.url)}" style="color:#444;">'
        f"{_esc(reference.url)}</a></li>"
        for reference in references
    )
    html = (
        '    <p style="margin:20px 0 4px;font-size:13px;color:#444;'
        'font-weight:600;">' + _esc(REFERENCES_HEADING) + "</p>\n"
        '    <ul style="margin:0 0 16px;padding-left:18px;font-size:13px;'
        'color:#444;">\n' + items + "\n    </ul>"
    )
    return text, html


def _render_html(
    *,
    greeting: str,
    paragraphs: tuple[str, ...],
    owner_name: str,
    mailing_address: str,
    references_html: str = "",
) -> str:
    """The HTML part.

    Deliberately plain: no images, no tables, no tracking pixel, no external
    stylesheet. A cold email that arrives looking like a newsletter is filtered
    like one, and every remote asset is another thing a receiver scores. Inline
    styles only, because mail clients strip a <style> block.

    The signature is smaller and grey, and the opt-out smaller again. There is
    no unsubscribe anchor here any more: the one-click control the reader
    actually uses is rendered by Gmail from the ``List-Unsubscribe`` headers,
    beside the sender's name and above the message, which is both more visible
    and less intrusive than anything that can be put in a body.
    """
    body = "\n".join(f'    <p style="margin:0 0 16px;">{para}</p>' for para in paragraphs)
    citations = f"{references_html}\n" if references_html else ""
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.55;color:#1a1a1a;max-width:560px;">\n'
        f'    <p style="margin:0 0 16px;">{_esc(greeting)},</p>\n'
        f"{body}\n"
        '    <p style="margin:24px 0 0;color:#444;">\n'
        f"      {_esc(owner_name)}<br>\n"
        f"      {_esc(mailing_address)}\n"
        "    </p>\n"
        f"{citations}"
        '    <p style="margin:12px 0 0;font-size:12px;color:#888;">'
        f"{_esc(OPT_OUT_LINE)}</p>\n"
        "</div>"
    )


def _subject(
    ctx: ComposerContext, vern: Vernacular, engine: Engine, family: str, index: int
) -> str:
    business = (ctx.business_name or "").strip()
    if not business or len(business) > _MAX_BUSINESS_NAME_CHARS:
        business = ctx.org_domain
    fields = {
        "business": business,
        "business_possessive": _possessive(business),
        "domain": ctx.org_domain,
    }

    if engine is Engine.AUTOMATION:
        template = _AUTOMATION_SUBJECTS[index % len(_AUTOMATION_SUBJECTS)]
        subject = template.format(workflow=vern.automation_workflow, **fields)
    elif engine is Engine.CONVERSION:
        topic = (
            _CONVERSION_TOPICS.get(ctx.finding.issue_type)
            or _page_noun(ctx.finding.page_url)
            or vern.money_page
        )
        template = _CONVERSION_SUBJECTS[index % len(_CONVERSION_SUBJECTS)]
        subject = template.format(topic=topic, **fields)
    else:
        pool = _QUALITY_SUBJECTS[family]
        subject = pool[index % len(pool)].format(**fields)

    # Registers that open on the topic rather than the name would otherwise send
    # "enquiry form on example.test" -- a sentence fragment starting lower case,
    # which reads as a truncation of something the recipient never saw.
    #
    # A domain is exempt, and has to be: capitalising one produces
    # "Thegigalegal.com -- consultation page issue", which is not how anybody
    # writes their own address and is the kind of small wrong that reads as a
    # mail merge.
    subject = subject.strip()
    first = subject.split(" ", 1)[0]
    if "." in first and first[:1].islower():
        return subject[:MAX_SUBJECT_CHARS]
    return (subject[:1].upper() + subject[1:])[:MAX_SUBJECT_CHARS]


#: How many phrasing registers exist. Public because the campaign manager needs
#: to bound a promotion against it, and reaching into the private tuple from
#: outside would couple the actuator's bound to this module's internals.
VARIANT_REGISTERS = len(_OBSERVATION_REGISTERS)


def _register_index(seed: str, modulo: int) -> int:
    """Pick a register from a stable hash of the lead.

    Not random: the same lead must compose to the same message on every retry,
    replay and re-run, or an activity retry would produce a second, differently
    worded draft for a person who has already been written to.

    Not ``hash()`` either -- Python randomises string hashing per process, so
    the "stable" choice would change on every restart.
    """
    digest = hashlib.sha256(seed.encode("utf-8", "replace")).digest()
    return digest[0] % modulo


def _finding_id(finding: FindingLike) -> str:
    return str(getattr(finding, "id", ""))


__all__ = [
    "FALLBACK_GREETING",
    "MAX_SUBJECT_CHARS",
    "PITCH_MAX_WORDS",
    "PITCH_MIN_WORDS",
    "VARIANT_REGISTERS",
    "ComposedMessage",
    "ComposerContext",
    "FindingLike",
    "compose",
    "family_for",
]
