# Railway Procfile (FareBeep) - the "Cloud Laptop" strategy.
# Create three Railway services from this repo:
#   1. web    - the FastAPI app: public HTTPS, Paystack hits /webhook/paystack
#   2. worker - Beep cycles + booking sweeps + FX snapshots (auto-scales)
#   3. poller - Telegram long-polling transport (no tunnel, ISP-proof)
# Railway injects $PORT for the web service.
web: uvicorn FareBeep.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: python -m FareBeep.worker
poller: python -m FareBeep.poller
