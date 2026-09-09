"""
Ship-Bot chat card builder.

Renders the shipment summary as a real Lark interactive card instead of one
long markdown blob:

    +--------------------------------------------------+
    | 📦 HLT Shipment Tracker                          |
    |  🟢 12 delivered  🔵 41 in transit  ⚪ 12 …      |
    |  [ All ] [ Flagged ] [ In transit ] [ Not scanned]|
    |  [ Filter by client            v ]                |
    |  [ ✓ Mark as delivered         v ]                |
    |  ---                                              |
    |  Hannah                                           |
    |   🔴 HLT-SO6583 · Half Evil · Customs delay · FedEx|
    |  ---                                              |
    |  7Brew Coffee                                     |
    |   🔵 HLT-SO6566 · Import scan, Louisville KY · UPS|
    +--------------------------------------------------+

A section is a BOOK OF BUSINESS: the shared inbound sheets stay as Hannah /
Lucy / Other, and each dedicated client sheet is its own section, so Hannah
and Lucy read as clients in their own right. The customer for each row is
shown on the line. The status buttons and the client dropdown are wired to
`card.action.trigger`; the webhook re-renders this card from the latest scan
snapshot and returns it, so filtering is instant and costs no carrier calls.

"Mark as delivered" writes Delivered + today's date back to the sheet, for
the shipments carriers never post a final scan for.

The card is data-only -- it never calls Lark or the carriers -- so it is
cheap to build, easy to unit test, and safe to call from the webhook thread.
"""

import re
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from config import SHEET_OWNERS

logger = logging.getLogger(__name__)

# --- glyphs -----------------------------------------------------------------
DOT_DELIVERED = "\U0001F7E2"   # green
DOT_TRANSIT = "\U0001F535"     # blue
DOT_UNSCANNED = "⚪"       # white
DOT_FLAGGED = "\U0001F534"     # red
SEP = "·"                 # middle dot

# Staff names that prefix a client sheet title ("2026 HANNAH 7BREW ...").
# Stripped so the card groups by CLIENT, not by who owns the sheet.
STAFF_NAMES = {"hannah", "lucy", "chen", "korey", "gabrielle", "brieanne",
               "elle", "brendan", "other"}

# Status buckets
FLAGGED = "flagged"        # exception, customs hold, refused, returned
ARRIVING = "arriving"      # out for delivery / landing today
TRANSIT = "transit"        # moving normally
UNSCANNED = "unscanned"    # label created, carrier hasn't scanned it
DELIVERED = "delivered"    # done -- counted in the header, not listed

BUCKET_DOT = {
    FLAGGED: DOT_FLAGGED,
    ARRIVING: DOT_DELIVERED,
    TRANSIT: DOT_TRANSIT,
    UNSCANNED: DOT_UNSCANNED,
    DELIVERED: DOT_DELIVERED,
}

# Order shipments appear inside a client section
BUCKET_ORDER = {FLAGGED: 0, ARRIVING: 1, TRANSIT: 2, UNSCANNED: 3, DELIVERED: 4}

# Which buckets each status tab keeps
TAB_BUCKETS = {
    "all": {FLAGGED, ARRIVING, TRANSIT, UNSCANNED},
    FLAGGED: {FLAGGED},
    TRANSIT: {ARRIVING, TRANSIT},
    UNSCANNED: {UNSCANNED},
}

STATUS_TABS = [
    ("all", "All"),
    (FLAGGED, "Flagged"),
    (TRANSIT, "In transit"),
    (UNSCANNED, "Not scanned"),
]

MAX_LINES_PER_CLIENT = 12
MAX_CLIENTS = 12

# An unscanned label this many days past its own ETA counts as flagged --
# and gets escalated to the urgent channel by stuck_detector.
OVERDUE_DAYS = 1

# Carrier boilerplate trimmed out of exception text
NOISE_PHRASES = [
    "We will deliver your package as soon as possible.",
    "We are experiencing transit delays.",
    "Your package will be delivered on the scheduled delivery date.",
    "This does not impact the scheduled delivery date.",
]

# Carriers write paragraphs; the card has one line. These are the sentences
# that turn up over and over, rewritten to what someone actually needs to know.
SHORTHAND = [
    ("forwarded to a ups facility in the destination city",
     "Forwarded to destination facility"),
    ("the package will be forwarded to", "Forwarded to"),
    ("shipment information sent to", "Label created, awaiting"),
    ("departed from facility", "Departed"),
    ("arrived at facility", "Arrived"),
    ("loaded on delivery vehicle", "On the truck"),
    ("in transit to next facility", "In transit"),
    ("customs clearance delay", "Customs delay"),
    ("held for payment of duties", "Held for duties"),
    ("processing at ups facility", "At UPS facility"),
]
MAX_DETAIL_CHARS = 58

