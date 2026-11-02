"""An SOP as a PowerPoint download: its content, who may have it, draft marking, long SOPs and the path from a transcript."""
import io

import pytest
from pptx import Presentation
from pptx.util import Inches

import sop_slides
from conftest import account_id, login, user_id
from models import KTProject, Sop, SopVersion, TranscriptItem, db
from timeutil import utcnow

A = "/accounts/ACME"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def make_sop(app, steps="1. Drain the node\n2. Patch it\nStep 3: Restart it", status="DRAFT", code="ACME", link="", purpose="Keep nodes patched."):
    with app.app_context():
        sop = Sop(account_id=account_id(code), number=1, title="Node patching & reboot", owner_id=user_id("sme"), created_at=utcnow(), updated_at=utcnow())
        sop.versions.append(SopVersion(number=1, purpose=purpose, steps=steps, link=link, status=status, change_note="First version",
                                       created_by_id=user_id("sme"), created_at=utcnow(), updated_at=utcnow()))
        db.session.add(sop)
        db.session.commit()
        return sop.id


def deck(response):
    assert response.status_code == 200 and response.mimetype == PPTX
    return Presentation(io.BytesIO(response.data))


def words(slide):
    return " ".join(shape.text_frame.text for shape in slide.shapes if shape.has_text_frame)


def test_download_has_title_purpose_steps_owner_and_version(app, client):
    sid = make_sop(app)
    login(client, "mem1")
    response = client.get(f"{A}/sops/{sid}/slides.pptx")
    assert "SOP-001-Node-patching-reboot-v1.pptx" in response.headers["Content-Disposition"]
    slides = list(deck(response).slides)
    assert len(slides) == 4
    assert "Node patching & reboot" in words(slides[0]) and "Acme Corporation" in words(slides[0])
    assert "Keep nodes patched." in words(slides[1])
    steps = words(slides[2])
    assert "Drain the node" in steps and "Restart it" in steps
    assert "1. Drain" not in steps and "Step 3" not in steps  # numbers come from the slide, not twice
    assert "Sme" in words(slides[3]) and "First version" in words(slides[3])


def test_draft_says_so_on_every_slide_and_approved_does_not(app, client):
    draft = make_sop(app)
    login(client, "mem1")
    slides = list(deck(client.get(f"{A}/sops/{draft}/slides.pptx")).slides)
    assert "Draft" in words(slides[0])
    assert all("NOT THE APPROVED SOP" in words(s) for s in slides[1:])
    with app.app_context():
        version = db.session.get(Sop, draft).versions[0]
        version.status, version.approved_at, version.approved_by_id = "APPROVED", utcnow(), user_id("lead")
        db.session.commit()
    slides = list(deck(client.get(f"{A}/sops/{draft}/slides.pptx")).slides)
    assert not any("NOT THE APPROVED SOP" in words(s) for s in slides)
    assert "Approved" in words(slides[0]) and "Lead" in words(slides[-1])


def test_retired_sop_is_marked(app, client):
    sid = make_sop(app, status="APPROVED")
    with app.app_context():
        db.session.get(Sop, sid).retired_at = utcnow()
        db.session.commit()
    login(client, "mem1")
    slides = list(deck(client.get(f"{A}/sops/{sid}/slides.pptx")).slides)
    assert "Retired" in words(slides[0]) and "NOT THE APPROVED SOP" in words(slides[1])


def test_chosen_version_is_the_one_downloaded(app, client):
    sid = make_sop(app, steps="Old step", status="APPROVED")
    with app.app_context():
        sop = db.session.get(Sop, sid)
        sop.versions.append(SopVersion(number=2, purpose="New purpose", steps="New step", status="DRAFT", created_by_id=user_id("sme"),
                                       created_at=utcnow(), updated_at=utcnow()))
        db.session.commit()
    login(client, "mem1")
    page = client.get(f"{A}/sops/{sid}?v=2").data.decode()
    assert f"{A}/sops/{sid}/slides.pptx?v=2" in page
    assert "New step" in " ".join(words(s) for s in deck(client.get(f"{A}/sops/{sid}/slides.pptx?v=2")).slides)
    assert "Old step" in " ".join(words(s) for s in deck(client.get(f"{A}/sops/{sid}/slides.pptx")).slides)  # the approved one


