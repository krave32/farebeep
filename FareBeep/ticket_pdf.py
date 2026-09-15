"""Ticket PDF rendering - the FareBeep booking voucher sent to the user
right after Paystack confirms a purchase. This is OUR confirmation with
the PNR; the airline-issued ticket/boarding pass is a separate artifact
the user forwards back to the bot after online check-in."""
import logging
from io import BytesIO

logger = logging.getLogger("farebeep.ticket_pdf")


def render_ticket_pdf(session, pnr: str, city_name=None) -> bytes:
    """Render the booking-confirmation voucher for a PAID session."""
    from reportlab.lib.pagesizes import A5
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas

    if city_name is None:
        city_name = lambda c: c

    buf = BytesIO()
    w, h = A5
    c = canvas.Canvas(buf, pagesize=A5)
    navy, ink, green, soft = (HexColor("#0b1220"), HexColor("#e8eefc"),
                              HexColor("#5ad19b"), HexColor("#8fa3c8"))

    c.setFillColor(navy)
    c.rect(0, 0, w, h, stroke=0, fill=1)

    # header
    c.setFillColor(green)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(36, h - 44, "FareBeep")
    c.setFillColor(soft)
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 36, h - 44, "Booking confirmation")

    # PNR - the star of the page
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 34)
    c.drawString(36, h - 100, pnr)
    c.setFillColor(soft)
    c.setFont("Helvetica", 9)
    c.drawString(36, h - 116, "Booking reference (PNR) - quote this at check-in")

    c.setStrokeColor(HexColor("#243356"))
    c.setLineWidth(0.7)
    c.line(36, h - 132, w - 36, h - 132)

    # route
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 20)
    route = f"{city_name(session.origin)}  ->  {city_name(session.destination)}"
    c.drawString(36, h - 170, route)

    rows = [
        ("Date", session.flight_date or "-"),
        ("Flight", getattr(session, "flight_iata", None) or "-"),
        ("Fare", f"NGN {session.airline_price:,.0f}"),
        ("Fees", f"NGN {(session.total_price or 0) - (session.airline_price or 0):,.0f}"),
        ("Total paid", f"NGN {session.total_price:,.0f}"),
        ("Payment ref", session.payment_ref or "-"),
        ("Status", "PAID / CONFIRMED"),
    ]
    y = h - 210
    for label, value in rows:
        c.setFillColor(soft)
        c.setFont("Helvetica", 9)
        c.drawString(36, y, label)
        c.setFillColor(ink)
        c.setFont(label == "Status" and "Helvetica-Bold" or "Helvetica", 10)
        c.drawRightString(w - 36, y, str(value))
        y -= 22

    # footer notes
    c.setFillColor(soft)
    c.setFont("Helvetica", 8)
    c.drawString(36, 58, "Present this reference at the airline check-in desk or use the")
    c.drawString(36, 47, "airline's online check-in (opens 24-48h before departure).")
    c.drawString(36, 32, "FareBeep is a booking facilitator - the airline operates the flight.")

    c.showPage()
    c.save()
    logger.info("Ticket PDF rendered for %s (%s)", session.payment_ref, pnr)
    return buf.getvalue()