# Carrier names as people write them, not as the sheet shouts them
CARRIER_DISPLAY = {
    "FEDEX": "FedEx", "FED EX": "FedEx", "UPS": "UPS", "USPS": "USPS",
    "DHL": "DHL", "ROYALMAIL": "Royal Mail", "ROYAL MAIL": "Royal Mail",
    "SFEXPRESS": "SF Express", "SF": "SF Express", "DPD": "DPD",
    "UNIUNI": "UniUni", "4PX": "4PX", "FOURPX": "4PX", "FIRST": "1ST",
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _parse_date(raw):
    clean = str(raw or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(clean, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def days_overdue(r):
    """How many days past its own ETA an unscanned shipment is."""
    eta = _parse_date(r.get("delivery_date"))
    if not eta:
        return 0
    return max(0, (_now_et().date() - eta).days)


def bucket_for(r):
    """Map a tracked row onto one of the card buckets."""
    status = (r.get("new_status") or r.get("current_status") or "").upper()
    raw = (r.get("raw_status") or "").upper()

    if "EXCEPTION" in status or "DELAY" in status:
        return FLAGGED
    for kw in ("CUSTOMS", "CLEARANCE", "HELD", "RETURNED", "REFUSED",
               "ADDRESS CORRECT"):
        if kw in raw:
            return FLAGGED
    if "OUT FOR DELIVERY" in raw or "ON VEHICLE" in raw:
        return ARRIVING
    if "DELIVER" in status:
        return DELIVERED

    packages = r.get("packages") or []
    unscanned = ("LABEL CREATED" in status or "NOT SCANNED" in status
                 or (packages and not any(p.get("scanned") for p in packages)))
    if unscanned:
        # A label cut two weeks ago whose ETA has already passed is not
        # "waiting to be scanned" -- it's a problem nobody has looked at.
        return FLAGGED if days_overdue(r) >= OVERDUE_DAYS else UNSCANNED
    return TRANSIT


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or "unknown"


def _strip_staff(name):
    """'Hannah 7Brew Coffee' -> '7Brew Coffee';  'Hannah' -> ''."""
    words = [w for w in (name or "").split() if w.lower() not in STAFF_NAMES]
    # "MFused 副本" and friends: a duplicated sheet is not a separate client.
    words = [w for w in words if w.lower() not in COPY_MARKERS]
    return " ".join(words).strip()


# Sheet-title noise that marks a duplicate, not a client
COPY_MARKERS = {"副本", "copy", "(copy)", "copy)", "backup", "old", "test"}

PLACEHOLDER_CUSTOMERS = {"", "-", "--", "N/A", "NA", "NONE", "UNKNOWN", "TBD",
                         "CUSTOMER DIRECT", "BRENDAN"}


def raw_client_for(r):
    """The client name exactly as this row spells it.

    Client sheets carry the client in the sheet title; the shared HANNAH /
    LUCY / OTHER sheets carry it in the Customer column -- often as two
    lines, "Contact name" over "Company", in which case the company wins.
    """
    owner = SHEET_OWNERS.get((r.get("sheet_token") or "").strip(), "")
    from_sheet = _strip_staff(owner)
    if from_sheet:
        return from_sheet, True  # authoritative: the sheet IS the client

    customer = (r.get("customer") or "").strip()
    lines = [l.strip() for l in re.split(r"[\r\n]+", customer) if l.strip()]
    if len(lines) > 1:
        customer = lines[-1]          # "Mike Christman / Spectrum Promo"
    elif lines:
        customer = lines[0]

    if customer and customer.upper() not in PLACEHOLDER_CUSTOMERS:
        return (customer if customer != customer.upper() else customer.title()), False

    recipient = (r.get("recipient") or "").strip()
    if recipient and recipient.upper() not in PLACEHOLDER_CUSTOMERS:
        return recipient.title(), False
    return "Unassigned", False


def _stem(name):
    """Collapsed comparison key: '7 Brew (MKE)' -> '7brewmke'."""
    base = re.sub(r"\(.*?\)", " ", name or "")     # drop "(MKE)", "(1)"
    return re.sub(r"[^a-z0-9]+", "", base.lower())


def _same_client(a, b, min_stem=5):
    """True when two spellings are the same client.

    '7Brew Coffee' / '7 Brew (MKE)' share the stem '7brew';
    'Craftworks' / 'Craftworks Design' share 'craftworks'.
    """
    sa, sb = _stem(a), _stem(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    short, long_ = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return len(short) >= min_stem and long_.startswith(short)


def build_client_map(results):
    """Map every spelling in this scan to one canonical client name.

    Names taken from a client sheet win, because that's the spelling the
    team already uses for the account; otherwise the fullest spelling wins.
    """
    seen = {}   # raw name -> authoritative?
    for r in results or []:
        name, authoritative = raw_client_for(r)
        seen[name] = seen.get(name, False) or authoritative

    canonical = {}
    groups = []   # [(canonical_name, authoritative, [members])]
    for name in sorted(seen, key=lambda n: (not seen[n], -len(n), n)):
        for g in groups:
            if _same_client(g[0], name):
                g[2].append(name)
                break
        else:
            groups.append((name, seen[name], [name]))

    for canon, _auth, members in groups:
        for m in members:
            canonical[m] = canon
    return canonical


def client_for(r, client_map=None):
    name = raw_client_for(r)[0]
    return (client_map or {}).get(name, name)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
# A section is a book of business, not a single customer: the shared inbound
# sheets stay as Hannah / Lucy / Other, and each dedicated client sheet is its
# own section. That way Hannah and Lucy read as clients in their own right and
# the dropdown filters between them.

PERMANENT_SECTIONS = ["Hannah", "Lucy", "Other"]


def section_for(r):
    owner = SHEET_OWNERS.get((r.get("sheet_token") or "").strip(), "") or "Other"
    return _strip_staff(owner) or owner


def line_name(r, section):
    """Who the shipment is for, shown on the line under a section header."""
    name = raw_client_for(r)[0]
    if not name or name == "Unassigned" or _same_client(name, section):
        return ""
    return name


def group_by_section(results):
    """-> OrderedDict {section: [rows]}, permanent books first."""
    groups = {}
    for r in results:
        groups.setdefault(section_for(r), []).append(r)

    def sort_key(item):
        name, rows = item
        if name in PERMANENT_SECTIONS:
            return (0, PERMANENT_SECTIONS.index(name), "")
        flagged = sum(1 for r in rows if bucket_for(r) == FLAGGED)
        return (1, -flagged, name.lower())

    ordered = OrderedDict()
    for name, rows in sorted(groups.items(), key=sort_key):
        ordered[name] = sorted(
            rows, key=lambda r: (BUCKET_ORDER[bucket_for(r)], _ref(r))
        )
    return ordered


def mark_delivered_value(r):
    """Opaque handle a 'mark delivered' option sends back to the webhook."""
    return "md|%s|%s|%s" % (r.get("sheet_token", ""), r.get("row_num", ""),
                            (r.get("tab") or "")[:24])


def parse_delivered_value(value):
    parts = (value or "").split("|")
    if len(parts) != 4 or parts[0] != "md":
        return None
    token, row, tab = parts[1], parts[2], parts[3]
    if not token or not str(row).isdigit():
        return None
    return {"sheet_token": token, "row_num": int(row), "tab": tab}


# ---------------------------------------------------------------------------
# Line rendering
# ---------------------------------------------------------------------------

def _tracking_url(tracking, carrier):
    c = (carrier or "").strip().upper()
    if c.startswith("FED"):
        return "https://www.fedex.com/fedextrack/?trknbr=%s" % tracking
    if c == "UPS":
        return "https://www.ups.com/track?tracknum=%s" % tracking
    if c == "USPS":
        return "https://tools.usps.com/go/TrackConfirmAction?tLabels=%s" % tracking
    if c.startswith("DHL"):
        return "https://www.dhl.com/en/express/tracking.html?AWB=%s" % tracking
    return ""


def _short_date(raw):
    """'2026-09-08' -> 'Sep 8'; today/tomorrow get words instead."""
    if not raw:
        return ""
    clean = str(raw).strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            dt = datetime.strptime(clean, fmt).date()
        except (ValueError, TypeError):
            continue
        today = datetime.now(timezone(timedelta(hours=-4))).date()
        if dt == today:
            return "Today"
        if dt == today + timedelta(days=1):
            return "Tomorrow"
        return "%s %d" % (dt.strftime("%b"), dt.day)
    return clean


def _detail(r, bucket):
    """The human-readable middle of a shipment line."""
    raw = (r.get("raw_status") or "").strip()
    location = (r.get("location") or "").strip()
    city = location.replace(" - ", ",").split(",")[0].strip() if location else ""

    overdue = days_overdue(r)
    unscanned_text = (
        "Label created %d days ago, never scanned" % overdue
        if overdue >= OVERDUE_DAYS else "Label created, not yet scanned"
    )

    status = (r.get("new_status") or "").upper()
    if raw and not ("LABEL CREATED" in status or "NOT SCANNED" in status):
        text = _tidy(raw)
    elif "LABEL CREATED" in status or "NOT SCANNED" in status:
        text = unscanned_text
    elif bucket in (DELIVERED, ARRIVING):
        text = "Delivered" if bucket == DELIVERED else "Out for delivery"
    elif bucket == FLAGGED:
        text = "Exception"
    else:
        text = "In transit"

    # "US" / "GB" alone is a country code, not a place worth appending.
    if len(city) <= 3 and city.isupper():
        city = ""
    if city and city.lower() not in text.lower():
        text = "%s, %s" % (text, city)
    return text


def _tidy(raw):
    """Trim carrier boilerplate and cap the length of a status blurb."""
    text = raw.strip()
    for phrase in NOISE_PHRASES:
        text = text.replace(phrase, "")
    lowered = text.lower()
    for needle, short in SHORTHAND:
        idx = lowered.find(needle)
        if idx != -1:
            text = short + text[idx + len(needle):]
            break
    text = re.sub(r"\s*/\s*", " / ", text)
    text = re.sub(r"\s+", " ", text).strip(" /")
    if len(text) > MAX_DETAIL_CHARS:
        cut = text[:MAX_DETAIL_CHARS].rsplit(" ", 1)[0]
        text = cut.rstrip(".,;/ ") + "…"
    text = text.rstrip(". ")
    return text[:1].upper() + text[1:] if text else text


def _ref(r):
    """Shipment reference shown in bold at the head of the line."""
    sid = (r.get("shipment_id") or "").strip()
    if sid:
        return sid
    order = (r.get("order_num") or "").strip()
    if order:
        return order
    return (r.get("tracking_num") or "").strip() or "No tracking #"


def shipment_line(r, bucket=None, section=""):
    bucket = bucket or bucket_for(r)
    tracking = (r.get("tracking_num") or "").strip()
    carrier = (r.get("carrier") or "").strip().upper()
    url = _tracking_url(tracking, carrier)

    ref = _ref(r)
    ref_md = "[**%s**](%s)" % (ref, url) if url else "**%s**" % ref

    parts = [BUCKET_DOT[bucket] + " " + ref_md]
    name = line_name(r, section)
    if name:
        parts.append(name)
    parts.append(_detail(r, bucket))
    if carrier:
        # Unknown short all-caps codes (EWS, GLS) stay as written.
        pretty = CARRIER_DISPLAY.get(
            carrier, carrier if len(carrier) <= 5 else carrier.title())
        parts.append("%s" % pretty)

    date = _short_date(r.get("delivery_date"))
    if date:
        parts.append("**%s**" % date if date in ("Today", "Tomorrow") else date)

    boxes = str(r.get("num_boxes") or "").strip()
    packages = r.get("packages") or []
    n_boxes = len(packages) if len(packages) > 1 else (
        int(boxes) if boxes.isdigit() and int(boxes) > 1 else 0
    )
    if n_boxes:
        parts.append("%d boxes" % n_boxes)

    return (" %s " % SEP).join(parts)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def dedupe(results):
    """One line per tracking number (sheets repeat it across item rows)."""
    seen, unique = set(), []
    for r in results or []:
        tn = (r.get("tracking_num") or "").strip()
        key = tn or "row:%s:%s" % (r.get("sheet_token"), r.get("row_num"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def group_by_client(results, client_map=None):
    """-> OrderedDict {client: [rows]} sorted by urgency then size."""
    if client_map is None:
        client_map = build_client_map(results)
    groups = {}
    for r in results:
        groups.setdefault(client_for(r, client_map), []).append(r)

    def sort_key(item):
        client, rows = item
        flagged = sum(1 for r in rows if bucket_for(r) == FLAGGED)
        return (-flagged, -len(rows), client.lower())

    ordered = OrderedDict()
    for client, rows in sorted(groups.items(), key=sort_key):
        ordered[client] = sorted(
            rows, key=lambda r: (BUCKET_ORDER[bucket_for(r)], _ref(r))
        )
    return ordered


def counts(results):
    out = {FLAGGED: 0, ARRIVING: 0, TRANSIT: 0, UNSCANNED: 0, DELIVERED: 0}
    for r in results:
        out[bucket_for(r)] += 1
    return out


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

def _now_et():
    return datetime.now(timezone(timedelta(hours=-4)))


def _delivered_label(r):
    """Short, unambiguous label for the 'mark as delivered' dropdown."""
    bits = [_ref(r)]
    name = line_name(r, "")
    if name:
        bits.append(name)
    carrier = (r.get("carrier") or "").strip().upper()
    if carrier and len(bits) < 2:
        bits.append(CARRIER_DISPLAY.get(carrier, carrier))
    return (" %s " % SEP).join(bits)[:70]


def build_tracker_card(results, client="all", status="all", sheet_count=None):
    """Build the Lark interactive card JSON (dict).

    `client` is a slug from the dropdown, `status` one of the tab keys.
    Both default to "all".
    """
    rows = dedupe(results or [])
    totals = counts(rows)

    # Delivered shipments are counted in the header but never listed --
    # nobody needs to read a line about a box that already arrived.
    listable = [r for r in rows if bucket_for(r) != DELIVERED]
    all_sections = list(group_by_section(listable).keys())

    keep = TAB_BUCKETS.get(status or "all", TAB_BUCKETS["all"])
    filtered = [r for r in listable if bucket_for(r) in keep]
    if client and client != "all":
        filtered = [r for r in filtered if slugify(section_for(r)) == client]

    grouped = group_by_section(filtered)

    summary = "   ".join([
        "%s **%d** delivered" % (DOT_DELIVERED, totals[DELIVERED]),
        "%s **%d** in transit" % (DOT_TRANSIT, totals[TRANSIT] + totals[ARRIVING]),
        "%s **%d** not scanned" % (DOT_UNSCANNED, totals[UNSCANNED]),
        "%s **%d** flagged" % (DOT_FLAGGED, totals[FLAGGED]),
    ])

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "primary" if key == status else "default",
                    "value": {"action": "status_filter", "status": key,
                              "client": client},
                }
                for key, label in STATUS_TABS
            ],
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "select_static",
                    "placeholder": {"tag": "plain_text",
                                    "content": "Filter by client"},
                    "initial_option": client if client != "all" else "all",
                    "value": {"action": "client_filter", "status": status},
                    "options": [
                        {"text": {"tag": "plain_text", "content": "All clients"},
                         "value": "all"}
                    ] + [
                        {"text": {"tag": "plain_text", "content": name},
                         "value": slugify(name)}
                        for name in all_sections
                    ],
                }
            ],
        },
    ]

    # Manual override: mark something delivered when the carrier never will.
    # Scoped to what's on screen, so filtering to a client shortens the list.
    if filtered:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "select_static",
                "placeholder": {"tag": "plain_text",
                                "content": "✓ Mark as delivered"},
                "value": {"action": "mark_delivered", "status": status,
                          "client": client},
                "options": [
                    {"text": {"tag": "plain_text",
                              "content": _delivered_label(r)},
                     "value": mark_delivered_value(r)}
                    for r in filtered[:50]
                ],
            }],
        })
    else:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": "_Nothing matches that filter right now._"},
        })

    for name in list(grouped)[:MAX_CLIENTS]:
        section_rows = grouped[name]
        shown = section_rows[:MAX_LINES_PER_CLIENT]
        body = "\n".join(shipment_line(r, section=name) for r in shown)
        overflow = len(section_rows) - len(shown)
        if overflow > 0:
            body += "\n_+%d more in %s_" % (overflow, name)
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**%s**" % name},
        })
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})

    hidden = max(0, len(grouped) - MAX_CLIENTS)
    note_bits = ["Updated %s ET" % _now_et().strftime("%b %-d, %-I:%M %p")]
    if sheet_count:
        note_bits.append("%d sheets" % sheet_count)
    note_bits.append("%d open shipments" % len(listable))
    if hidden:
        note_bits.append("%d more sections hidden -- use the filter" % hidden)

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": (" %s " % SEP).join(note_bits)}],
    })

    header_title = "\U0001F4E6 HLT Shipment Tracker"
    if client != "all":
        label = next((c for c in all_sections if slugify(c) == client), None)
        if label:
            header_title += " %s %s" % (SEP, label)

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "red" if totals[FLAGGED] else "blue",
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": elements,
    }


