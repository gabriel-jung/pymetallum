"""Country name to ISO 3166-1 alpha-2 code mapping.

Codes match the ``<select name="country">`` values used by Metal Archives
advanced search. Generated from the MA search page.
"""

# fmt: off
COUNTRY_CODES = {
    "afghanistan": "AF", "albania": "AL", "algeria": "DZ",
    "american samoa": "AS", "andorra": "AD", "angola": "AO",
    "anguilla": "AI", "antarctica": "AQ", "antigua and barbuda": "AG",
    "argentina": "AR", "armenia": "AM", "aruba": "AW",
    "australia": "AU", "austria": "AT", "azerbaijan": "AZ",
    "bahamas": "BS", "bahrain": "BH", "bangladesh": "BD",
    "barbados": "BB", "belarus": "BY", "belgium": "BE",
    "belize": "BZ", "benin": "BJ", "bermuda": "BM",
    "bhutan": "BT", "bolivia": "BO",
    "bonaire, sint eustatius and saba": "BQ",
    "bosnia and herzegovina": "BA", "botswana": "BW",
    "bouvet island": "BV", "brazil": "BR",
    "british indian ocean territory": "IO", "brunei": "BN",
    "bulgaria": "BG", "burkina faso": "BF", "burundi": "BI",
    "cambodia": "KH", "cameroon": "CM", "canada": "CA",
    "cape verde": "CV", "cayman islands": "KY",
    "central african republic": "CF", "chad": "TD", "chile": "CL",
    "china": "CN", "christmas island": "CX",
    "cocos (keeling) islands": "CC", "colombia": "CO", "comoros": "KM",
    "congo, democratic republic of": "CD", "congo, republic of": "CG",
    "cook islands": "CK", "costa rica": "CR", "croatia": "HR",
    "cuba": "CU", "curaçao": "CW", "cyprus": "CY", "czechia": "CZ",
    "côte d'ivoire": "CI", "denmark": "DK", "djibouti": "DJ",
    "dominica": "DM", "dominican republic": "DO", "east timor": "TL",
    "ecuador": "EC", "egypt": "EG", "el salvador": "SV",
    "equatorial guinea": "GQ", "eritrea": "ER", "estonia": "EE",
    "eswatini": "SZ", "ethiopia": "ET", "falkland islands": "FK",
    "faroe islands": "FO", "fiji": "FJ", "finland": "FI",
    "france": "FR", "french guiana": "GF", "french polynesia": "PF",
    "french southern territories": "TF", "gabon": "GA", "gambia": "GM",
    "georgia": "GE", "germany": "DE", "ghana": "GH",
    "gibraltar": "GI", "greece": "GR", "greenland": "GL",
    "grenada": "GD", "guadeloupe": "GP", "guam": "GU",
    "guatemala": "GT", "guernsey": "GG", "guinea": "GN",
    "guinea-bissau": "GW", "guyana": "GY", "haiti": "HT",
    "heard and mcdonald islands": "HM", "honduras": "HN",
    "hong kong": "HK", "hungary": "HU", "iceland": "IS",
    "india": "IN", "indonesia": "ID", "international": "XX",
    "iran": "IR", "iraq": "IQ", "ireland": "IE", "isle of man": "IM",
    "israel": "IL", "italy": "IT", "jamaica": "JM", "japan": "JP",
    "jersey": "JE", "jordan": "JO", "kazakhstan": "KZ", "kenya": "KE",
    "kiribati": "KI", "korea, north": "KP", "korea, south": "KR",
    "kuwait": "KW", "kyrgyzstan": "KG", "laos": "LA", "latvia": "LV",
    "lebanon": "LB", "lesotho": "LS", "liberia": "LR", "libya": "LY",
    "liechtenstein": "LI", "lithuania": "LT", "luxembourg": "LU",
    "macau": "MO", "madagascar": "MG", "malawi": "MW",
    "malaysia": "MY", "maldives": "MV", "mali": "ML", "malta": "MT",
    "marshall islands": "MH", "martinique": "MQ", "mauritania": "MR",
    "mauritius": "MU", "mayotte": "YT", "mexico": "MX",
    "micronesia, federated states of": "FM", "moldova": "MD",
    "monaco": "MC", "mongolia": "MN", "montenegro": "ME",
    "montserrat": "MS", "morocco": "MA", "mozambique": "MZ",
    "myanmar": "MM", "namibia": "NA", "nauru": "NR", "nepal": "NP",
    "netherlands": "NL", "new caledonia": "NC", "new zealand": "NZ",
    "nicaragua": "NI", "niger": "NE", "nigeria": "NG", "niue": "NU",
    "norfolk island": "NF", "north macedonia": "MK",
    "northern mariana islands": "MP", "norway": "NO", "oman": "OM",
    "pakistan": "PK", "palau": "PW", "palestine": "PS", "panama": "PA",
    "papua new guinea": "PG", "paraguay": "PY", "peru": "PE",
    "philippines": "PH", "pitcairn island": "PN", "poland": "PL",
    "portugal": "PT", "puerto rico": "PR", "qatar": "QA",
    "reunion": "RE", "romania": "RO", "russia": "RU", "rwanda": "RW",
    "saint barthélemy": "BL", "saint kitts & nevis": "KN",
    "saint lucia": "LC", "saint martin (french part)": "MF",
    "saint pierre and miquelon": "PM",
    "saint vincent and the grenadines": "VC", "samoa": "WS",
    "san marino": "SM", "sao tome and principe": "ST",
    "saudi arabia": "SA", "senegal": "SN", "serbia": "RS",
    "seychelles": "SC", "sierra leone": "SL", "singapore": "SG",
    "sint maarten (dutch part)": "SX", "slovakia": "SK",
    "slovenia": "SI", "solomon islands": "SB", "somalia": "SO",
    "south africa": "ZA",
    "south georgia & south sandwich islands": "GS",
    "south sudan": "SS", "spain": "ES", "sri lanka": "LK",
    "st helena, ascension & tristan da cunha": "SH", "sudan": "SD",
    "suriname": "SR", "svalbard": "SJ", "sweden": "SE",
    "switzerland": "CH", "syria": "SY", "taiwan": "TW",
    "tajikistan": "TJ", "tanzania": "TZ", "thailand": "TH",
    "togo": "TG", "tokelau": "TK", "tonga": "TO",
    "trinidad and tobago": "TT", "tunisia": "TN",
    "turkmenistan": "TM", "turks and caicos islands": "TC",
    "tuvalu": "TV", "türkiye": "TR",
    "u.s. minor outlying islands": "UM", "uganda": "UG",
    "ukraine": "UA", "united arab emirates": "AE",
    "united kingdom": "GB", "united states": "US", "unknown": "ZZ",
    "uruguay": "UY", "uzbekistan": "UZ", "vanuatu": "VU",
    "vatican city": "VA", "venezuela": "VE", "vietnam": "VN",
    "virgin islands (british)": "VG", "virgin islands (us)": "VI",
    "wallis and futuna": "WF", "western sahara": "EH", "yemen": "YE",
    "zambia": "ZM", "zimbabwe": "ZW", "åland islands": "AX",
}
# fmt: on

# Reverse map for display purposes
CODE_TO_COUNTRY = {v: k.title() for k, v in COUNTRY_CODES.items()}


def resolve_country(value: str) -> str | None:
    """Resolve a country name or code to an ISO 3166-1 alpha-2 code.

    Accepts either an ISO code (e.g. "FR", "NO") or a country name
    (e.g. "france", "Norway"). Returns the code, or None if unrecognized.
    """
    upper = value.strip().upper()

    # Already a valid 2-letter code?
    if len(upper) == 2 and upper in CODE_TO_COUNTRY:
        return upper

    # Try name lookup
    lower = value.strip().lower()
    if lower in COUNTRY_CODES:
        return COUNTRY_CODES[lower]

    # Partial match: find countries starting with the input
    matches = [code for name, code in COUNTRY_CODES.items() if name.startswith(lower)]
    if len(matches) == 1:
        return matches[0]

    return None
