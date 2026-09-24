"""The short, phone-friendly email.

Only NEW or CHANGED top/review items get full cards. Items you've already been sent appear as one line
("Still open") so the same opportunity never fills your inbox twice. No email is sent when nothing
meaningful happened (settings.yaml → email.send_when_empty).

Sending uses SMTP with credentials from environment variables / GitHub Secrets:
  EMAIL_USERNAME, EMAIL_PASSWORD, EMAIL_TO (comma-separated for several people)
  optional: EMAIL_FROM, SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587; 465 = SSL)
Works with Gmail (app password), Outlook/Microsoft 365, or any transactional provider's SMTP relay
(SendGrid, Mailgun, Amazon SES, Postmark, Brevo…).
"""
from __future__ import annotations

import base64
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from core.config import OUTPUT_DIR, Config, env

from .charts import donut_png

log = logging.getLogger(__name__)

INK, MUTED, LINE, PAPER = "#14213D", "#5B6575", "#D5DAE1", "#F3F5F7"
ACCENT = {"top": "#C2410C", "review": "#B7791F", "watch": "#8A94A6"}
FLAG_COLOR = {"red": "#B42318", "orange": "#B7791F", "green": "#2F6B45", "none": MUTED, "expired": MUTED}


def e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


class EmailReport:
    def __init__(self, config: Config):
        self.config = config

    # ── decide ───────────────────────────────────────────────────────
    @staticmethod
    def is_meaningful(report: dict) -> bool:
        return bool(report.get("top") or report.get("review") or report.get("updates")
                    or report.get("all_sources_failed"))           # silence must never hide a broken scanner

    # ── build ────────────────────────────────────────────────────────
    def build(self, report: dict) -> dict:
        date = report["date"]
        date_label = f"{date:%B} {date.day}, {date.year}"
        subject = f"{self.config.get('email.subject_prefix', 'Texas Government Contract Report')} — {date_label}"
        if report.get("is_demo"):
            subject = "[SAMPLE DATA] " + subject
        png = donut_png(report["stats"])
        return {"subject": subject, "html": self._html(report, date_label), "text": self._text(report, date_label),
                "png": png}

    def _html(self, r: dict, date_label: str) -> str:
        site = r.get("site_url") or ""
        top, review = r.get("top", []), r.get("review", [])
        pre = (f"{len(top)} top, {len(review)} worth reviewing" if top or review else "No strong opportunities this cycle")
        parts = [f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{e(date_label)}</title></head>
<body style="margin:0;padding:0;background:{PAPER};">
<div style="display:none;max-height:0;overflow:hidden;">{e(pre)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER};">
<tr><td align="center" style="padding:12px 8px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:{INK};">
<tr><td style="background:{INK};color:#fff;padding:18px 20px;border-radius:10px 10px 0 0;">
<div style="font-size:13px;opacity:.8;">{e(self.config.company.get('legal_name', ''))}</div>
<div style="font-size:20px;font-weight:700;line-height:1.3;">Texas Government Contract Report</div>
<div style="font-size:14px;opacity:.85;">{e(date_label)}</div></td></tr>
<tr><td style="background:#fff;padding:4px 20px 20px;border-radius:0 0 10px 10px;">"""]
        if r.get("is_demo"):
            parts.append(_note("This report uses SAMPLE data to show the format. Nothing here is a real solicitation.",
                               "#B42318"))
        if top:
            parts.append(_h("🔥 TOP OPPORTUNITIES"))
            for i, c in enumerate(top, 1):
                parts.append(self._card(c, f"OPPORTUNITY #{i}", site, full=True))
        else:
            parts.append(f'<p style="font-size:17px;font-weight:700;margin:18px 0 6px;">No strong opportunities found this cycle.</p>')
            if r.get("watch_best"):
                parts.append('<p style="font-size:14px;color:%s;margin:0 0 6px;">Strongest watchlist item:</p>' % MUTED)
                parts.append(self._card(r["watch_best"], "⚪ WATCHLIST", site, full=False))
        if review:
            parts.append(_h("🟡 WORTH REVIEWING"))
            for c in review:
                parts.append(self._card(c, "", site, full=False))
        if r.get("updates"):
            parts.append(_h("🔔 UPDATES ON OPPORTUNITIES YOU'RE TRACKING"))
            for c in r["updates"]:
                notes = "".join(f"<li>{e(n)}</li>" for n in c.get("changes", [])[:4])
                parts.append(f"""<div style="border:1px solid {LINE};border-radius:8px;padding:12px 14px;margin:10px 0;">
<div style="font-weight:700;font-size:15px;">{e(c['title'])}</div>
<div style="font-size:13px;color:{MUTED};">{e(c['id'])} &nbsp; {e(c['deadline']['emoji'])} {e(c['deadline']['label'])}</div>
<ul style="margin:8px 0 4px 18px;padding:0;font-size:14px;">{notes}</ul>
{_link(c['link'], 'Open official solicitation')}</div>""")
        if r.get("still_open"):
            rows = "".join(f'<li style="margin:4px 0;">{e(c["deadline"]["emoji"])} {e(c["service"])} — {e(c["location"])} '
                           f'({e(c["deadline"]["label"])}) <span style="color:{MUTED};">{e(c["id"])}</span></li>'
                           for c in r["still_open"][:6])
            parts.append(f'<p style="font-size:14px;font-weight:700;margin:18px 0 4px;">Still open from earlier reports</p>'
                         f'<ul style="margin:0 0 0 18px;padding:0;font-size:14px;">{rows}</ul>')

        s = r["stats"]
        rows = [("Opportunities found", s.get("found", 0)), ("Passed initial filter", s.get("passed", 0)),
                ("Fully reviewed", s.get("reviewed", 0)), ("🔥 Top opportunities", s.get("top", 0)),
                ("🟡 Worth reviewing", s.get("review", 0)), ("⚪ Watchlist", s.get("watch", 0)),
                ("❌ Filtered", s.get("filtered", 0))]
        if r.get("teaming_leads"):
            rows.append(("🤝 Teaming leads (IDIQ/BPA holders)", r["teaming_leads"]))
        table = "".join(f'<tr><td style="padding:5px 0;border-bottom:1px solid {LINE};font-size:15px;">{e(k)}</td>'
                        f'<td align="right" style="padding:5px 0;border-bottom:1px solid {LINE};font-size:15px;font-weight:700;">{e(v)}</td></tr>'
                        for k, v in rows)
        parts.append(_h("📊 REPORT SUMMARY"))
        parts.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{table}</table>')
        parts.append('<img src="cid:donut" width="100%" alt="Chart: top, worth reviewing, watchlist, and filtered counts" '
                     'style="display:block;max-width:540px;width:100%;height:auto;margin:14px auto 4px;">')
        if r.get("all_sources_failed"):
            parts.append(_note("None of the sources could be read this run, so nothing was searched. Check the "
                               "workflow log and the Where we searched table (a SAM.gov key may have expired).",
                               "#B42318"))
        elif r.get("source_problems"):
            parts.append(_note(f"{r['source_problems']} source(s) couldn't be read automatically this run — see "
                               f"Where we searched on the dashboard.", MUTED))
        if site:
            parts.append(f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0 6px;">
<tr><td align="center" bgcolor="{INK}" style="border-radius:8px;">
<a href="{e(site)}" style="display:block;padding:15px 18px;color:#fff;text-decoration:none;font-weight:700;font-size:16px;letter-spacing:.3px;">VIEW FULL ANALYSIS</a>
</td></tr></table>""")
        parts.append(f"""<p style="font-size:12px;color:{MUTED};line-height:1.5;margin:14px 0 0;">
"Top" means best match to your criteria on the evidence found — not the most likely to win. Confirm every requirement
in the official solicitation, and have pricing and contract terms reviewed by a professional before you commit.
Mark items Pursue, Watch, or Pass on the dashboard.</p>
</td></tr></table></td></tr></table></body></html>""")
        return "".join(parts)

    def _card(self, c: dict, heading: str, site: str, full: bool) -> str:
        accent = ACCENT.get(c.get("category"), MUTED)
        dl = c["deadline"]
        rows = [("Service", c["service"]), ("Location", c["location"]), ("Government", c["government"]),
                ("Contract value", c["value"]),
                ("Deadline", f'<span style="color:{FLAG_COLOR.get(dl["flag"], INK)};font-weight:700;">{e(dl["emoji"])} '
                             f'{e(dl["date"])}</span><br><span style="color:{MUTED};font-size:13px;">{e(dl["label"])}</span>')]
        if c.get("vehicle"):
            rows.insert(1, ("Contract type", c["vehicle"]))
        if full:
            rows += [("Startup", c["startup"]), ("Outsource potential", c["outsource"])]
        body = "".join(
            f'<tr><td valign="top" style="padding:4px 10px 4px 0;font-size:13px;color:{MUTED};width:38%;">{e(k)}</td>'
            f'<td valign="top" style="padding:4px 0;font-size:15px;">{v if k == "Deadline" else e(v)}</td></tr>'
            for k, v in rows)
        extra = f'<p style="margin:10px 0 4px;font-size:13px;color:{MUTED};">Why it made the list</p>' \
                f'<p style="margin:0;font-size:15px;line-height:1.45;">{e(c["why"])}</p>' \
                f'<p style="margin:10px 0 4px;font-size:13px;color:{MUTED};">Biggest risk</p>' \
                f'<p style="margin:0;font-size:15px;line-height:1.45;">{e(c["risk"])}</p>'
        if full:
            todo = "".join(f'<li style="margin:3px 0;">{e(t)}</li>' for t in c["todo"])
            extra += f'<p style="margin:10px 0 4px;font-size:13px;color:{MUTED};">What I need to do</p>' \
                     f'<ol style="margin:0 0 0 20px;padding:0;font-size:15px;line-height:1.45;">{todo}</ol>' \
                     f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;">' \
                     f'<tr><td style="padding:4px 10px 4px 0;font-size:13px;color:{MUTED};width:38%;">Previous winner</td>' \
                     f'<td style="font-size:15px;">{e(c["previous_winner"])}</td></tr>' \
                     f'<tr><td style="padding:4px 10px 4px 0;font-size:13px;color:{MUTED};">Previous award</td>' \
                     f'<td style="font-size:15px;">{e(c["previous_award"])}</td></tr></table>'
        detail = f"{site.rstrip('/')}/#{c['id']}" if site else ""
        links = _link(c["link"], "LINK: Official solicitation") + (
            f' &nbsp;·&nbsp; {_link(detail, "Full analysis")}' if detail else "")
        head = f'<div style="font-size:12px;font-weight:700;color:{accent};letter-spacing:.4px;">{e(heading)}</div>' if heading else ""
        return f"""<div style="border:1px solid {LINE};border-top:4px solid {accent};border-radius:8px;padding:12px 14px;margin:12px 0;">
{head}<div style="font-size:16px;font-weight:700;line-height:1.35;margin:2px 0 8px;">{e(c['title'])}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{body}</table>{extra}
<p style="margin:12px 0 0;font-size:14px;">{links}</p>
<p style="margin:6px 0 0;font-size:12px;color:{MUTED};">{e(c['id'])} &nbsp; {e(c.get('notice', ''))}</p></div>"""

    def _text(self, r: dict, date_label: str) -> str:
        out = [f"Texas Government Contract Report — {date_label}", ""]
        if r.get("is_demo"):
            out += ["*** SAMPLE DATA — not real solicitations ***", ""]
        site = r.get("site_url") or ""

        def block(c, heading, full):
            lines = [heading] if heading else []
            lines += [c["title"], f"Service: {c['service']}"] + ([f"Contract type: {c['vehicle']}"] if c.get("vehicle") else []) + \
                     [f"Location: {c['location']}", f"Government: {c['government']}",
                      f"Contract Value: {c['value']}", f"Deadline: {c['deadline']['emoji']} {c['deadline']['date']} ({c['deadline']['label']})"]
            if full:
                lines += [f"Startup: {c['startup']}", f"Outsource Potential: {c['outsource']}"]
            lines += [f"Why it made the list: {c['why']}", f"Biggest risk: {c['risk']}"]
            if full:
                lines.append("What I need to do:")
                lines += [f"  {i}. {t}" for i, t in enumerate(c["todo"], 1)]
                lines += [f"Previous winner: {c['previous_winner']}", f"Previous award: {c['previous_award']}"]
            lines.append(f"LINK: {c['link']}")
            if site:
                lines.append(f"Full analysis: {site.rstrip('/')}/#{c['id']}")
            return lines + [""]

        if r.get("top"):
            out += ["🔥 TOP OPPORTUNITIES", ""]
            for i, c in enumerate(r["top"], 1):
                out += block(c, f"OPPORTUNITY #{i}", True)
        else:
            out += ["No strong opportunities found this cycle.", ""]
            if r.get("watch_best"):
                out += block(r["watch_best"], "Strongest watchlist item:", False)
        if r.get("review"):
            out += ["🟡 WORTH REVIEWING", ""]
            for c in r["review"]:
                out += block(c, "", False)
        if r.get("updates"):
            out += ["🔔 UPDATES ON OPPORTUNITIES YOU'RE TRACKING"]
            for c in r["updates"]:
                out += [f"- {c['title']} ({c['id']})"] + [f"    {n}" for n in c.get("changes", [])[:4]]
            out.append("")
        if r.get("still_open"):
            out += ["Still open from earlier reports:"]
            out += [f"- {c['deadline']['emoji']} {c['service']} — {c['location']} ({c['deadline']['label']}) {c['id']}"
                    for c in r["still_open"][:6]]
            out.append("")
        s = r["stats"]
        out += ["📊 REPORT SUMMARY", f"Opportunities Found: {s.get('found', 0)}", f"Passed Initial Filter: {s.get('passed', 0)}",
                f"Fully Reviewed: {s.get('reviewed', 0)}", f"🔥 Top Opportunities: {s.get('top', 0)}",
                f"🟡 Worth Reviewing: {s.get('review', 0)}", f"⚪ Watchlist: {s.get('watch', 0)}",
                f"❌ Filtered: {s.get('filtered', 0)}"] + \
               ([f"🤝 Teaming leads (IDIQ/BPA holders): {r['teaming_leads']}"] if r.get("teaming_leads") else []) + [""]
        if r.get("all_sources_failed"):
            out += ["⚠️ None of the sources could be read this run. Check the workflow log (a SAM.gov key may have expired).", ""]
        if site:
            out += [f"VIEW FULL ANALYSIS: {site}", ""]
        out.append("Top = best match to your criteria, not most likely to win. Verify everything in the official solicitation.")
        return "\n".join(out)

    # ── output ───────────────────────────────────────────────────────
    def save_preview(self, msg: dict, out_dir=None) -> str:
        out_dir = out_dir or OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        data_uri = "data:image/png;base64," + base64.b64encode(msg["png"]).decode()
        path = out_dir / "last_email.html"
        path.write_text(msg["html"].replace("cid:donut", data_uri), encoding="utf-8")
        (out_dir / "last_email.txt").write_text(msg["subject"] + "\n\n" + msg["text"], encoding="utf-8")
        return str(path)

    def send(self, msg: dict) -> tuple[bool, str]:
        user, password, to = env("EMAIL_USERNAME"), env("EMAIL_PASSWORD"), env("EMAIL_TO")
        if not (user and password and to):
            return False, "Email not sent: EMAIL_USERNAME, EMAIL_PASSWORD, and EMAIL_TO must all be set."
        recipients = [x.strip() for x in to.replace(";", ",").split(",") if x.strip()]
        sender = env("EMAIL_FROM", user)
        host, port = env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "587"))

        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = msg["subject"], sender, ", ".join(recipients)
        m["Date"] = formatdate(localtime=True)
        m["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
        m.set_content(msg["text"])
        m.add_alternative(msg["html"], subtype="html")
        m.get_payload()[1].add_related(msg["png"], maintype="image", subtype="png", cid="<donut>",
                                       filename="summary.png")
        try:
            context = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as s:
                    s.login(user, password)
                    s.send_message(m)
            else:
                with smtplib.SMTP(host, port, timeout=30) as s:
                    s.starttls(context=context)
                    s.login(user, password)
                    s.send_message(m)
            return True, f"Email sent to {len(recipients)} recipient(s)."
        except Exception as exc:
            log.error("Email failed: %s", exc)
            return False, f"Email failed: {exc.__class__.__name__}: {exc}"


def _h(text: str) -> str:
    return f'<p style="font-size:15px;font-weight:800;letter-spacing:.4px;margin:22px 0 4px;">{e(text)}</p>'


def _note(text: str, color: str) -> str:
    return f'<p style="font-size:13px;color:{color};margin:12px 0 0;">{e(text)}</p>'


def _link(url: str, label: str) -> str:
    if not url:
        return f'<span style="color:{MUTED};">{e(label)} (no link)</span>'
    return f'<a href="{e(url)}" style="color:#1D4ED8;font-weight:600;">{e(label)}</a>'
