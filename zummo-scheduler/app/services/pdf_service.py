"""Weekly schedule PDF generation with ReportLab.

Produces a clean employee × day grid. Deterministic, no LLM.
Saved under output/pdfs/ and (optionally) served publicly for MMS delivery.
"""

import logging
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy.orm import Session

from app.models import Employee, Schedule, SubmissionStatus, AvailabilitySubmission

log = logging.getLogger("pdf")

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OUTPUT_DIR = os.path.join("output", "pdfs")


def generate_schedule_pdf(db: Session, schedule: Schedule) -> str:
    """Render a schedule to a PDF file. Returns the file path."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cycle = schedule.cycle
    filename = f"schedule_week_{cycle.week_start_date}_v{schedule.version}.pdf"
    path = os.path.join(OUTPUT_DIR, filename)

    # Gather assignments: {employee_name: set(days)}.
    emp_days: dict[str, set[str]] = {}
    emp_lookup = {e.id: e.name for e in db.query(Employee).all()}
    for sh in schedule.shifts:
        name = emp_lookup.get(sh.employee_id, f"#{sh.employee_id}")
        emp_days.setdefault(name, set()).add(sh.day)

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(path, pagesize=letter, title=filename)
    story = []

    story.append(Paragraph("Zummo Bike — Weekly Schedule", styles["Title"]))
    story.append(
        Paragraph(f"Week of {cycle.week_start_date}", styles["Heading2"])
    )
    story.append(Spacer(1, 0.25 * inch))

    # Header row + one row per employee, ✓ where scheduled.
    header = ["Employee"] + DAYS
    data = [header]
    for name in sorted(emp_days):
        row = [name] + ["✓" if d in emp_days[name] else "" for d in DAYS]
        data.append(row)

    if len(data) == 1:  # no assignments
        data.append(["(no shifts assigned)"] + [""] * len(DAYS))

    table = Table(data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e79")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#f0f4f8")]),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(table)

    # Footnote: anyone who never responded, so Steve sees the gap.
    no_response = (
        db.query(Employee)
        .join(AvailabilitySubmission)
        .filter(
            AvailabilitySubmission.cycle_id == cycle.id,
            AvailabilitySubmission.status == SubmissionStatus.NO_RESPONSE,
        )
        .all()
    )
    if no_response:
        story.append(Spacer(1, 0.3 * inch))
        names = ", ".join(e.name for e in no_response)
        story.append(
            Paragraph(
                f"<i>Did not confirm availability this week: {names}</i>",
                styles["Normal"],
            )
        )

    doc.build(story)
    log.info("Generated PDF %s", path)
    return path
