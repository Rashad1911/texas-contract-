"""
bid_prep.py — Bid prep step for the Texas Government Contract Opportunity Agent.
For each opportunity: finds 5 local subs (name, phone, address, website, email if found),
writes a Sources Sought response OR a subcontracting question, writes a sub quote request,
saves to bid_prep/<id>.md, and returns markdown to append to the emailed report.
Secrets: GOOGLE_PLACES_API_KEY (required for subs), COMPANY_PHONE, COMPANY_EMAIL, COMPANY_UEI.
"""
from __future__ import annotations
import argparse, os, re, time
from pathlib import Path
import requests

COMPANY = {
    "legal_name": "Deyampert & Patterson Enterprises LLC",
    "dba": "D&P Cleaning",
    "ein": "33-3960248",
    "website": "cleaningdp.com",
    "contact": "Rashad Deyampert",
    "phone": os.getenv("COMPANY_PHONE", "[phone]"),
    "email": os.getenv("COMPANY_EMAIL", "[email]"),
    "uei": os.getenv("COMPANY_UEI", "[add once SAM.gov issues it]"),
}
SIGNATURE = (f"Rashad Deyampert\n{COMPANY['legal_name']}\n"
             f"dba {COMPANY['dba']} · {COMPANY['website']}\n"
             f"{COMPANY['phone']} · {COMPANY['email']}")

SERVICE_QUERIES = {
    "janitorial": ["commercial janitorial service", "office cleaning company"],
    "custodial": ["commercial janitorial service", "office cleaning company"],
    "landscaping": ["commercial landscaping company", "lawn maintenance service"],
    "grounds": ["commercial landscaping company", "grounds maintenance service"],
    "security": ["security guard company", "security services company"],
    "debris": ["junk removal company", "debris removal service"],
    "kitchen": ["kitchen hood cleaning service", "restaurant fryer filter cleaning"],
    "pest": ["commercial pest control"],
    "moving": ["commercial moving company"],
}
OUT_DIR = Path("bid_prep")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JUNK_EMAIL = ("example.", "sentry", "wixpress", "godaddy", "@2x", ".png", ".jpg", "domain.com")

def _queries_for(service):
    s = (service or "").lower()
    for key, qs in SERVICE_QUERIES.items():
        if key in s:
            return qs
    return [f"{service} company"]

def _places_search(query, api_key):
    r = requests.post("https://places.googleapis.com/v1/places:searchText",
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask":
            "places.id,places.displayName,places.formattedAddress,places.nationalPhoneNumber,"
            "places.websiteUri,places.rating,places.userRatingCount,places.businessStatus"},
        json={"textQuery": query, "maxResultCount": 10}, timeout=20)
    r.raise_for_status()
    return r.json().get("places", [])

def _find_email(website):
    if not website:
        return None
    base = website.rstrip("/")
    for url in (base, base + "/contact", base + "/contact-us"):
        try:
            html = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).text
        except requests.RequestException:
            continue
        for e in EMAIL_RE.findall(html):
            if not any(j in e.lower() for j in JUNK_EMAIL):
                return e
        time.sleep(0.5)
    return None

def find_subcontractors(service, location, limit=5):
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        print("GOOGLE_PLACES_API_KEY not set — skipping sub search.")
        return []
    if not location or "not listed" in location.lower():
        return []
    seen, results = set(), []
    for q in _queries_for(service):
        try:
            places = _places_search(f"{q} near {location}", api_key)
        except requests.RequestException as e:
            print(f"Places search failed for '{q}': {e}")
            continue
        for p in places:
            if p["id"] in seen or p.get("businessStatus") not in (None, "OPERATIONAL"):
                continue
            if not p.get("nationalPhoneNumber"):
                continue
            seen.add(p["id"])
            results.append({"name": p["displayName"]["text"], "address": p.get("formattedAddress", ""),
                "phone": p.get("nationalPhoneNumber", ""), "website": p.get("websiteUri"),
                "rating": p.get("rating"), "reviews": p.get("userRatingCount", 0)})
    results.sort(key=lambda r: (r["reviews"] or 0), reverse=True)
    results = results[:limit]
    for r in results:
        r["email"] = _find_email(r["website"])
    return results

