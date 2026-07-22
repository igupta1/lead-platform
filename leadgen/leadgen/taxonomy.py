"""Two-level industry taxonomy for the CFO lead inventory.

A lead is classified into a granular ``Niche``; the coarse ``industry``
(parent) is DERIVED from the niche via a fixed map, never classified
separately. That guarantees the rollup is always consistent — querying a
parent (``software_saas``) returns every lead in any of its children
(``b2b_saas``, ``ai_ml``, …) — and keeps parent-level matching robust
even when the exact child niche is occasionally off.

The outreach workflow (System B) maps a prospect's stated niche to one
of these values before querying; that mapping is not this module's job.
``/api/niches`` serves ``PARENT_CHILDREN`` so System B knows the shape.
"""

from __future__ import annotations

from enum import Enum


class Niche(str, Enum):
    # Software / Tech
    B2B_SAAS = "b2b_saas"
    CONSUMER_APP = "consumer_app"
    AI_ML = "ai_ml"
    DEVTOOLS_INFRA = "devtools_infra"
    VERTICAL_SAAS = "vertical_saas"
    HARDWARE_IOT = "hardware_iot"
    # Fintech
    PAYMENTS = "payments"
    LENDING = "lending"
    WEALTH_INVESTING = "wealth_investing"
    INSURTECH = "insurtech"
    CRYPTO_WEB3 = "crypto_web3"
    BANKING_INFRA = "banking_infra"
    # E-commerce / Consumer Brands
    DTC_BRAND = "dtc_brand"
    CPG_FOOD_BEVERAGE = "cpg_food_beverage"
    APPAREL_FASHION = "apparel_fashion"
    BEAUTY_PERSONAL_CARE = "beauty_personal_care"
    BRICK_MORTAR_RETAIL = "brick_mortar_retail"
    MARKETPLACE = "marketplace"
    # Healthcare
    MEDICAL_PRACTICE = "medical_practice"
    DENTAL = "dental"
    VETERINARY = "veterinary"
    BEHAVIORAL_MENTAL_HEALTH = "behavioral_mental_health"
    BIOTECH_PHARMA = "biotech_pharma"
    MEDICAL_DEVICES = "medical_devices"
    DIGITAL_HEALTH = "digital_health"
    SENIOR_CARE = "senior_care"
    # Professional Services
    LAW_FIRM = "law_firm"
    ACCOUNTING_BOOKKEEPING = "accounting_bookkeeping"
    MARKETING_CREATIVE_AGENCY = "marketing_creative_agency"
    CONSULTING = "consulting"
    STAFFING_HR = "staffing_hr"
    IT_MSP = "it_msp"
    ARCHITECTURE_ENGINEERING = "architecture_engineering"
    # Manufacturing / Industrial
    INDUSTRIAL_MFG = "industrial_mfg"
    CONSUMER_GOODS_MFG = "consumer_goods_mfg"
    FOOD_MANUFACTURING = "food_manufacturing"
    AEROSPACE_DEFENSE = "aerospace_defense"
    ELECTRONICS_MFG = "electronics_mfg"
    CHEMICALS_MATERIALS = "chemicals_materials"
    # Construction / Trades
    GENERAL_CONTRACTOR = "general_contractor"
    SPECIALTY_TRADES = "specialty_trades"
    HOMEBUILDER_DEVELOPER = "homebuilder_developer"
    CIVIL_INFRASTRUCTURE = "civil_infrastructure"
    # Home & Field Services
    HVAC_PLUMBING_ELECTRICAL = "hvac_plumbing_electrical"
    CLEANING_JANITORIAL = "cleaning_janitorial"
    LANDSCAPING = "landscaping"
    PEST_CONTROL = "pest_control"
    SECURITY_SERVICES = "security_services"
    # Real Estate
    PROPERTY_MANAGEMENT = "property_management"
    REAL_ESTATE_INVESTMENT = "real_estate_investment"
    BROKERAGE = "brokerage"
    PROPTECH = "proptech"
    # Logistics / Transportation
    TRUCKING_FREIGHT = "trucking_freight"
    LAST_MILE_DELIVERY = "last_mile_delivery"
    SUPPLY_CHAIN_3PL = "supply_chain_3pl"
    FLEET_SERVICES = "fleet_services"
    # Hospitality / Food & Beverage
    RESTAURANT = "restaurant"
    HOTEL_LODGING = "hotel_lodging"
    CATERING_EVENTS = "catering_events"
    BAR_BREWERY = "bar_brewery"
    FOOD_SERVICE = "food_service"
    # Fitness / Wellness
    GYM_STUDIO = "gym_studio"
    SPA_WELLNESS = "spa_wellness"
    HEALTH_COACHING = "health_coaching"
    # Media / Entertainment
    CONTENT_CREATOR = "content_creator"
    PRODUCTION_STUDIO = "production_studio"
    GAMING = "gaming"
    PUBLISHING = "publishing"
    MUSIC_EVENTS = "music_events"
    # Education
    K12_SCHOOL = "k12_school"
    HIGHER_ED = "higher_ed"
    EDTECH = "edtech"
    TRAINING_BOOTCAMP = "training_bootcamp"
    # Energy / Utilities
    OIL_GAS = "oil_gas"
    RENEWABLES_SOLAR = "renewables_solar"
    UTILITIES = "utilities"
    CLEANTECH = "cleantech"
    # Agriculture
    FARMING = "farming"
    AGTECH = "agtech"
    FOOD_PRODUCTION = "food_production"
    # Nonprofit / Public
    CHARITY_FOUNDATION = "charity_foundation"
    RELIGIOUS = "religious"
    MEMBERSHIP_ASSOCIATION = "membership_association"
    SOCIAL_SERVICES = "social_services"
    # Cannabis
    CANNABIS = "cannabis"
    # Fallbacks
    OTHER = "other"
    UNKNOWN = "unknown"


