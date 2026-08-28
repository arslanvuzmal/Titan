"""What to search for, inside each industry Titan can actually pitch to.

``territories`` answers *where* to look and rotates through it when the ground
is worked out. Nothing answered *what to look for*: a campaign carried one
``target_business_type`` for its whole life, so "dentists" exhausted twenty UK
cities and then stopped, while "orthodontists", "dental clinics" and "emergency
dentists" — different businesses, same playbook, same offers — were never asked
about at all.

Measured when this was written: five active campaigns had spent every territory
their region has, and were re-asking the same five questions every hour for
$18.30 a week and nothing.

**The playbook and the search term are different things.** A playbook is chosen
by ``campaign.industry`` and decides which offers may be made and in whose
vocabulary; the search term is ``campaign.target_business_type`` and only
decides what Google Places is asked. So one playbook covers every term listed
under it here, and a term is admissible only where the playbook's offers make
sense for it. ``emergency dentists`` belongs under DENTIST because the dental
playbook's offers — booking, recall, out-of-hours capture — are exactly what an
emergency practice needs. ``dental laboratories`` does not: it is business to
business, it books nothing, and the offers would be nonsense.

**Ordered by expected value, best first.** The measured yields on this
workspace, contact rate and share scoring above the pitch bar:

    law firms      34.9% contact   17.4% pitchable
    gyms           33.3%           18.5%
    med spas       30.1%           11.2%
    dentists       27.3%           13.7%
    estate agents  25.6%           16.3%
    home services  23.8%            6.9%

Rotation takes the first term whose territories are not yet worked out, so the
order is the priority. The head of each list is the term most likely to return
businesses that book by telephone during office hours, which is the shape of
business Titan sells against.
"""

from __future__ import annotations

from titan.db.enums import Industry

#: Search terms per industry, best first.
#:
#: Phrased as Google Places would categorise them, not as a marketer would
#: write them: "solicitors" and "law firms" return materially different sets in
#: the UK, and "aesthetic clinic" returns a different set again from "med spa".
#: Every term here is one a business would use to describe itself.
VERTICALS: dict[Industry, tuple[str, ...]] = {
    Industry.LAW_FIRM: (
        "law firms",
        "solicitors",
        "personal injury solicitors",
        "family law solicitors",
        "conveyancing solicitors",
        "immigration lawyers",
        "employment solicitors",
        "wills and probate solicitors",
        "criminal defence solicitors",
        "commercial solicitors",
    ),
    Industry.GYM_FITNESS: (
        "gyms",
        "fitness studios",
        "personal training studios",
        "pilates studios",
        "yoga studios",
        "boxing gyms",
        "crossfit gyms",
        "climbing gyms",
        "martial arts schools",
        "swim schools",
    ),
    Industry.MED_SPA: (
        "med spas",
        "aesthetic clinics",
        "skin clinics",
        "laser hair removal clinics",
        "cosmetic clinics",
        "dermatology clinics",
        "hair transplant clinics",
        "weight loss clinics",
        "day spas",
        "wellness clinics",
    ),
    Industry.DENTIST: (
        "dentists",
        "dental clinics",
        "orthodontists",
        "cosmetic dentists",
        "dental implant clinics",
        "emergency dentists",
        "paediatric dentists",
        "endodontists",
        "periodontists",
        "denture clinics",
    ),
    Industry.REAL_ESTATE: (
        "estate agents",
        "letting agents",
        "property management companies",
        "commercial property agents",
        "buying agents",
        "student accommodation agents",
        "holiday letting agents",
    ),
    Industry.HVAC_HOME_SERVICES: (
        "hvac contractors",
        "plumbers",
        "electricians",
        "boiler repair services",
        "roofers",
        "locksmiths",
        "pest control services",
        "garage door repair",
        "window installers",
        "damp proofing specialists",
    ),
    Industry.VETERINARY: (
        "veterinary practices",
        "vet clinics",
        "emergency vets",
        "animal hospitals",
        "veterinary specialists",
        "equine vets",
    ),
    Industry.ACCOUNTANT: (
        "accountants",
        "chartered accountants",
        "bookkeepers",
        "tax advisors",
        "payroll services",
        "company formation agents",
    ),
    Industry.OPTICIAN: (
        "opticians",
        "optometrists",
        "eye clinics",
        "contact lens specialists",
        "laser eye surgery clinics",
    ),
    Industry.PHYSIOTHERAPY: (
        "physiotherapy clinics",
        "sports injury clinics",
        "chiropractors",
        "osteopaths",
        "podiatrists",
        "sports massage clinics",
        "rehabilitation clinics",
    ),
    Industry.SALON_BARBER: (
        "hair salons",
        "barbershops",
        "beauty salons",
        "nail salons",
        "brow and lash bars",
        "hair extension salons",
        "tanning salons",
    ),
    Industry.RESTAURANT: (
        "restaurants",
        "fine dining restaurants",
        "bistros",
        "gastropubs",
        "wine bars",
        "private dining venues",
        "event caterers",
    ),
}


def verticals_for(industry: Industry | None) -> tuple[str, ...]:
    """Search terms for an industry, best first.

    ``GENERAL`` and anything unrecognised return nothing rather than a guess.
    A campaign with no vertical list keeps the term it was configured with,
    which is the behaviour before this module existed.
    """
    if industry is None:
        return ()
    return VERTICALS.get(industry, ())


def next_vertical(
    industry: Industry | None, *, exhausted: set[str], current: str | None = None
) -> str | None:
    """The best search term whose ground is not yet worked out.

    ``exhausted`` is the set of terms this campaign has spent -- meaning every
    territory available to it has been searched with that term and stopped
    admitting anything. Comparison is casefolded because the set is built from
    stored query labels and the catalogue is written in lower case, and one
    capital letter should not resurrect a term that is finished.

    Returns ``None`` when the industry has no list, or every term in it is
    spent. That is a real answer: it says "this industry is worked out, widen
    the catalogue" rather than "search again".
    """
    spent = {term.strip().casefold() for term in exhausted}
    here = (current or "").strip().casefold()
    for term in verticals_for(industry):
        key = term.casefold()
        if key in spent or key == here:
            continue
        return term
    return None


def catalogue_size() -> int:
    """How many distinct search terms exist across every industry."""
    return sum(len(terms) for terms in VERTICALS.values())


__all__ = ["VERTICALS", "catalogue_size", "next_vertical", "verticals_for"]
