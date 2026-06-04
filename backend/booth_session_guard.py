"""Shared booth-flow guards for CONFIDEX.

These helpers make QR login, purchase, and recovery use the same rules:
- QR login may only start a new flow when the kiosk is actually on QRLoginPage.
- A same-user unfinished paid flow must resume instead of starting a new purchase.
- A different user's unfinished paid flow must not block or overwrite this user.
"""

from __future__ import annotations

from typing import Any

try:
    from backend.flow_state import get_active_flow
except Exception:  # pragma: no cover
    get_active_flow = None


POST_PAYMENT_STAGES = {
    "ChangeDispensingPage",
    "ReceiptPage",
    "DispensingPage",
    "HowToUsePage",
    "KitInsertionPage",
}

SAFE_AUTO_RESUME_STAGES = {
    "ReceiptPage",
    "HowToUsePage",
    "KitInsertionPage",
}

HARDWARE_RESUME_STAGES = {
    "ChangeDispensingPage",
    "DispensingPage",
}


def extract_user_id(user_data: Any) -> str:
    if not isinstance(user_data, dict):
        return ""

    return str(
        user_data.get("_id")
        or user_data.get("userID")
        or user_data.get("userId")
        or user_data.get("user_id")
        or user_data.get("id")
        or ""
    ).strip()


def normalize_flow(flow: Any) -> dict[str, Any] | None:
    if not isinstance(flow, dict):
        return None

    stage = str(flow.get("stage") or "").strip()
    tx = str(flow.get("transaction_id") or "").strip()

    if stage not in POST_PAYMENT_STAGES or not tx:
        return None

    return flow


def get_resume_flow() -> dict[str, Any] | None:
    """Backward-compatible fallback.

    This returns the latest active flow only for older callers.

    New login code should use get_resume_flow_for_user(user_data), because the
    booth now supports multiple unfinished paid flows from different users.
    """
    if get_active_flow is None:
        return None

    try:
        return normalize_flow(get_active_flow())
    except Exception:
        return None


def get_resume_flow_for_user(user_data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return only the scanned user's unfinished paid booth flow.

    Important:
    This must not load the latest/global flow first. If User A has an unfinished
    flow and User B scans, User B should not be blocked. The system should only
    check whether User B has their own unfinished flow.
    """
    if get_active_flow is None:
        return None

    if not isinstance(user_data, dict):
        return None

    logged_id = extract_user_id(user_data)

    if not logged_id:
        return None

    try:
        try:
            flow = get_active_flow(user_data=user_data)
        except TypeError:
            # Old flow_state.py does not support per-user lookup yet.
            # This fallback keeps the app from crashing, but proper multi-user
            # behavior requires replacing backend/flow_state.py with the
            # per-user active_flows version.
            flow = get_active_flow()
    except Exception:
        return None

    flow = normalize_flow(flow)

    if not flow:
        return None

    stored_id = extract_user_id(flow.get("user_data") or {})

    if stored_id and stored_id != logged_id:
        return None

    return flow


def has_active_flow_for_other_user(user_data: dict[str, Any] | None) -> bool:
    """Legacy helper.

    With per-user active flows, another user's unfinished transaction should not
    block the current user. This helper is kept only for compatibility/logging.

    Do not use this to refuse QR login.
    Do not use this to clear another user's flow.
    """
    if get_active_flow is None:
        return False

    if not isinstance(user_data, dict):
        return False

    logged_id = extract_user_id(user_data)

    if not logged_id:
        return False

    try:
        flow = normalize_flow(get_active_flow())
    except Exception:
        return False

    if not flow:
        return False

    stored_id = extract_user_id(flow.get("user_data") or {})

    return bool(stored_id and stored_id != logged_id)


def build_resume_kwargs(user_data: dict[str, Any], flow: dict[str, Any]) -> dict[str, Any]:
    flow = flow or {}

    extra = flow.get("extra") if isinstance(flow.get("extra"), dict) else {}
    kwargs = dict(extra or {})

    stored_user = flow.get("user_data") if isinstance(flow.get("user_data"), dict) else {}

    merged_user = {}

    if isinstance(stored_user, dict):
        merged_user.update(stored_user)

    if isinstance(user_data, dict):
        merged_user.update(user_data)

    tx = str(flow.get("transaction_id") or kwargs.get("transaction_id") or "").strip()

    if tx:
        merged_user["transaction_id"] = tx
        merged_user["latest_transaction_id"] = tx
        kwargs["transaction_id"] = tx

    product = (
        kwargs.get("selected_product")
        or kwargs.get("product")
        or flow.get("selected_product")
        or flow.get("product")
        or {}
    )

    if not isinstance(product, dict):
        product = {}

    kwargs["user_data"] = merged_user
    kwargs["selected_product"] = product
    kwargs["product"] = product

    return kwargs