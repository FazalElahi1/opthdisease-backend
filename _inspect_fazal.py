"""Read-only: show FAZAL's stored user doc to bisect the profile-save bug."""
from services.firebase import get_user_doc
FAZAL = "BNuYRnNNwWfj7kgr6u47KsgUl8w1"
doc = get_user_doc(FAZAL) or {}
for k in ("user_id", "name", "email", "role", "phone", "phoneNumber", "age", "gender", "auth_method"):
    print(f"  {k}: {doc.get(k)!r}")