def plain_text_fallback(results):
    """Text version used when the card API rejects the payload."""
    rows = [r for r in dedupe(results or []) if bucket_for(r) != DELIVERED]
    if not rows:
        return "All shipments delivered. Nothing to track."
    totals = counts(rows)
    lines = ["HLT Shipment Tracker",
             "%d in transit, %d not scanned, %d flagged"
             % (totals[TRANSIT] + totals[ARRIVING], totals[UNSCANNED],
                totals[FLAGGED])]
    for name, section_rows in group_by_section(rows).items():
        lines.append("")
        lines.append(name)
        for r in section_rows[:MAX_LINES_PER_CLIENT]:
            b = bucket_for(r)
            who = line_name(r, name)
            lines.append("  %s %s%s %s %s" % (
                BUCKET_DOT[b], _ref(r), (" " + SEP + " " + who) if who else "",
                SEP, _detail(r, b)))
    return "\n".join(lines)


# ===========================================================================
# Card JSON 2.0 -- the dashboard layout
# ===========================================================================
# Card 1.0 can only stack markdown lines, which is why the first version read
# like a list. Card 2.0 gives us a real table component (typed columns, tag
# pills, paging) plus filled tab buttons -- the same shape as the Fulfillment
# screen in the client portal:
#
#   Shipment Tracker                                    [8 flagged]
#   24 open  ·  14 in transit  ·  2 not scanned  ·  8 flagged
#   [ All 24 ][ Hannah 13 ][ Lucy 1 ][ Other 5 ][ 7Brew Coffee 2 ] ...
#   ┌────────────┬──────────────┬────────────┬───────────┬────────┬───────┐
#   │ SHIPMENT   │ CLIENT       │ STATUS     │ DETAIL    │CARRIER │ ETA   │
#   ├────────────┼──────────────┼────────────┼───────────┼────────┼───────┤
#   │ HLT-SO6638 │ Craftworks   │ ● Flagged  │ Forwarded │ UPS    │ Today │
#   └────────────┴──────────────┴────────────┴───────────┴────────┴───────┘
#   [ All statuses v ]            [ ✓ Mark as delivered v ]
#
# Tabs are the books of business (All / Hannah / Lucy / Other / each client
# sheet), exactly the filter order asked for.

