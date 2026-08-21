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
        """Offers justified by what was actually found on this site.

        The industry's own offers rank ahead of the quality ones, because a
        conversion defect is worth more to both sides than a remediation job
        and the caller takes the first match.
        """
        return [
            offer
            for offer in self.offers + QUALITY_OFFERS
            if offer.requires_finding_types & evidenced_issue_types
        ]


#: What is offered against a quality finding, in every industry alike.
#:
#: Added because the offer now has to be justified by the *headline* finding
#: rather than by anything found on the site, and no playbook had an offer for
#: an accessibility, speed or search finding. Seven thousand of the eight
#: thousand findings in the database are one of those three, so without these
#: the stricter rule would have refused to write to almost everybody -- a
#: correct rule producing a silent outage.
#:
#: Deliberately shared rather than copied into eight playbooks. Remediating an
#: alt-text problem is the same job for a gym as for a solicitor; only the
#: sentence describing it differs, and that lives in ``vernacular``.
#:
#: The values are modest on purpose. These are smaller jobs than a booking
#: system and pricing them like one is the fabricated-metric habit wearing a
#: different hat.
QUALITY_OFFERS: tuple[Offer, ...] = ()

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


QUALITY_OFFERS = (
    Offer(
        "accessibility_remediation",
        "Accessibility remediation",
        "Correcting the accessibility faults found on the site",
        frozenset({"images_missing_alt_text", "serious_accessibility_violations"}),
        1200,
    ),
    Offer(
        "page_speed",
        "Page speed work",
        "Cutting the time the main content takes to appear",
        frozenset({"slow_largest_contentful_paint", "failed_network_requests"}),
        1500,
    ),
    Offer(
        "search_visibility",
        "Search listing improvements",
        "Giving search engines the summary and structure to read",
        frozenset({"missing_meta_description", "no_structured_data"}),
        900,
    ),
    Offer(
        "site_reliability",
        "Site reliability fixes",
        "Clearing the errors and gaps that break pages for some visitors",
        frozenset(
            {
                "javascript_console_errors",
                "missing_mobile_viewport",
                "missing_security_headers",
                "broken_internal_link",
            }
        ),
        1000,
    ),
)


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

# --------------------------------------------------------------------------
# The appointment trades
#
# Five businesses that live or die on a booked slot and take almost all of them
# by telephone during office hours. Their priority paths and their offers are
# genuinely similar to one another, and deliberately not merged: the offer names
# and the priority labels are what an operator reads in the cockpit, and "book
# an appointment" means a different thing to a vet, an optician and a barber.
# --------------------------------------------------------------------------

VETERINARY = Playbook(
    industry=Industry.VETERINARY,
    name="Veterinary appointment booking and registration",
    description=(
        "New-client registration and same-day appointment requests dominate: a "
        "worried owner rings the first practice that answers."
    ),
    priority_paths=(
        "/contact",
        "/appointments",
        "/register",
        "/new-clients",
        "/services",
        "/emergency",
    ),
    priorities=(
        _p(
            "appointment_request",
            "How quickly an appointment can be requested",
            FindingCategory.CONVERSION,
        ),
        _p(
            "registration_friction",
            "Steps to register a new pet",
            FindingCategory.CONVERSION,
        ),
        _p(
            "out_of_hours_route",
            "Whether the out-of-hours route is findable",
            FindingCategory.CONVERSION,
        ),
    ),
    offers=(
        Offer(
            "appointment_booking",
            "Online appointment booking",
            "Self-service appointment booking with confirmations",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2400,
        ),
        Offer(
            "registration_intake",
            "New-client registration",
            "A short registration form that reaches the practice system",
            frozenset({"high_friction_contact_form", "no_booking_or_enquiry_path"}),
            2000,
        ),
        Offer(
            "reminder_automation",
            "Appointment reminders",
            "Automatic reminders for appointments and boosters",
            frozenset({"no_visible_phone_number", "no_booking_or_enquiry_path"}),
            1800,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to the paths that lose an owner mid-enquiry",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2200,
        ),
    ),
    prohibited_claims=(
        "Never imply a clinical outcome for an animal",
        "Never suggest the practice is failing a duty of care",
    ),
    category_emphasis={FindingCategory.CONVERSION.value: 1.25},
)

ACCOUNTANT = Playbook(
    industry=Industry.ACCOUNTANT,
    name="Accountancy enquiry and onboarding",
    description=(
        "Enquiries cluster hard around filing deadlines and go to whoever "
        "replies first; onboarding a new client is a paperwork problem."
    ),
    priority_paths=(
        "/contact",
        "/enquiry",
        "/services",
        "/get-a-quote",
        "/about",
    ),
    priorities=(
        _p(
            "enquiry_speed",
            "How quickly an enquiry can be made and answered",
            FindingCategory.CONVERSION,
        ),
        _p(
            "quote_friction",
            "Steps to get an indicative fee",
            FindingCategory.CONVERSION,
        ),
        _p(
            "onboarding_paperwork",
            "How much of onboarding happens on paper",
            FindingCategory.FOLLOW_UP,
        ),
    ),
    offers=(
        Offer(
            "enquiry_intake",
            "Enquiry intake",
            "A short enquiry form routed to the right person",
            frozenset({"high_friction_contact_form", "no_booking_or_enquiry_path"}),
            3000,
        ),
        Offer(
            "consultation_scheduling",
            "Consultation scheduling",
            "Self-service consultation booking with reminders",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2600,
        ),
        Offer(
            "client_onboarding",
            "Client onboarding",
            "Collecting documents and details without email ping-pong",
            frozenset({"no_visible_phone_number", "no_booking_or_enquiry_path"}),
            2400,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to the paths that lose an enquiry",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2600,
        ),
    ),
    prohibited_claims=(
        "Never state or imply a tax saving",
        "Never suggest the firm is non-compliant",
    ),
    category_emphasis={FindingCategory.CONVERSION.value: 1.2},
)

