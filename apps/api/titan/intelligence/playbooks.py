"""Industry playbooks.

A playbook is a set of **priors, not conclusions** (mission section 8). It
tells the research engine which paths to visit and which checks matter most for
an industry, and it constrains which offers may be proposed. It never asserts
that a problem exists -- only evidence does that.

Two mechanisms enforce the distinction:

* ``priority_paths`` and ``priorities`` steer *where Titan looks*.
* ``offers[].requires_finding_types`` gates *what may be proposed*: an offer is
  selectable only when at least one of its required finding types was actually
  evidenced on this lead's site.

So a gym with a perfectly good trial-booking flow never receives the
"trial-lead automation" pitch, however well it fits the industry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from titan.db.enums import FindingCategory, Industry


@dataclass(frozen=True, slots=True)
class Priority:
    key: str
    label: str
    category: FindingCategory


@dataclass(frozen=True, slots=True)
class Offer:
    key: str
    label: str
    delivers: str
    #: At least one of these finding issue_types must be evidenced.
    requires_finding_types: frozenset[str]
    estimated_value_usd: float


@dataclass(frozen=True, slots=True)
class Playbook:
    industry: Industry
    name: str
    description: str
    priority_paths: tuple[str, ...]
    priorities: tuple[Priority, ...]
    offers: tuple[Offer, ...]
    #: Claims that must never be made for this industry, whatever the evidence.
    prohibited_claims: tuple[str, ...] = field(default=())
    #: Per-category multipliers applied to finding severity when scoring.
    category_emphasis: dict[str, float] = field(default_factory=dict)

    def selectable_offers(self, evidenced_issue_types: set[str]) -> list[Offer]:
        """Offers justified by what was actually found on this site."""
        return [
            offer
            for offer in self.offers
            if offer.requires_finding_types & evidenced_issue_types
        ]


_COMMON_CONVERSION = (
    "broken_primary_cta",
    "no_booking_or_enquiry_path",
    "high_friction_contact_form",
    "no_visible_phone_number",
)
_COMMON_TECH = (
    "missing_mobile_viewport",
    "javascript_console_errors",
    "broken_internal_link",
    "slow_largest_contentful_paint",
)


def _p(key: str, label: str, category: FindingCategory) -> Priority:
    return Priority(key, label, category)


LAW_FIRM = Playbook(
    industry=Industry.LAW_FIRM,
    name="Law firm intake and consultation",
    description=(
        "Enquiry capture and response speed dominate: legal enquiries are "
        "time-sensitive and rarely repeated if the first firm is slow."
    ),
    priority_paths=(
        "/contact",
        "/contact-us",
        "/consultation",
        "/practice-areas",
        "/services",
        "/about",
    ),
    priorities=(
        _p(
            "consultation_speed",
            "How quickly a consultation can be requested",
            FindingCategory.CONVERSION,
        ),
        _p(
            "contact_friction",
            "Number of steps and fields to make contact",
            FindingCategory.CONVERSION,
        ),
        _p(
            "mobile_call_access",
            "Tappable phone number on mobile",
            FindingCategory.CONVERSION,
        ),
        _p(
            "intake_forms",
            "Whether an intake form exists and is usable",
            FindingCategory.CONVERSION,
        ),
        _p(
            "practice_area_clarity",
            "Whether practice areas are clearly stated",
            FindingCategory.CONTENT,
        ),
        _p(
            "trust_signals",
            "Credentials, regulator registration, testimonials",
            FindingCategory.REPUTATION,
        ),
        _p(
            "after_hours_handling",
            "What happens to an out-of-hours enquiry",
            FindingCategory.FOLLOW_UP,
        ),
    ),
    offers=(
        Offer(
            "intake_automation",
            "Intake automation",
            "Structured intake that routes enquiries by practice area",
            frozenset({"high_friction_contact_form", "no_booking_or_enquiry_path"}),
            3500,
        ),
        Offer(
            "consultation_scheduling",
            "Consultation scheduling",
            "Self-service consultation booking with reminders",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2800,
        ),
        Offer(
            "missed_enquiry_followup",
            "Missed-enquiry follow-up",
            "Automatic follow-up when an enquiry goes unanswered",
            frozenset({"no_booking_or_enquiry_path", "no_visible_phone_number"}),
            2200,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to the paths that lose high-intent visitors",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            3000,
        ),
    ),
    prohibited_claims=(
        "Never predict or imply a case outcome",
        "Never state or imply a success rate",
        "Never suggest the firm is breaching regulatory obligations",
    ),
    category_emphasis={
        FindingCategory.CONVERSION.value: 1.25,
        FindingCategory.FOLLOW_UP.value: 1.15,
    },
)

GYM_FITNESS = Playbook(
    industry=Industry.GYM_FITNESS,
    name="Gym trial conversion and retention",
    description=(
        "Trial booking and the follow-up that converts a trial into a member "
        "are where most gym revenue is won or lost."
    ),
    priority_paths=(
        "/join",
        "/membership",
        "/free-trial",
        "/trial",
        "/timetable",
        "/classes",
        "/contact",
    ),
    priorities=(
        _p(
            "trial_booking",
            "Whether a trial can be booked online",
            FindingCategory.BOOKING,
        ),
        _p(
            "membership_enquiry",
            "How a membership enquiry is captured",
            FindingCategory.CONVERSION,
        ),
        _p(
            "class_schedule",
            "Whether the class schedule is available and current",
            FindingCategory.CONTENT,
        ),
        _p(
            "lead_followup",
            "Whether enquiries receive follow-up",
            FindingCategory.FOLLOW_UP,
        ),
        _p(
            "retention",
            "Signals about member retention and reactivation",
            FindingCategory.RETENTION,
        ),
        _p("reviews", "Volume and recency of public reviews", FindingCategory.REPUTATION),
        _p("mobile_usability", "Usability on a phone", FindingCategory.TECHNICAL),
    ),
    offers=(
        Offer(
            "trial_lead_automation",
            "Trial-lead automation",
            "Trial booking plus an automatic sequence up to the first session",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2400,
        ),
        Offer(
            "member_reactivation",
            "Member reactivation",
            "Win-back sequence for lapsed members",
            frozenset({"no_booking_or_enquiry_path", "no_visible_phone_number"}),
            1800,
        ),
        Offer(
            "class_reminders",
            "Class reminders",
            "Automatic reminders that reduce no-shows",
            frozenset({"no_booking_or_enquiry_path"}),
            1500,
        ),
        Offer(
            "review_collection",
            "Review collection",
            "Post-session review requests",
            frozenset({"no_structured_data"}),
            1200,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to the join and trial paths",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2600,
        ),
    ),
    prohibited_claims=("Never make health, fitness, or weight-loss outcome claims",),
    category_emphasis={
        FindingCategory.BOOKING.value: 1.3,
        FindingCategory.RETENTION.value: 1.2,
    },
)

RESTAURANT = Playbook(
    industry=Industry.RESTAURANT,
    name="Restaurant reputation and reservations",
    description=(
        "Reputation and the reservation path drive covers; menu availability "
        "on mobile is the most common silent failure."
    ),
    priority_paths=(
        "/menu",
        "/menus",
        "/reservations",
        "/book",
        "/booking",
        "/contact",
        "/about",
    ),
    priorities=(
        _p(
            "reputation",
            "Public review volume, rating and responses",
            FindingCategory.REPUTATION,
        ),
        _p(
            "reservation_flow",
            "Whether a table can be reserved online",
            FindingCategory.BOOKING,
        ),
        _p(
            "menu_availability",
            "Whether the menu is readable on mobile",
            FindingCategory.CONTENT,
        ),
        _p("mobile_experience", "Overall mobile usability", FindingCategory.TECHNICAL),
        _p(
            "local_seo",
            "Structured data and local listing signals",
            FindingCategory.CONTENT,
        ),
        _p(
            "missed_calls",
            "Whether calls are the only booking route",
            FindingCategory.FOLLOW_UP,
        ),
    ),
    offers=(
        Offer(
            "reputation_management",
            "Reputation management",
            "Review monitoring and response workflow",
            frozenset({"no_structured_data"}),
            1400,
        ),
        Offer(
            "reservation_automation",
            "Reservation automation",
            "Online reservations with confirmation and reminders",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2000,
        ),
        Offer(
            "review_requests",
            "Review-request workflow",
            "Automatic review requests after a visit",
            frozenset({"no_structured_data", "no_visible_phone_number"}),
            1100,
        ),
        Offer(
            "website_conversion",
            "Website and menu fixes",
            "Fixes to menu and booking paths",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            1800,
        ),
    ),
    prohibited_claims=("Never make claims about food safety or hygiene ratings",),
    category_emphasis={
        FindingCategory.REPUTATION.value: 1.3,
        FindingCategory.BOOKING.value: 1.2,
    },
)

REAL_ESTATE = Playbook(
    industry=Industry.REAL_ESTATE,
    name="Property enquiry routing and follow-up",
    description=(
        "Speed of response to a property enquiry is the dominant variable; "
        "most losses are routing and follow-up failures, not marketing."
    ),
    priority_paths=(
        "/properties",
        "/listings",
        "/contact",
        "/valuation",
        "/book-viewing",
        "/agents",
    ),
    priorities=(
        _p(
            "enquiry_response",
            "How a property enquiry is captured and routed",
            FindingCategory.CONVERSION,
        ),
        _p(
            "lead_routing",
            "Whether enquiries reach the right agent",
            FindingCategory.AUTOMATION,
        ),
        _p(
            "agent_contact_visibility",
            "Whether agent contact details are visible",
            FindingCategory.CONVERSION,
        ),
        _p(
            "listing_freshness",
            "Whether listings appear current",
            FindingCategory.CONTENT,
        ),
        _p(
            "viewing_booking",
            "Whether a viewing can be booked online",
            FindingCategory.BOOKING,
        ),
        _p("nurture", "Buyer and seller nurture sequences", FindingCategory.FOLLOW_UP),
    ),
    offers=(
        Offer(
            "lead_routing",
            "Lead routing",
            "Enquiries routed to the right agent with escalation",
            frozenset({"high_friction_contact_form", "no_booking_or_enquiry_path"}),
            3200,
        ),
        Offer(
            "viewing_scheduling",
            "Viewing scheduling",
            "Self-service viewing booking",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2600,
        ),
        Offer(
            "automated_followup",
            "Automated follow-up",
            "Structured follow-up for unanswered enquiries",
            frozenset({"no_booking_or_enquiry_path", "no_visible_phone_number"}),
            2400,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to enquiry and listing paths",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2800,
        ),
    ),
    prohibited_claims=("Never make property valuation or market-direction claims",),
    category_emphasis={
        FindingCategory.AUTOMATION.value: 1.25,
        FindingCategory.FOLLOW_UP.value: 1.2,
    },
)

HVAC_HOME_SERVICES = Playbook(
    industry=Industry.HVAC_HOME_SERVICES,
    name="Home services emergency capture and quotes",
    description=(
        "Emergency calls and quote requests are the revenue events; a missed "
        "call out of hours is usually a permanently lost job."
    ),
    priority_paths=(
        "/contact",
        "/services",
        "/emergency",
        "/quote",
        "/booking",
        "/service-area",
    ),
    priorities=(
        _p(
            "emergency_handling",
            "How an emergency call is handled",
            FindingCategory.CONVERSION,
        ),
        _p("quote_requests", "How a quote is requested", FindingCategory.CONVERSION),
        _p(
            "missed_call_recovery",
            "Whether missed calls are followed up",
            FindingCategory.FOLLOW_UP,
        ),
        _p(
            "service_area_clarity",
            "Whether the service area is stated",
            FindingCategory.CONTENT,
        ),
        _p("booking", "Whether a job can be booked online", FindingCategory.BOOKING),
        _p("reviews", "Public review signals", FindingCategory.REPUTATION),
    ),
    offers=(
        Offer(
            "missed_call_followup",
            "Missed-call follow-up",
            "Automatic text/email follow-up when a call is missed",
            frozenset({"no_visible_phone_number", "no_booking_or_enquiry_path"}),
            1900,
        ),
        Offer(
            "quote_nurturing",
            "Quote nurturing",
            "Follow-up sequence for quotes that go quiet",
            frozenset({"high_friction_contact_form", "no_booking_or_enquiry_path"}),
            2100,
        ),
        Offer(
            "seasonal_reminders",
            "Seasonal maintenance reminders",
            "Recurring service reminders",
            frozenset({"no_booking_or_enquiry_path"}),
            1600,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to quote and emergency contact paths",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2200,
        ),
    ),
    prohibited_claims=("Never claim a safety hazard exists without direct evidence",),
    category_emphasis={
        FindingCategory.CONVERSION.value: 1.3,
        FindingCategory.FOLLOW_UP.value: 1.25,
    },
)

MED_SPA = Playbook(
    industry=Industry.MED_SPA,
    name="Med spa consultation and rebooking",
    description=(
        "Consultation booking and no-show reduction dominate; regulated "
        "treatment claims make message discipline unusually important."
    ),
    priority_paths=(
        "/treatments",
        "/book",
        "/booking",
        "/consultation",
        "/contact",
        "/pricing",
    ),
    priorities=(
        _p(
            "consultation_calendar",
            "Whether consultations can be booked online",
            FindingCategory.BOOKING,
        ),
        _p(
            "treatment_pages",
            "Whether treatments are clearly described",
            FindingCategory.CONTENT,
        ),
        _p("booking_friction", "Number of steps to book", FindingCategory.CONVERSION),
        _p(
            "no_show_reduction",
            "Whether reminders are in place",
            FindingCategory.RETENTION,
        ),
        _p(
            "lead_followup",
            "Follow-up on consultation enquiries",
            FindingCategory.FOLLOW_UP,
        ),
        _p(
            "rebooking",
            "Whether repeat treatments are prompted",
            FindingCategory.RETENTION,
        ),
    ),
    offers=(
        Offer(
            "calendar_automation",
            "Calendar automation",
            "Online consultation booking with availability",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2700,
        ),
        Offer(
            "consultation_reminders",
            "Consultation reminders",
            "Reminder sequence that reduces no-shows",
            frozenset({"no_booking_or_enquiry_path"}),
            1700,
        ),
        Offer(
            "lead_nurturing",
            "Lead nurturing",
            "Follow-up for enquiries that do not book immediately",
            frozenset({"high_friction_contact_form", "no_booking_or_enquiry_path"}),
            2000,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to booking and treatment paths",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2400,
        ),
    ),
    prohibited_claims=(
        "Never make medical, clinical, or treatment-outcome claims",
        "Never reference before/after imagery as proof of results",
        "Never imply a regulatory or licensing deficiency",
    ),
    category_emphasis={
        FindingCategory.BOOKING.value: 1.3,
        FindingCategory.RETENTION.value: 1.2,
    },
)

DENTIST = Playbook(
    industry=Industry.DENTIST,
    name="Dental appointments and recall",
    description=(
        "New-patient booking plus recall automation; most practices lose more "
        "to missed recalls than to weak acquisition."
    ),
    priority_paths=(
        "/book",
        "/booking",
        "/appointments",
        "/new-patients",
        "/contact",
        "/emergency",
        "/fees",
    ),
    priorities=(
        _p(
            "appointment_booking",
            "Whether appointments can be booked online",
            FindingCategory.BOOKING,
        ),
        _p("new_patient_flow", "How a new patient registers", FindingCategory.CONVERSION),
        _p(
            "checkup_reminders",
            "Whether check-up reminders exist",
            FindingCategory.RETENTION,
        ),
        _p(
            "emergency_contact", "How an emergency is handled", FindingCategory.CONVERSION
        ),
        _p(
            "insurance_information",
            "Whether fees and cover are explained",
            FindingCategory.CONTENT,
        ),
        _p(
            "patient_reactivation",
            "Whether lapsed patients are re-engaged",
            FindingCategory.RETENTION,
        ),
    ),
    offers=(
        Offer(
            "booking_improvement",
            "Booking improvement",
            "Online appointment booking for new and existing patients",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2600,
        ),
        Offer(
            "recall_automation",
            "Recall automation",
            "Automatic check-up recall sequence",
            frozenset({"no_booking_or_enquiry_path"}),
            2200,
        ),
        Offer(
            "patient_reactivation",
            "Patient reactivation",
            "Re-engagement for patients who have lapsed",
            frozenset({"no_booking_or_enquiry_path", "no_visible_phone_number"}),
            1900,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to booking and new-patient paths",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2300,
        ),
    ),
    prohibited_claims=(
        "Never make clinical or treatment-outcome claims",
        "Never imply a regulatory or hygiene deficiency",
    ),
    category_emphasis={
        FindingCategory.BOOKING.value: 1.25,
        FindingCategory.RETENTION.value: 1.25,
    },
)

GENERAL = Playbook(
    industry=Industry.GENERAL,
    name="General business fallback",
    description=(
        "Used when the industry is unknown. Identifies the dominant conversion "
        "event from what the site actually offers, then works only from "
        "evidenced friction on that path. Never opens with a generic "
        "'your business could use AI' framing."
    ),
    priority_paths=("/contact", "/about", "/services", "/pricing", "/book"),
    priorities=(
        _p(
            "dominant_conversion",
            "The main action the site asks visitors to take",
            FindingCategory.CONVERSION,
        ),
        _p(
            "contact_friction",
            "How hard it is to make contact",
            FindingCategory.CONVERSION,
        ),
        _p(
            "technical_health",
            "Errors, speed, and mobile usability",
            FindingCategory.TECHNICAL,
        ),
        _p(
            "followup",
            "Whether enquiries appear to be followed up",
            FindingCategory.FOLLOW_UP,
        ),
    ),
    offers=(
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to the path that loses the most high-intent visitors",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2000,
        ),
        Offer(
            "enquiry_automation",
            "Enquiry automation",
            "Capture and follow-up for enquiries",
            frozenset({"high_friction_contact_form", "no_booking_or_enquiry_path"}),
            1800,
        ),
    ),
    prohibited_claims=(
        "Never assert an industry-specific problem without evidence from this site",
        "Never open with a generic claim that the business needs AI",
    ),
)

PLAYBOOKS: dict[Industry, Playbook] = {
    Industry.LAW_FIRM: LAW_FIRM,
    Industry.GYM_FITNESS: GYM_FITNESS,
    Industry.RESTAURANT: RESTAURANT,
    Industry.REAL_ESTATE: REAL_ESTATE,
    Industry.HVAC_HOME_SERVICES: HVAC_HOME_SERVICES,
    Industry.MED_SPA: MED_SPA,
    Industry.DENTIST: DENTIST,
    Industry.GENERAL: GENERAL,
}


def get_playbook(industry: Industry) -> Playbook:
    """Always returns a playbook; unknown industries fall back to GENERAL."""
    return PLAYBOOKS.get(industry, GENERAL)


def select_offers(industry: Industry, evidenced_issue_types: set[str]) -> list[Offer]:
    """Offers a message is permitted to propose for this lead.

    Empty when nothing was evidenced -- which is the intended outcome, because
    there is then nothing truthful to offer.
    """
    return get_playbook(industry).selectable_offers(evidenced_issue_types)


__all__ = ["PLAYBOOKS", "Offer", "Playbook", "Priority", "get_playbook", "select_offers"]