TAG_COLOR = {
    FLAGGED: "red",
    ARRIVING: "green",
    TRANSIT: "blue",
    UNSCANNED: "grey",
    DELIVERED: "green",
}

TAG_TEXT = {
    FLAGGED: "Flagged",
    ARRIVING: "Out for delivery",
    TRANSIT: "In transit",
    UNSCANNED: "Not scanned",
    DELIVERED: "Delivered",
}

STATUS_MENU = [
    ("all", "All statuses"),
    (FLAGGED, "Flagged only"),
    (TRANSIT, "In transit"),
    (UNSCANNED, "Not scanned"),
]

MAX_TABS = 14

# Filter order as asked for: All, Not scanned, In transit, Flagged
STATUS_TABS_V2 = [
    ("all", "All"),
    (UNSCANNED, "Not scanned"),
    (TRANSIT, "In transit"),
    (FLAGGED, "Flagged"),
]


def _kpi_tile(value, label, color=""):
    number = "**%s**" % value
    if color:
        number = "<font color='%s'>%s</font>" % (color, number)
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 1,
        "vertical_align": "top",
        "elements": [
            {"tag": "markdown", "text_size": "heading", "content": number},
            {"tag": "markdown", "content": "<font color='grey'>%s</font>" % label},
        ],
    }


