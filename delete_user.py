"""
Run once to delete a user from Firebase Auth + Firestore.
Usage:  python delete_user.py f.elahi1767@gmail.com
"""
import sys
import os
from dotenv import load_dotenv

load_dotenv()

import firebase_admin
from firebase_admin import credentials, auth as firebase_auth, firestore

CREDS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH", "firebase-credentials.json")

if not firebase_admin._apps:
    cred = credentials.Certificate(CREDS_PATH)
    firebase_admin.initialize_app(cred)

db = firestore.client()

APP_DATA_DOC = "app_data/meta"

def delete_user_by_email(email: str):
    # 1. Look up Firebase Auth UID
    try:
        user = firebase_auth.get_user_by_email(email)
        uid  = user.uid
        print(f"Found Firebase Auth user: uid={uid}, email={email}")
    except firebase_auth.UserNotFoundError:
        print(f"No Firebase Auth user found for {email}.")
        uid = None

    # 2. Delete from Firebase Auth
    if uid:
        firebase_auth.delete_user(uid)
        print(f"Deleted Firebase Auth user {uid}.")

    # 3. Delete from Firestore users sub-collection (app_data/meta/users/<uid>)
    if uid:
        users_ref = db.document(APP_DATA_DOC).collection("users").document(uid)
        if users_ref.get().exists:
            users_ref.delete()
            print(f"Deleted Firestore users/{uid}.")
        else:
            print(f"No Firestore users/{uid} document found.")

        doctors_ref = db.document(APP_DATA_DOC).collection("doctors").document(uid)
        if doctors_ref.get().exists:
            doctors_ref.delete()
            print(f"Deleted Firestore doctors/{uid}.")
        else:
            print(f"No Firestore doctors/{uid} document found.")

    print("Done. You can now re-register with this email.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python delete_user.py <email>")
        sys.exit(1)
    delete_user_by_email(sys.argv[1])