def test_who_may_download(app, client):
    sid = make_sop(app)
    login(client, "viewer")
    assert f"{A}/sops/{sid}/slides.pptx" in client.get(f"{A}/sops/{sid}").data.decode()
    assert client.get(f"{A}/sops/{sid}/slides.pptx").status_code == 200
    client.get("/logout")
    login(client, "mem2")  # another account
    assert client.get(f"{A}/sops/{sid}/slides.pptx").status_code == 404
    client.get("/logout")
    assert client.get(f"{A}/sops/{sid}/slides.pptx").status_code == 302  # sign in first
    other = make_sop(app, code="BETA")
    login(client, "lead")
    assert client.get(f"{A}/sops/{other}/slides.pptx").status_code == 404


def test_long_sop_runs_over_slides_and_stays_on_them(app, client):
    steps = "\n".join(f"{n}. Do thing number {n}" for n in range(1, 41)) + "\n41. " + "word " * 900
    sid = make_sop(app, steps=steps, purpose="x")
    login(client, "mem1")
    slides = list(deck(client.get(f"{A}/sops/{sid}/slides.pptx")).slides)
    step_slides = [s for s in slides if "PROCEDURE" in words(s)]
    assert len(step_slides) > 2 and "(1 of " in words(step_slides[0])
    text = " ".join(words(s) for s in step_slides)
    assert "Do thing number 1 " in text + " " and "Do thing number 40" in text and text.count("word") == 900
    for slide in step_slides:
        for shape in slide.shapes:
            assert shape.top + shape.height <= Inches(7.5)


def test_no_steps_and_a_link_only_sop(app, client):
    sid = make_sop(app, steps="", link="https://x.sharepoint.com/sop.docx")
    login(client, "mem1")
    slides = list(deck(client.get(f"{A}/sops/{sid}/slides.pptx")).slides)
    assert "The steps are in the SOP document." in " ".join(words(s) for s in slides)
    links = [run.hyperlink.address for shape in slides[-1].shapes if shape.has_text_frame for p in shape.text_frame.paragraphs for run in p.runs if run.hyperlink.address]
    assert links == ["https://x.sharepoint.com/sop.docx"]


def test_odd_text_does_not_break_the_file(app, client):
    sid = make_sop(app, steps="Use <b>tags</b> & \x07bells\n\n   \n- A bullet\n* Another", purpose="Quotes \" ' and \x0b")
    login(client, "mem1")
    slides = list(deck(client.get(f"{A}/sops/{sid}/slides.pptx")).slides)
    text = " ".join(words(s) for s in slides)
    assert "<b>tags</b> & bells" in text and "A bullet" in text and "- A bullet" not in text


def test_from_a_transcript_without_any_api_key(app, client):
    with app.app_context():
        project = KTProject(account_id=account_id("ACME"), name="Platform", workflow="CLIENT_KT")
        db.session.add(project)
        db.session.commit()
        pid = project.id
    assert not app.config.get("ANTHROPIC_API_KEY")
    login(client, "lead")
    client.post(f"{A}/from-recording/sop", data={"project_id": pid, "title": "Patching KT", "pasted":
                "Priya: the process for patching is as follows\nPriya: first drain the node\nPriya: then patch it\nPriya: finally restart it"})
    with app.app_context():
        item = TranscriptItem.query.one()
        item_id, tid, title, detail = item.id, item.transcript_id, item.title, item.detail
    client.post(f"{A}/projects/{pid}/meetings/{tid}/create", data={f"item-{item_id}": "1", f"title-{item_id}": title, f"detail-{item_id}": detail})
    with app.app_context():
        sid = Sop.query.one().id
    slides = list(deck(client.get(f"{A}/sops/{sid}/slides.pptx")).slides)
    text = " ".join(words(s) for s in slides)
    assert "Drain the node" in text and "Patch it" in text and "Restart it" in text and "NOT THE APPROVED SOP" in text


@pytest.mark.parametrize("line, step", [("1. One", "One"), ("2) Two", "Two"), ("Step 3: Three", "Three"), ("- Four", "Four"), ("Five", "Five"),
                                        ("6 servers are patched", "6 servers are patched")])
def test_numbers_and_bullets_are_taken_off_steps(line, step):
    assert sop_slides._steps(line) == [step]