def _tab_button(label, count, value, active):
    return {
        "tag": "button",
        "size": "small",
        "type": "primary_filled" if active else "text",
        "text": {"tag": "plain_text", "content": "%s  %d" % (label, count)},
        "behaviors": [{"type": "callback", "value": value}],
    }


def _table_row(r):
    bucket = bucket_for(r)
    tracking = (r.get("tracking_num") or "").strip()
    carrier = (r.get("carrier") or "").strip().upper()
    url = _tracking_url(tracking, carrier)
    ref = _ref(r)

    eta = _short_date(r.get("delivery_date"))
    overdue = days_overdue(r)
    if bucket == FLAGGED and overdue >= OVERDUE_DAYS and eta:
        eta = "%s (%dd late)" % (eta, overdue)

    return {
        "shipment": "[%s](%s)" % (ref, url) if url else ref,
        "client": line_name(r, "") or "—",
        "status": [{"text": TAG_TEXT[bucket], "color": TAG_COLOR[bucket]}],
        "detail": _detail(r, bucket),
        "carrier": (CARRIER_DISPLAY.get(carrier, carrier if len(carrier) <= 5
                                        else carrier.title()) or "—"),
        "eta": eta or "—",
    }


TABLE_COLUMNS = [
    {"name": "shipment", "display_name": "SHIPMENT", "data_type": "lark_md",
     "width": "150px"},
    {"name": "client", "display_name": "CLIENT", "data_type": "text",
     "width": "150px"},
    {"name": "status", "display_name": "STATUS", "data_type": "options",
     "width": "130px"},
    {"name": "detail", "display_name": "DETAIL", "data_type": "text",
     "width": "auto"},
    {"name": "carrier", "display_name": "CARRIER", "data_type": "text",
     "width": "90px"},
    {"name": "eta", "display_name": "ETA", "data_type": "text",
     "width": "120px"},
]