OPTICIAN = Playbook(
    industry=Industry.OPTICIAN,
    name="Optician eye-test booking",
    description=(
        "An eye test is a booked slot and a recall cycle. Both are usually run "
        "from a diary and a telephone."
    ),
    priority_paths=(
        "/book",
        "/appointments",
        "/eye-test",
        "/contact",
        "/services",
    ),
    priorities=(
        _p(
            "test_booking",
            "How an eye test is booked",
            FindingCategory.CONVERSION,
        ),
        _p(
            "recall_cycle",
            "Whether recalls are automated",
            FindingCategory.FOLLOW_UP,
        ),
        _p(
            "mobile_call_access",
            "How easily a phone number is reached on a phone",
            FindingCategory.CONVERSION,
        ),
    ),
    offers=(
        Offer(
            "appointment_booking",
            "Online eye-test booking",
            "Self-service appointment booking with confirmations",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2400,
        ),
        Offer(
            "recall_automation",
            "Recall automation",
            "Automatic recall when a test is due",
            frozenset({"no_booking_or_enquiry_path"}),
            2000,
        ),
        Offer(
            "enquiry_intake",
            "Enquiry intake",
            "A short enquiry form routed to the practice",
            frozenset({"high_friction_contact_form", "no_visible_phone_number"}),
            1800,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to the booking and enquiry paths",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2200,
        ),
    ),
    prohibited_claims=("Never imply a clinical outcome or diagnosis",),
    category_emphasis={FindingCategory.CONVERSION.value: 1.25},
)

PHYSIOTHERAPY = Playbook(
    industry=Industry.PHYSIOTHERAPY,
    name="Physiotherapy appointment booking",
    description=(
        "Somebody in pain books the first clinic that will see them, and a "
        "course of treatment depends on the second appointment being made."
    ),
    priority_paths=(
        "/book",
        "/appointments",
        "/contact",
        "/treatments",
        "/services",
    ),
    priorities=(
        _p(
            "appointment_request",
            "How quickly an appointment can be requested",
            FindingCategory.CONVERSION,
        ),
        _p(
            "rebooking",
            "Whether the next appointment is booked before leaving",
            FindingCategory.FOLLOW_UP,
        ),
        _p(
            "mobile_call_access",
            "How easily a phone number is reached on a phone",
            FindingCategory.CONVERSION,
        ),
    ),
    offers=(
        Offer(
            "appointment_booking",
            "Online appointment booking",
            "Self-service appointment booking with confirmations",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2400,
        ),
        Offer(
            "rebooking_automation",
            "Rebooking and reminders",
            "Reminders that keep a course of treatment on schedule",
            frozenset({"no_booking_or_enquiry_path", "no_visible_phone_number"}),
            1900,
        ),
        Offer(
            "enquiry_intake",
            "Enquiry intake",
            "A short enquiry form routed to the clinic",
            frozenset({"high_friction_contact_form"}),
            1700,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to the booking and enquiry paths",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            2200,
        ),
    ),
    prohibited_claims=(
        "Never imply a clinical outcome",
        "Never suggest a treatment will cure anything",
    ),
    category_emphasis={FindingCategory.CONVERSION.value: 1.25},
)

SALON_BARBER = Playbook(
    industry=Industry.SALON_BARBER,
    name="Salon and barber appointment booking",
    description=(
        "A chair empty at two o'clock is revenue that cannot be recovered, and "
        "the whole business runs on rebooking."
    ),
    priority_paths=(
        "/book",
        "/booking",
        "/appointments",
        "/services",
        "/contact",
    ),
    priorities=(
        _p(
            "appointment_booking",
            "Whether an appointment can be booked without ringing",
            FindingCategory.CONVERSION,
        ),
        _p(
            "rebooking",
            "Whether the next visit is booked automatically",
            FindingCategory.FOLLOW_UP,
        ),
        _p(
            "no_show_reminders",
            "Whether appointments are reminded",
            FindingCategory.FOLLOW_UP,
        ),
    ),
    offers=(
        Offer(
            "appointment_booking",
            "Online appointment booking",
            "Self-service booking that fills the quiet hours",
            frozenset({"no_booking_or_enquiry_path", "broken_primary_cta"}),
            2000,
        ),
        Offer(
            "reminder_automation",
            "Reminders and rebooking",
            "Automatic reminders and a prompt to book the next visit",
            frozenset({"no_booking_or_enquiry_path", "no_visible_phone_number"}),
            1600,
        ),
        Offer(
            "enquiry_intake",
            "Enquiry intake",
            "A short enquiry form routed to the salon",
            frozenset({"high_friction_contact_form"}),
            1400,
        ),
        Offer(
            "website_conversion",
            "Website conversion improvements",
            "Fixes to the booking path",
            frozenset(_COMMON_CONVERSION + _COMMON_TECH),
            1800,
        ),
    ),
    category_emphasis={FindingCategory.CONVERSION.value: 1.3},
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
    Industry.VETERINARY: VETERINARY,
    Industry.ACCOUNTANT: ACCOUNTANT,
    Industry.OPTICIAN: OPTICIAN,
    Industry.PHYSIOTHERAPY: PHYSIOTHERAPY,
    Industry.SALON_BARBER: SALON_BARBER,
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


__all__ = [
    "PLAYBOOKS",
    "QUALITY_OFFERS",
    "Offer",
    "Playbook",
    "Priority",
    "get_playbook",
    "select_offers",
]
