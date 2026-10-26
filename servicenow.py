"""ServiceNow knowledge base: find KB articles that match a doubt and its solution, link them to the doubt,
and publish a solved doubt as a draft KB article.

Each client account can have its own ServiceNow instance (admin console -> ServiceNow). KITE uses the
REST Table API with basic authentication: it reads published articles from kb_knowledge (the integration
user needs the knowledge role) and creates drafts in the configured knowledge base. The instance "demo"
answers from built-in sample articles, to try the feature without a ServiceNow instance.
"""
import base64
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from markupsafe import escape

import notify
from auth import login_required
from kt import visible_account_or_404
from models import Question, QuestionKBLink, ServiceNowConnection, db
from timeutil import utcnow

bp = Blueprint("kb", __name__, url_prefix="/accounts/<code>")

DEMO_INSTANCE = "demo"
TIMEOUT = 8
SUGGESTIONS = 5
CACHE_SECONDS = 300
KB_NUMBER_RE = re.compile(r"^KB\d{4,12}$")
SYS_ID_RE = re.compile(r"^[0-9a-f]{32}$")
STOPWORDS = frozenset("""
a about after again all also am an and any are as at be been before but by can cannot could did do does
doing done each for from get gets got had has have having hello hi how i if in into is it its just like
may me more my need needs no not now of on once one only or other our out over please same should so
some still such than thank thanks that the their them then there these they this those to too try tried
trying under until up use used using very want was we were what when where which while who why will
with without would yes you your
""".split())


class ServiceNowError(Exception):
    """Something went wrong talking to ServiceNow; the message is safe to show to the user."""


# --- connection -----------------------------------------------------------------------------------
def connection_for(account):
    """The account's ServiceNow connection if it is switched on, else None."""
    conn = ServiceNowConnection.query.filter_by(account_id=account.id).first()
    return conn if conn is not None and conn.enabled else None


def is_demo(conn):
    return conn.instance == DEMO_INSTANCE


def article_url(conn, number):
    if is_demo(conn):
        return url_for("kb.demo_article", code=conn.account.code, number=number)
    return f"https://{conn.instance}/kb_view.do?sysparm_article={urllib.parse.quote(number)}"


def _request(conn, method, path, params=None, body=None):
    """Call the ServiceNow REST API and return the decoded JSON. Tests replace this function."""
    url = f"https://{conn.instance}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    password = notify.decrypt_secret(conn.password_enc) if conn.password_enc else ""
    if password is None:
        raise ServiceNowError("The saved ServiceNow password can no longer be read; an admin must enter it again.")
    token = base64.b64encode(f"{conn.username}:{password}".encode()).decode()
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Basic {token}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        reasons = {401: "ServiceNow rejected the user name or password.",
                   403: "The ServiceNow user may not do this (it needs the knowledge role).",
                   404: "ServiceNow did not find it (check the instance name and knowledge base sys_id)."}
        raise ServiceNowError(reasons.get(err.code, f"ServiceNow answered with HTTP {err.code}.")) from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        reason = getattr(err, "reason", err)
        raise ServiceNowError(f"Could not reach {conn.instance}: {reason}") from err
    except ValueError as err:
        raise ServiceNowError(f"{conn.instance} did not answer like a ServiceNow instance.") from err


# --- articles -------------------------------------------------------------------------------------
def _words(text):
    return re.findall(r"[a-z0-9][a-z0-9_.-]*[a-z0-9]|[a-z0-9]", (text or "").lower())


def keywords(*weighted_texts, limit=8):
    """The most telling words of (text, weight) pairs, for a ServiceNow text search.

    Short terms such as GL or AP count; transaction codes and error numbers (anything with a digit) too."""
    scores, first_seen = Counter(), {}
    for text, weight in weighted_texts:
        for word in _words(text):
            if word in STOPWORDS or (len(word) < 2 and not word.isdigit()):
                continue
            scores[word] += weight
            first_seen.setdefault(word, len(first_seen))
    return sorted(scores, key=lambda w: (-scores[w], first_seen[w]))[:limit]


def _snippet(text, length=220):
    plain = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())
    return plain if len(plain) <= length else plain[:length].rsplit(" ", 1)[0] + "…"