def build_tracker_card_v2(results, client="all", status="all", sheet_count=None):
    """The chat card: counts, one row of controls, then client sections.

    Layout follows the demo card -- a counts line, the filters, and grouped
    shipment lines -- with the status filter and the client dropdown sharing
    a single row instead of stacking. The searchable, per-row-actionable view
    lives on the dashboard; this stays a summary you can read at a glance.
    """
    rows = dedupe(results or [])
    totals = counts(rows)
    listable = [r for r in rows if bucket_for(r) != DELIVERED]
    sections = group_by_section(listable)

    keep = TAB_BUCKETS.get(status or "all", TAB_BUCKETS["all"])
    filtered = [r for r in listable if bucket_for(r) in keep]
    if client and client != "all":
        filtered = [r for r in filtered if slugify(section_for(r)) == client]
    grouped = group_by_section(filtered)

    per_status = {}
    for r in listable:
        per_status[bucket_for(r)] = per_status.get(bucket_for(r), 0) + 1

    # --- counts line -------------------------------------------------------
    summary = "   ".join([
        "%s **%d** delivered" % (DOT_DELIVERED, totals[DELIVERED]),
        "%s **%d** in transit" % (DOT_TRANSIT, totals[TRANSIT] + totals[ARRIVING]),
        "%s **%d** not scanned" % (DOT_UNSCANNED, totals[UNSCANNED]),
        "%s **%d** flagged" % (DOT_FLAGGED, totals[FLAGGED]),
    ])
    elements = [{"tag": "markdown", "content": summary}]

    # --- one control row: status buttons + client dropdown -----------------
    def _count_for(key):
        if key == "all":
            return len(listable)
        return sum(per_status.get(b, 0) for b in TAB_BUCKETS.get(key, ()))

    controls = []
    for key, label in STATUS_TABS_V2:
        controls.append({
            "tag": "column", "width": "auto", "vertical_align": "center",
            "elements": [{
                "tag": "button",
                "size": "small",
                "type": "primary_filled" if key == status else "text",
                "text": {"tag": "plain_text",
                         "content": "%s  %d" % (label, _count_for(key))},
                "behaviors": [{"type": "callback",
                               "value": {"action": "status_filter",
                                         "status": key, "client": client}}],
            }],
        })
    controls.append({
        "tag": "column", "width": "auto", "vertical_align": "center",
        "elements": [{
            "tag": "select_static",
            "initial_option": client,
            "placeholder": {"tag": "plain_text", "content": "All clients"},
            "behaviors": [{"type": "callback",
                           "value": {"action": "client_filter",
                                     "status": status}}],
            "options": [{"text": {"tag": "plain_text", "content": "All clients"},
                         "value": "all"}] + [
                {"text": {"tag": "plain_text",
                          "content": "%s (%d)" % (name, len(sections[name]))},
                 "value": slugify(name)}
                for name in list(sections)[:MAX_TABS]
            ],
        }],
    })
    elements.append({
        "tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "4px",
        "horizontal_align": "left", "margin": "8px 0px 4px 0px",
        "columns": controls,
    })

    # --- client sections ---------------------------------------------------
    if not filtered:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown",
                         "content": "<font color='grey'>Nothing here right "
                                    "now.</font>"})

    for name in list(grouped)[:MAX_CLIENTS]:
        section_rows = grouped[name]
        shown = section_rows[:MAX_LINES_PER_CLIENT]
        body = "\n".join(shipment_line(r, section=name) for r in shown)
        overflow = len(section_rows) - len(shown)
        if overflow > 0:
            body += "\n<font color='grey'>+%d more in %s</font>" % (overflow, name)
        elements.append({"tag": "hr", "margin": "6px 0px"})
        elements.append({"tag": "markdown", "content": "**%s**" % name})
        elements.append({"tag": "markdown", "content": body})

    # --- footer: mark delivered + dashboard --------------------------------
    footer = []
    if filtered:
        footer.append({
            "tag": "column", "width": "auto", "vertical_align": "center",
            "elements": [{
                "tag": "select_static",
                "placeholder": {"tag": "plain_text",
                                "content": "\u2713 Mark as delivered"},
                "behaviors": [{"type": "callback",
                               "value": {"action": "mark_delivered",
                                         "client": client, "status": status}}],
                "options": [{"text": {"tag": "plain_text",
                                      "content": _delivered_label(r)},
                             "value": mark_delivered_value(r)}
                            for r in filtered[:50]],
            }],
        })
    try:
        import dashboard
        # AppLink-wrapped so it opens in the Lark sidebar, not a browser.
        deep_link = dashboard.lark_link()
    except Exception:
        deep_link = ""
    if deep_link:
        footer.append({
            "tag": "column", "width": "auto", "vertical_align": "center",
            "elements": [{
                "tag": "button",
                "size": "small",
                "type": "primary_filled",
                "text": {"tag": "plain_text", "content": "Open dashboard"},
                "behaviors": [{"type": "open_url", "default_url": deep_link}],
            }],
        })
    if footer:
        elements.append({"tag": "hr", "margin": "8px 0px 6px 0px"})
        elements.append({"tag": "column_set", "flex_mode": "flow",
                         "horizontal_spacing": "8px", "columns": footer})

    hidden = max(0, len(grouped) - MAX_CLIENTS)
    note_bits = ["Updated %s ET" % _now_et().strftime("%b %-d, %-I:%M %p")]
    if sheet_count:
        note_bits.append("%d sheets" % sheet_count)
    note_bits.append("%d open" % len(listable))
    if hidden:
        note_bits.append("%d more clients -- use the dropdown" % hidden)
    elements.append({"tag": "note",
                     "elements": [{"tag": "plain_text",
                                   "content": (" %s " % SEP).join(note_bits)}]})

    header = {
        "title": {"tag": "plain_text",
                  "content": "\U0001F4E6 HLT Shipment Tracker"},
        "template": "red" if totals[FLAGGED] else "blue",
    }
    if client != "all":
        label = next((n for n in sections if slugify(n) == client), "")
        if label:
            header["subtitle"] = {"tag": "plain_text", "content": label}

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": header,
        "body": {"direction": "vertical", "padding": "12px 12px 12px 12px",
                 "elements": elements},
    }


