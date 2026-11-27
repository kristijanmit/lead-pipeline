"""English audit report wording (UAE and other `en` markets).

Written for the business owner: what a visitor sees and why it matters. No tool names,
scores, ratings or "free" (the same rules as the first message, docs/OUTREACH.md)."""

LANG = "en"
TITLE = "Website check: {company}"
CHECKED = "{domain} · checked {date}"
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def date(d) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


INTRO = "Worth improving on {domain}:"

# item -> (title, text). Placeholders: {domain}, {year}, {cms}
FINDINGS = {
    "cert_error": (
        "Browsers show a security warning",
        "Opening {domain} shows a \"not secure\" warning before the page loads, so most visitors turn back. "
        "Renewing the site's security certificate fixes it.",
    ),
    "not_mobile": (
        "Hard to use on a phone",
        "The site opens in its desktop layout on phones, so visitors have to zoom in and scroll sideways. "
        "Most people who look you up do it on their phone.",
    ),
    "no_https": (
        "Marked \"not secure\" in the browser",
        "The site doesn't open over a secure connection (https), so Chrome and Safari label it \"not secure\" "
        "next to the address. Visitors notice it, and Google ranks such sites lower.",
    ),
    "outdated": (
        "Looks out of date",
        "The footer still shows © {year}. To a visitor that reads as a site nobody looks after, or a business "
        "that may have closed.",
    ),
    "outdated_cms": (
        "Built on an older platform",
        "The site runs on {cms}, which few new sites use today. It gets harder to keep secure and to change, "
        "and it tends to look dated next to newer sites.",
    ),
    "perf_below_50": (
        "Slow to load on a phone",
        "On a typical mobile connection the homepage takes a while to appear. Many visitors leave before it "
        "loads, and Google takes speed into account.",
    ),
    "no_booking_or_call": (
        "No quick way to get in touch",
        "There's no tap-to-call link, booking form or WhatsApp button, so a visitor on a phone has to copy "
        "your number or look for another way to reach you.",
    ),
    "seo_below_70": (
        "Easy for Google to miss",
        "Some of the basics Google reads to understand a page are missing, so the site can show up less often "
        "when people search for what you offer.",
    ),
}

WORKS_HEADING = "What already works"
WORKS = {
    "https": "Secure connection (https)",
    "mobile": "Adapts to phone screens",
    "fast": "Loads quickly",
    "click_to_call": "Tap-to-call phone number",
    "booking": "Online booking",
    "messaging": "WhatsApp / Viber button",
    "recent": "Kept up to date",
}

CLOSING = "Happy to walk you through any of these."