def _article(conn, row):
    number = row.get("number", "")
    return {"number": number, "title": row.get("short_description", "") or number,
            "snippet": _snippet(row.get("text", "")), "url": article_url(conn, number),
            "updated": (row.get("sys_updated_on") or "")[:10]}


_cache = {}


def search_articles(conn, words, match_any=True, limit=SUGGESTIONS):
    """Published articles matching the words, best first. Raises ServiceNowError."""
    if not words:
        return []
    key = (conn.id, conn.updated_at, tuple(words), match_any, limit)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    if is_demo(conn):
        found = [_article(conn, row) for row in _demo_rows(words, match_any)[:limit]]
    else:
        terms = (" OR " if match_any else " ").join(words)
        query = f"workflow_state=published^active=true^123TEXTQUERY321={terms}"
        if conn.knowledge_base:
            query += f"^kb_knowledge_base={conn.knowledge_base}"
        data = _request(conn, "GET", "/api/now/table/kb_knowledge", {
            "sysparm_query": query, "sysparm_limit": limit, "sysparm_display_value": "true",
            "sysparm_fields": "number,short_description,text,sys_updated_on"})
        found = [_article(conn, row) for row in data.get("result", [])]
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.monotonic() + CACHE_SECONDS, found)
    return found


def get_article(conn, number):
    """One published article by its number, or None. Raises ServiceNowError."""
    if is_demo(conn):
        row = next((a for a in DEMO_ARTICLES if a["number"] == number), None)
        return _article(conn, row) if row else None
    data = _request(conn, "GET", "/api/now/table/kb_knowledge", {
        "sysparm_query": f"number={number}", "sysparm_limit": 1, "sysparm_display_value": "true",
        "sysparm_fields": "number,short_description,text,sys_updated_on"})
    rows = data.get("result", [])
    return _article(conn, rows[0]) if rows else None


def create_draft(conn, title, body_html):
    """Create a draft article in the connection's knowledge base. Returns (number, url); no url in demo mode."""
    if is_demo(conn):
        return f"KB{9000000 + int(time.time()) % 1000000}", None
    data = _request(conn, "POST", "/api/now/table/kb_knowledge", body={
        "short_description": title, "text": body_html, "kb_knowledge_base": conn.knowledge_base,
        "workflow_state": "draft"})
    row = data.get("result") or {}
    if not row.get("number"):
        raise ServiceNowError("ServiceNow did not return the new article number.")
    return row["number"], f"https://{conn.instance}/kb_knowledge.do?sys_id={row.get('sys_id', '')}"


def check_connection(conn):
    """A one-line description of what KITE can see, for the admin's test button. Raises ServiceNowError."""
    if is_demo(conn):
        return f"Demo mode: {len(DEMO_ARTICLES)} sample articles, nothing is sent anywhere."
    data = _request(conn, "GET", "/api/now/table/kb_knowledge", {
        "sysparm_query": "workflow_state=published", "sysparm_limit": 1, "sysparm_fields": "number"})
    message = f"Connected to {conn.instance}" + ("" if data.get("result") else " (no published articles found)")
    if conn.knowledge_base:
        base = _request(conn, "GET", f"/api/now/table/kb_knowledge_base/{conn.knowledge_base}",
                        {"sysparm_fields": "title"}).get("result") or {}
        message += f"; drafts go to the knowledge base '{base.get('title', conn.knowledge_base)}'"
    return message + "."


# --- doubts ---------------------------------------------------------------------------------------
def _question_or_404(account, question_id):
    question = db.session.get(Question, question_id)
    if question is None or question.account_id != account.id:
        abort(404)
    return question


def _conn_or_404(account):
    conn = connection_for(account)
    if conn is None:
        abort(404)
    return conn


def solution_texts(question):
    """The answers to a doubt: replies from anyone but the asker."""
    return [r.body for r in question.replies if r.author_id != question.asked_by_id]


def question_keywords(question):
    return keywords((question.title, 3), (question.detail, 1), *((text, 2) for text in solution_texts(question)))


def can_publish(user, account):
    return user.role in ("sme", "kt_lead", "admin") and user.can_view(account)


def _back(account, question):
    return redirect(url_for("doubts.question", code=account.code, question_id=question.id) + "#kb")