# =========================================================================
# Dashboard payload
# =========================================================================
# The web dashboard and the /api/shipments endpoint both consume this shape.
# Keeping it here means the dashboard classifies shipments exactly the way
# the chat card and the urgent alerts do -- one definition of "flagged".

def _status_text(r, bucket):
    return _detail(r, bucket)


def shipment_payload(r, client_map=None):
    """One shipment as the dashboard consumes it."""
    bucket = bucket_for(r)
    tracking = (r.get("tracking_num") or "").strip()
    carrier_raw = (r.get("carrier") or "").strip().upper()
    carrier = CARRIER_DISPLAY.get(
        carrier_raw, carrier_raw if len(carrier_raw) <= 5 else carrier_raw.title())

    boxes = str(r.get("num_boxes") or "").strip()
    packages = r.get("packages") or []
    n_boxes = len(packages) if len(packages) > 1 else (
        int(boxes) if boxes.isdigit() else 0)

    eta_date = _parse_date(r.get("delivery_date"))
    overdue = days_overdue(r)

    return {
        "id": _ref(r),
        "tracking": tracking,
        "track_url": _tracking_url(tracking, carrier_raw),
        "carrier": carrier or "—",
        "client": ((client_map or {}).get(line_name(r, ""), line_name(r, ""))
                   or "Unassigned"),
        "owner": section_for(r),
        "status": bucket,
        "status_label": TAG_TEXT[bucket],
        "detail": _status_text(r, bucket),
        "boxes": n_boxes,
        "eta": eta_date.isoformat() if eta_date else "",
        "eta_label": _short_date(r.get("delivery_date")) or "Unknown",
        "overdue_days": overdue if bucket == FLAGGED else 0,
        "location": (r.get("location") or "").strip(),
        "tab": r.get("tab") or "",
        "sheet_token": r.get("sheet_token") or "",
        "row_num": r.get("row_num"),
        "handle": mark_delivered_value(r),
    }