def _is_sources_sought(notice_type, title):
    t = f"{notice_type} {title}".lower()
    return any(k in t for k in ("sources sought", "rfi", "request for information"))

def capability_response(opp):
    loc = f" in {opp['location']}" if opp.get("location") else ""
    return f"""Subject: Response to Sources Sought – {opp['title']}

Dear Contracting Officer,

{COMPANY['legal_name']}, doing business as {COMPANY['dba']}, submits this response to the Sources Sought notice for {opp['title']}{loc}.

COMPANY INFORMATION
{COMPANY['legal_name']} ({COMPANY['dba']})
EIN: {COMPANY['ein']}
UEI: {COMPANY['uei']}
Texas-based small business · {COMPANY['website']}
Point of contact: {COMPANY['contact']} · {COMPANY['phone']} · {COMPANY['email']}

CAPABILITIES
{COMPANY['dba']} provides commercial service delivery in Texas, managed through vetted local service partners. {COMPANY['dba']} provides supervision, quality inspections, scheduling, and a single point of contact for the government.

APPROACH FOR THIS REQUIREMENT
We would perform the work using an established local firm under {COMPANY['dba']} management, with regular quality inspections and a documented corrective-action process.

QUESTIONS FOR THE GOVERNMENT
1. Will the resulting solicitation be set aside for small businesses, and if so, what limitations on subcontracting will apply?
2. What is the approximate size of the facility and the required service frequency?
3. Will a site visit be offered?

We are interested in this requirement and would welcome notification when the solicitation is released.

Respectfully,
{SIGNATURE}
"""

def subcontracting_question(opp):
    sol = opp.get("solicitation") or opp.get("id", "")
    return f"""Subject: Question: {sol} – {opp['title']} – Subcontracting

Hello,

I'm reviewing {opp['title']} ({sol}) and plan to submit a response. I have two questions:

1. Is subcontracting permitted under this solicitation? If so, are there limits on the percentage of work that may be subcontracted?
2. May an approved subcontractor's supervisor serve as the on-site point of contact?

Thank you for your time.

Best regards,
{SIGNATURE}
"""

def sub_quote_request(opp):
    return f"""Subject: Quote request: {opp['title']}

Hello,

I'm preparing a response for a government service contract and would like a price from your company to perform the work as my subcontractor.

Contract: {opp['title']}
Agency: {opp.get('agency', '')}
Location: {opp.get('location', '')}
Link: {opp.get('link', '')}

Please send an annual price (and monthly if possible), the crew size you'd assign, and a copy of your certificate of insurance. I'm happy to share the full specifications.

Thank you,
{SIGNATURE}
"""

def prep_opportunity(opp):
    subs = find_subcontractors(opp.get("service", ""), opp.get("location", ""))
    ss = _is_sources_sought(opp.get("notice_type", ""), opp["title"])
    letter = capability_response(opp) if ss else subcontracting_question(opp)
    label = ("Sources Sought response (no price needed)" if ss
             else "Subcontracting question for the buyer (send before Q&A deadline)")
    lines = [f"## Bid prep: {opp['title']}",
             f"Deadline: {opp.get('deadline', 'see notice')} · {opp.get('link', '')}", "",
             "### Subcontractors to call"]
    if subs:
        for i, s in enumerate(subs, 1):
            em = f" · {s['email']}" if s["email"] else " · email: ask when you call"
            lines.append(f"{i}. **{s['name']}** — {s['phone']}{em}")
            d = s["address"]
            if s["website"]:
                d += " · " + s["website"]
            if s["rating"]:
                d += f" · {s['rating']}★ ({s['reviews']} reviews)"
            lines.append("   " + d)
    else:
        lines.append("No subs found (location not listed or search unavailable). Check the notice for the site location.")
    lines += ["", f"### {label}", "```", letter.strip(), "```", "",
              "### Sub quote request email", "```", sub_quote_request(opp).strip(), "```", ""]
    md = "\n".join(lines)
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"{opp.get('id', 'opportunity')}.md").write_text(md, encoding="utf-8")
    return md

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for f in ("id", "title", "service", "location", "agency", "notice-type", "deadline", "link", "solicitation"):
        ap.add_argument(f"--{f}", default="")
    a = ap.parse_args()
    print(prep_opportunity({k.replace("-", "_"): v for k, v in vars(a).items()}))
