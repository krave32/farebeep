"""Generate the fareBeep Use Case Specification PDF."""
from fpdf import FPDF


class UseCasePDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 16)
        self.cell(0, 10, 'fareBeep: Use Case Specification', 0, 1, 'C')
        self.set_font('Arial', 'I', 10)
        self.cell(0, 10, 'Target: Universal Nigerian Domestic Travel', 0, 1, 'C')
        self.ln(10)

    def section_title(self, title):
        self.set_font('Arial', 'B', 12)
        self.set_fill_color(230, 230, 230)
        self.cell(0, 10, title, 0, 1, 'L', 1)
        self.ln(2)

    def section_body(self, body):
        self.set_font('Arial', '', 11)
        self.multi_cell(0, 7, body)
        self.ln(5)


pdf = UseCasePDF()
pdf.add_page()

content = {
    "1. Scenario Goal": "Enable a user to identify a price drop and complete a secure booking on WhatsApp within a 10-minute guaranteed price window.",

    "2. The Shared Ledger (Efficiency)": "User A searches LOS-ABV. The result is cached in Supabase. User B (and 500 others) receives that same data instantly. This reduces Tiqwa API costs by 90% and ensures sub-second response times.",

    "3. The 'Beep' (Proactive Alerting)": "The system monitors the Ledger. When the price for a tracked route drops by > 10%, a 'Utility' message is sent via Meta Cloud API. This high-intent alert minimizes messaging costs while maximizing conversion.",

    "4. The 10-Minute Handshake (Financial Safety)": "To protect against Nigeria's volatile seat buckets, the moment a user initiates booking, a 10-minute timer starts. This 'Price Lock' is the handshake between the cached Ledger and the Live Inventory.",

    "5. Settlement & Fulfillment": "Payment is handled via Paystack. A 2-minute 'Bank Grace Period' is added to the timer to account for NIBSS network latency. Upon successful webhook verification, the ticket is issued automatically.",

    "6. Flight Status (The Value-Add)": "Post-booking, fareBeep monitors the physical aircraft location via Aviationstack. The user receives a 'Status Beep' if the plane is delayed, often before the airline gate announces it."
}

for title, body in content.items():
    pdf.section_title(title)
    pdf.section_body(body)

pdf.output("fareBeep_UseCase.pdf")
print("PDF Generated successfully as fareBeep_UseCase.pdf")