def dashboard_payload(results, sheet_count=None):
    """Everything the dashboard needs in one JSON-serializable dict."""
    rows = dedupe(results or [])
    totals = counts(rows)
    open_rows = [r for r in rows if bucket_for(r) != DELIVERED]

    # Merge spellings so the client filter has one "7Brew Coffee", not three.
    client_map = build_client_map(open_rows)
    shipments = [shipment_payload(r, client_map) for r in open_rows]

    groups = group_by_section(open_rows)
    clients = sorted({s["client"] for s in shipments if s["client"] != "Unassigned"})

    return {
        "updated": _now_et().isoformat(),
        "updated_label": _now_et().strftime("%b %-d, %Y") + " " + SEP + " "
                         + _now_et().strftime("%-I:%M %p"),
        "sheet_count": sheet_count or len({s["sheet_token"] for s in shipments}),
        "totals": {
            "flagged": totals[FLAGGED],
            "arriving": totals[ARRIVING],
            "transit": totals[TRANSIT],
            "unscanned": totals[UNSCANNED],
            "delivered": totals[DELIVERED],
            "open": len(open_rows),
        },
        "owners": list(groups.keys()),
        "clients": clients,
        "carriers": sorted({s["carrier"] for s in shipments if s["carrier"] != "—"}),
        "shipments": shipments,
    }