@bp.route("/doubts/<int:question_id>/kb")
@login_required
def suggestions(code, question_id):
    """KB articles matching the doubt and its solution; loaded into the doubt page in the background."""
    account = visible_account_or_404(code)
    question = _question_or_404(account, question_id)
    conn = _conn_or_404(account)
    linked = {link.number for link in question.kb_links}
    words, error, articles = question_keywords(question), None, []
    try:
        articles = [a for a in search_articles(conn, words, limit=SUGGESTIONS + len(linked))
                    if a["number"] not in linked][:SUGGESTIONS]
    except ServiceNowError as err:
        error = str(err)
    return render_template("doubts/_kb.html", account=account, question=question, articles=articles,
                           words=words, error=error, can_link=g.user.can_contribute(account))


@bp.route("/doubts/<int:question_id>/kb/link", methods=["POST"])
@login_required
def link(code, question_id):
    account = visible_account_or_404(code)
    if not g.user.can_contribute(account):
        abort(403)
    question = _question_or_404(account, question_id)
    conn = _conn_or_404(account)
    number = request.form.get("number", "").strip().upper()
    if not KB_NUMBER_RE.match(number):
        flash("Enter a KB article number such as KB0010001.", "error")
        return _back(account, question)
    if any(existing.number == number for existing in question.kb_links):
        flash(f"{number} is already linked.", "info")
        return _back(account, question)
    try:
        article = get_article(conn, number)
    except ServiceNowError as err:
        flash(str(err), "error")
        return _back(account, question)
    if article is None:
        flash(f"{number} is not a published article in ServiceNow.", "error")
        return _back(account, question)
    question.kb_links.append(QuestionKBLink(number=number, title=article["title"][:300], url=article["url"],
                                            source="LINKED", added_by_id=g.user.id, added_at=utcnow()))
    question.updated_at = utcnow()
    db.session.commit()
    flash(f"Linked {number} to {question.code}.", "success")
    return _back(account, question)


@bp.route("/doubts/<int:question_id>/kb/<int:link_id>/remove", methods=["POST"])
@login_required
def unlink(code, question_id, link_id):
    account = visible_account_or_404(code)
    if not g.user.can_contribute(account):
        abort(403)
    question = _question_or_404(account, question_id)
    kb_link = next((k for k in question.kb_links if k.id == link_id), None)
    if kb_link is None:
        abort(404)
    question.kb_links.remove(kb_link)
    db.session.commit()
    flash(f"Removed {kb_link.number} from {question.code}.", "success")
    return _back(account, question)


def article_body(question):
    """The draft article: the problem as raised and the answers that solved it, as safe HTML."""
    def para(text):
        return "<p>" + str(escape(text)).replace("\n", "<br>") + "</p>"

    answers = [r for r in question.replies if r.author_id != question.asked_by_id]
    parts = ["<h2>Problem</h2>", para(question.detail or question.title), "<h2>Solution</h2>"]
    parts += [para(r.body) for r in answers]
    about = [question.transaction.code if question.transaction else "", question.topic.title if question.topic else ""]
    parts.append(para(f"Source: KITE doubt {question.code} ({question.account.code})"
                      + "".join(f", {a}" for a in about if a)
                      + f"; answered by {', '.join(dict.fromkeys(r.author.full_name for r in answers))}."))
    return "\n".join(parts)


@bp.route("/doubts/<int:question_id>/kb/publish", methods=["POST"])
@login_required
def publish(code, question_id):
    account = visible_account_or_404(code)
    if not can_publish(g.user, account):
        abort(403)
    question = _question_or_404(account, question_id)
    conn = _conn_or_404(account)
    if not conn.knowledge_base and not is_demo(conn):
        flash("No knowledge base is set for this account's ServiceNow; ask an admin to add its sys_id.", "error")
        return _back(account, question)
    if not solution_texts(question):
        flash("Answer the doubt first; the answers become the solution in the article.", "error")
        return _back(account, question)
    if any(k.source == "PUBLISHED" for k in question.kb_links):
        flash("This doubt is already published as a KB article.", "info")
        return _back(account, question)
    try:
        number, url = create_draft(conn, question.title, article_body(question))
    except ServiceNowError as err:
        flash(str(err), "error")
        return _back(account, question)
    url = url or url_for("doubts.question", code=account.code, question_id=question.id) + "#kb"
    question.kb_links.append(QuestionKBLink(number=number, title=question.title, url=url, source="PUBLISHED",
                                            added_by_id=g.user.id, added_at=utcnow()))
    question.updated_at = utcnow()
    db.session.commit()
    flash(f"Created draft {number} in ServiceNow. It goes live after the knowledge base's review.", "success")
    return _back(account, question)