# Parent (coarse industry value) -> ordered child niches. Source of truth
# for both the derived industry and the /api/niches reference.
PARENTS: dict[str, tuple[Niche, ...]] = {
    "software_saas": (
        Niche.B2B_SAAS, Niche.CONSUMER_APP, Niche.AI_ML,
        Niche.DEVTOOLS_INFRA, Niche.VERTICAL_SAAS, Niche.HARDWARE_IOT,
    ),
    "fintech": (
        Niche.PAYMENTS, Niche.LENDING, Niche.WEALTH_INVESTING,
        Niche.INSURTECH, Niche.CRYPTO_WEB3, Niche.BANKING_INFRA,
    ),
    "ecommerce_retail": (
        Niche.DTC_BRAND, Niche.CPG_FOOD_BEVERAGE, Niche.APPAREL_FASHION,
        Niche.BEAUTY_PERSONAL_CARE, Niche.BRICK_MORTAR_RETAIL, Niche.MARKETPLACE,
    ),
    "healthcare": (
        Niche.MEDICAL_PRACTICE, Niche.DENTAL, Niche.VETERINARY,
        Niche.BEHAVIORAL_MENTAL_HEALTH, Niche.BIOTECH_PHARMA,
        Niche.MEDICAL_DEVICES, Niche.DIGITAL_HEALTH, Niche.SENIOR_CARE,
    ),
    "professional_services": (
        Niche.LAW_FIRM, Niche.ACCOUNTING_BOOKKEEPING,
        Niche.MARKETING_CREATIVE_AGENCY, Niche.CONSULTING, Niche.STAFFING_HR,
        Niche.IT_MSP, Niche.ARCHITECTURE_ENGINEERING,
    ),
    "manufacturing": (
        Niche.INDUSTRIAL_MFG, Niche.CONSUMER_GOODS_MFG, Niche.FOOD_MANUFACTURING,
        Niche.AEROSPACE_DEFENSE, Niche.ELECTRONICS_MFG, Niche.CHEMICALS_MATERIALS,
    ),
    "construction": (
        Niche.GENERAL_CONTRACTOR, Niche.SPECIALTY_TRADES,
        Niche.HOMEBUILDER_DEVELOPER, Niche.CIVIL_INFRASTRUCTURE,
    ),
    "home_services": (
        Niche.HVAC_PLUMBING_ELECTRICAL, Niche.CLEANING_JANITORIAL,
        Niche.LANDSCAPING, Niche.PEST_CONTROL, Niche.SECURITY_SERVICES,
    ),
    "real_estate": (
        Niche.PROPERTY_MANAGEMENT, Niche.REAL_ESTATE_INVESTMENT,
        Niche.BROKERAGE, Niche.PROPTECH,
    ),
    "logistics_transport": (
        Niche.TRUCKING_FREIGHT, Niche.LAST_MILE_DELIVERY,
        Niche.SUPPLY_CHAIN_3PL, Niche.FLEET_SERVICES,
    ),
    "hospitality_food": (
        Niche.RESTAURANT, Niche.HOTEL_LODGING, Niche.CATERING_EVENTS,
        Niche.BAR_BREWERY, Niche.FOOD_SERVICE,
    ),
    "fitness_wellness": (
        Niche.GYM_STUDIO, Niche.SPA_WELLNESS, Niche.HEALTH_COACHING,
    ),
    "media_entertainment": (
        Niche.CONTENT_CREATOR, Niche.PRODUCTION_STUDIO, Niche.GAMING,
        Niche.PUBLISHING, Niche.MUSIC_EVENTS,
    ),
    "education": (
        Niche.K12_SCHOOL, Niche.HIGHER_ED, Niche.EDTECH, Niche.TRAINING_BOOTCAMP,
    ),
    "energy": (
        Niche.OIL_GAS, Niche.RENEWABLES_SOLAR, Niche.UTILITIES, Niche.CLEANTECH,
    ),
    "agriculture": (
        Niche.FARMING, Niche.AGTECH, Niche.FOOD_PRODUCTION,
    ),
    "nonprofit": (
        Niche.CHARITY_FOUNDATION, Niche.RELIGIOUS,
        Niche.MEMBERSHIP_ASSOCIATION, Niche.SOCIAL_SERVICES,
    ),
    "cannabis": (Niche.CANNABIS,),
    "other": (Niche.OTHER,),
    "unknown": (Niche.UNKNOWN,),
}

# child niche value -> parent industry value
NICHE_PARENT: dict[str, str] = {
    niche.value: parent for parent, niches in PARENTS.items() for niche in niches
}

# Every child niche value, in declaration order.
ALL_NICHES: tuple[str, ...] = tuple(NICHE_PARENT.keys())

# parent value -> list of child values (for the /api/niches reference).
PARENT_CHILDREN: dict[str, list[str]] = {
    parent: [n.value for n in niches] for parent, niches in PARENTS.items()
}


def parent_of(niche: str | None) -> str:
    """Coarse industry value for a niche. Unknown / unmapped -> 'unknown'."""
    if not niche:
        return "unknown"
    return NICHE_PARENT.get(niche, "unknown")
