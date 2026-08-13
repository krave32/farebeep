# Railway Procfile (FareBeep) - ALL-IN-ONE mode.
# A single service runs web + worker + poller together
# (FareBeep/serve_all.py holds a Postgres advisory lock, so extra web
# replicas are pure API and never double-run the loops).
web: python -m FareBeep.serve_all