@bp.route("/search/kb")
@login_required
def search(code):
    """ServiceNow hits for the account search page; loaded in the background."""
    account = visible_account_or_404(code)
    conn = _conn_or_404(account)
    q = " ".join(request.args.get("q", "").split())[:100]
    words, error, articles = keywords((q, 1)), None, []
    try:
        articles = search_articles(conn, words, match_any=False, limit=10)
    except ServiceNowError as err:
        error = str(err)
    return render_template("knowledge/_kb.html", articles=articles, error=error)


@bp.route("/kb/demo/<number>")
@login_required
def demo_article(code, number):
    account = visible_account_or_404(code)
    conn = _conn_or_404(account)
    row = next((a for a in DEMO_ARTICLES if a["number"] == number), None)
    if not is_demo(conn) or row is None:
        abort(404)
    return render_template("knowledge/demo_article.html", account=account, article=row)


# --- demo -----------------------------------------------------------------------------------------
DEMO_ARTICLES = [
    {"number": "KB0010001", "short_description": "Which GL account to use for vendor credit notes",
     "sys_updated_on": "2026-09-14 10:20:00",
     "text": "<p>Vendor credit notes are posted with FB65 to GL account 4100 (Purchase returns), not 4000. "
             "Use document type KG and reference the original invoice number.</p>"},
    {"number": "KB0010002", "short_description": "Posting a vendor invoice with FB60",
     "sys_updated_on": "2026-08-30 08:00:00",
     "text": "<p>Open FB60, enter the vendor, invoice date, amount and tax code, then the expense GL line. "
             "Invoices over 10,000 need approval in the workflow inbox before payment.</p>"},
    {"number": "KB0010003", "short_description": "Month-end close checklist for accounts payable",
     "sys_updated_on": "2026-10-02 12:45:00",
     "text": "<p>Clear the GR/IR account (F.13), post accruals, run the payment proposal (F110) and "
             "reconcile the vendor sub-ledger with the GL before period close (OB52).</p>"},
    {"number": "KB0010004", "short_description": "Payment run F110 fails with 'no valid payment method'",
     "sys_updated_on": "2026-07-19 16:05:00",
     "text": "<p>Check the vendor master payment methods (XK02, payment transactions) and the house bank "
             "ranking in FBZP. Blocked invoices are skipped by the payment proposal.</p>"},
    {"number": "KB0010005", "short_description": "Reset a locked SAP user or password",
     "sys_updated_on": "2026-06-11 09:30:00",
     "text": "<p>Raise a request in the self-service portal (catalog item 'SAP access'). The service desk "
             "unlocks the user in SU01 within 4 hours; urgent cases go to the on-call basis team.</p>"},
    {"number": "KB0010006", "short_description": "VPN connects but SAP GUI cannot reach the server",
     "sys_updated_on": "2026-05-03 14:10:00",
     "text": "<p>Use the full tunnel VPN profile, then check the SAP Logon entry points to the message server, "
             "not the old application server host name.</p>"},
    {"number": "KB0010007", "short_description": "How to get database (DB) access",
     "sys_updated_on": "2026-10-20 11:00:00",
     "text": "<p>Raise the catalog item 'Database access' in the self-service portal with the database name, "
             "environment (DEV, QA or PROD) and read or write access. Your manager approves it, then the DBA team "
             "creates the account within 2 working days. PROD write access also needs the application owner.</p>"},
]


def _demo_rows(words, match_any):
    """Sample articles containing the words, most words first (a rough stand-in for ServiceNow's relevance)."""
    scored = []
    for row in DEMO_ARTICLES:
        haystack = set(_words(row["short_description"] + " " + _snippet(row["text"], 10000)))
        hits = sum(1 for w in words if w in haystack)
        if hits and (match_any or hits == len(words)):
            scored.append((-hits, row["number"], row))
    return [row for _, _, row in sorted(scored, key=lambda s: s[:2])]
