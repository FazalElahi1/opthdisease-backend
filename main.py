from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from routers import auth, users, appointments, calls, scans, admin, analytics, xai, reviews
from routers import stripe_payments, payouts
# JazzCash payments replaced by Stripe Hosted Checkout. Files kept for rollback:
# from routers import jazzcash_payments
from services.firebase import get_firebase_app
from services.escrow import start_scheduler
from services.ratings import start_rating_scheduler

load_dotenv()
get_firebase_app()

app = FastAPI(
    title       = "OpthdiseaseAI API",
    description = "Telemedicine + AI eye screening — complete backend",
    version     = "5.0.0",
    docs_url    = "/docs",
    redoc_url   = "/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

app.include_router(auth.router)                # /auth/*
app.include_router(users.router)               # /users/*
app.include_router(appointments.router)        # /appointments/*
app.include_router(stripe_payments.router)     # /stripe/*   (Stripe Hosted Checkout)
app.include_router(payouts.router)             # /payouts/*  (doctor earnings + manual payouts)
app.include_router(calls.router)               # /calls/*
app.include_router(scans.router)               # /scans/*
app.include_router(admin.router)               # /admin/*
app.include_router(analytics.router)           # /analytics/*
app.include_router(xai.router)                 # /xai/*
app.include_router(reviews.router)             # /reviews/*


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Catch-all: always return JSON so the mobile client can parse the error."""
    from fastapi import HTTPException
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please try again."},
    )


@app.on_event("startup")
async def _startup():
    # Start the escrow auto-release scheduler (releases held funds after the
    # cooling period when there is no complaint).
    start_scheduler()
    # Start the weekly doctor-rating snapshot (Mon 00:00 UTC); seeds once at boot.
    start_rating_scheduler()


@app.get("/")
async def root():
    return {
        "status":  "ok",
        "app":     "OpthdiseaseAI API",
        "version": "5.0.0",
        "docs":    "/docs",
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
