"""Generate the fareBeep Internal System Blueprint PDF."""
from fpdf import FPDF


class BlueprintPDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 16)
        self.set_text_color(33, 37, 41)
        self.cell(0, 10, 'fareBeep | INTERNAL SYSTEM BLUEPRINT', 0, 1, 'L')
        self.set_draw_color(0, 123, 255)
        self.line(10, 20, 200, 20)
        self.ln(10)

    def section_header(self, title):
        self.set_font('Arial', 'B', 12)
        self.set_fill_color(240, 240, 240)
        self.cell(0, 10, f" {title}", 0, 1, 'L', 1)
        self.ln(3)

    def body_text(self, text):
        self.set_font('Arial', '', 11)
        self.multi_cell(0, 7, text)
        self.ln(5)


pdf = BlueprintPDF()
pdf.add_page()

# Content Sections
sections = [
    ("1. THE SHARED LEDGER ALGORITHM",
     "Every search initiated via 360dialog follows a Cache-First protocol. We check the Supabase 'fare_ledger' table before hitting external APIs. Data fresh for < 15 minutes is served immediately. This architecture reduces search latency by 80% and ensures unit economics stay positive as the user base scales."),

    ("2. THE TRANSACTIONAL HANDSHAKE (TIMER)",
     "To mitigate 'Price Jumps', every booking is a timed session. A 10-minute expiry (expires_at) is written to the database. If the Paystack webhook confirms payment after the 10-minute window, the system triggers an automatic refund. A NGN 1,000 volatility buffer is maintained to absorb minor airline price changes during the payment window."),

    ("3. ENTERPRISE STACK (THE PLUMBING)",
     "- Messaging: 360dialog (Meta Official BSP)\n- Intelligence: Gemini 1.5 Flash (NLU Parser)\n- Persistence: Supabase (PostgreSQL)\n- Fare Inventory: Tiqwa (Local African GDS)\n- Status: Aviationstack (ADS-B Live Tracking)\n- Settlement: Paystack (HMAC-Verified Gateway)"),

    ("4. FLIGHT STATUS LOGIC (RETENTION HOOK)",
     "We initiate 'Status Watch' 3 hours before departure. Using Aviationstack data, we track the physical aircraft coming from the previous destination. If the aircraft is delayed, we send a 'Status Beep' to the user on WhatsApp instantly."),
]

for title, text in sections:
    pdf.section_header(title)
    pdf.body_text(text)

pdf.output("fareBeep_Team_Blueprint.pdf")
print("Blueprint Generated: fareBeep_Team_Blueprint.pdf")