# import os
# import stripe
# from dotenv import load_dotenv

# load_dotenv()

# stripe.api_key        = os.getenv("STRIPE_SECRET_KEY", "")
# PLATFORM_DOMAIN       = os.getenv("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")
# STRIPE_CURRENCY       = "pkr"


# # ── Stripe Connect account creation ───────────────────────────────────────────

# def create_connect_account(doctor_email: str, doctor_name: str) -> str:
#     """
#     Create a Stripe Express account for the doctor.
#     Returns the Stripe account ID (acct_xxxx) — save this to Firestore.
#     """
#     account = stripe.Account.create(
#         type         = "express",
#         country      = "PK",          # Pakistan
#         email        = doctor_email,
#         capabilities = {
#             "transfers": {"requested": True},
#         },
#         business_type = "individual",
#         individual    = {
#             "email":      doctor_email,
#             "first_name": doctor_name.split()[0] if doctor_name else "",
#             "last_name":  " ".join(doctor_name.split()[1:]) if len(doctor_name.split()) > 1 else "",
#         },
#         business_profile = {
#             "name": doctor_name,
#             "mcc":  "8011",           # MCC code for doctors/physicians
#         },
#         settings = {
#             "payouts": {
#                 "schedule": {"interval": "manual"},  # doctor controls when to withdraw
#             }
#         },
#     )
#     return account.id


# def create_account_onboarding_link(stripe_account_id: str, doctor_id: str) -> str:
#     """
#     Create a Stripe Connect onboarding link.
#     Doctor clicks this to enter bank details on Stripe's hosted page.
#     """
#     link = stripe.AccountLink.create(
#         account     = stripe_account_id,
#         refresh_url = f"{PLATFORM_DOMAIN}/stripe/connect/refresh?doctor_id={doctor_id}",
#         return_url  = f"{PLATFORM_DOMAIN}/stripe/connect/return?doctor_id={doctor_id}",
#         type        = "account_onboarding",
#     )
#     return link.url


# def create_account_dashboard_link(stripe_account_id: str) -> str:
#     """
#     Create a Stripe Express Dashboard link so the doctor can
#     view their payouts and bank account details.
#     """
#     link = stripe.Account.create_login_link(stripe_account_id)
#     return link.url


# def get_connect_account(stripe_account_id: str) -> stripe.Account:
#     return stripe.Account.retrieve(stripe_account_id)


# def is_account_onboarded(stripe_account_id: str) -> bool:
#     """
#     Check if the doctor has completed Stripe onboarding
#     (i.e. added their bank account details).
#     """
#     account = stripe.Account.retrieve(stripe_account_id)
#     return (
#         account.details_submitted and
#         not account.requirements.currently_due and
#         account.payouts_enabled
#     )


# # ── Payouts ────────────────────────────────────────────────────────────────────

# def get_available_balance(stripe_account_id: str) -> int:
#     """
#     Get the doctor's available balance in PKR (smallest unit = paisa).
#     Returns amount in whole rupees.
#     """
#     balance = stripe.Balance.retrieve(stripe_account=stripe_account_id)
#     available_pkr = 0
#     for b in balance.available:
#         if b.currency == STRIPE_CURRENCY:
#             available_pkr += b.amount   # PKR is zero-decimal so no conversion needed
#     return available_pkr


# def create_payout(stripe_account_id: str, amount_pkr: int, description: str = "Consultation earnings") -> stripe.Payout:
#     """
#     Initiate a payout to the doctor's bank account.
#     amount_pkr: amount in whole rupees (PKR is zero-decimal).
#     """
#     payout = stripe.Payout.create(
#         amount      = amount_pkr,
#         currency    = STRIPE_CURRENCY,
#         description = description,
#         stripe_account = stripe_account_id,
#     )
#     return payout


# def transfer_earnings_to_doctor(
#     stripe_account_id: str,
#     amount_pkr:        int,
#     appointment_ids:   list,
# ) -> stripe.Transfer:
#     """
#     Transfer platform earnings to the doctor's Connect account.
#     Called when appointments are completed — moves money from platform
#     to doctor's Stripe balance so they can later withdraw to bank.
#     """
#     transfer = stripe.Transfer.create(
#         amount      = amount_pkr,
#         currency    = STRIPE_CURRENCY,
#         destination = stripe_account_id,
#         metadata    = {
#             "appointment_ids": ",".join(appointment_ids),
#             "description":     "Consultation earnings transfer",
#         },
#     )
#     return transfer


# def get_payout_history(stripe_account_id: str, limit: int = 20) -> list:
#     """Get the doctor's payout history."""
#     payouts = stripe.Payout.list(limit=limit, stripe_account=stripe_account_id)
#     return [
#         {
#             "id":          p.id,
#             "amount":      p.amount,
#             "currency":    p.currency,
#             "status":      p.status,
#             "arrival_date": p.arrival_date,
#             "description": p.description,
#         }
#         for p in payouts.auto_paging_iter()
#     ]