"""
Universal Ingestion Stage 7A — Quick Capture Natural Grammar Parser & Candidate Generator.

Provides deterministic Indonesian natural grammar parsing for concise informal financial captures,
strict fail-closed behavior on ambiguity, exact Decimal amount extraction, account & transfer
normalization, and direct binding to Stage 6 Safe Apply candidate models.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Sequence
import uuid

from aturuang.safe_apply import (
    ApplyCandidate,
    CandidateLifecycleState,
    LedgerMutation,
    compute_preview_hash,
)

# Indonesian Timezone UTC+7
WIB = dt.timezone(dt.timedelta(hours=7))

# Suffix multipliers for informal numbers
MULTIPLIERS: dict[str, Decimal] = {
    "rb": Decimal("1000"),
    "ribu": Decimal("1000"),
    "k": Decimal("1000"),
    "jt": Decimal("1000000"),
    "juta": Decimal("1000000"),
    "m": Decimal("1000000"),
    "mio": Decimal("1000000"),
    "b": Decimal("1000000000"),
    "miliar": Decimal("1000000000"),
    "milyar": Decimal("1000000000"),
}

# Unrecognized / foreign currencies that trigger fail-closed
FOREIGN_CURRENCY_PATTERNS: list[str] = [
    r"(?:\d|\b)(?:usd|dollar|dollars|eur|euro|euros|sgd|myr|jpy|yen|gbp|aud|cny|thb|krw|cad|chf|hkd)\b",
    r"\$\s*\d+",
    r"\d+\s*\$",
    r"€\s*\d+",
    r"\d+\s*€",
    r"£\s*\d+",
    r"\d+\s*£",
    r"¥\s*\d+",
    r"\d+\s*¥",
]

# Transfer verbs in informal Indonesian
TRANSFER_VERBS: frozenset[str] = frozenset({
    "transfer", "tf", "trf", "kirim", "pindah",
})

# Account alias mappings
ACCOUNT_ALIASES: dict[str, str] = {
    "bca": "BCA",
    "bca main": "BCA Main",
    "bca poket": "BCA Poket",
    "jago": "Jago",
    "jago main": "Jago Main",
    "bank jago": "Jago Main",
    "gopay": "GoPay",
    "go-pay": "GoPay",
    "shopeepay": "ShopeePay",
    "shopee pay": "ShopeePay",
    "cash": "Cash",
    "tunai": "Cash",
    "seabank": "SeaBank",
    "sea bank": "SeaBank",
    "blu": "Blu",
    "blu bca": "Blu",
    "stockbit": "Stockbit RDN",
}

# Category heuristics
CATEGORY_KEYWORDS: list[tuple[str, Sequence[str]]] = [
    ("Main Meals", ["makan", "bakso", "nasi", "ayam", "resto", "warung", "mie", "sate", "lunch", "dinner", "sarapan"]),
    ("Cafe & Drinks", ["kopi", "cafe", "kafe", "starbucks", "minum", "teh", "boba", "kopi kenangan"]),
    ("Snacks", ["snack", "cemilan", "roti", "jajan", "gorengan"]),
    ("Groceries & Daily Needs", ["supermarket", "indomaret", "alfamart", "belanja", "sayur", "buah", "pasar"]),
    ("Fuel", ["bensin", "pertalite", "pertamax", "spbu", "shell", "fuel"]),
    ("Parking/Toll", ["parkir", "tol"]),
    ("Transport / Other Transport", ["ojol", "grab", "gojek", "taksi", "kereta", "mrt", "krl", "busway", "transjakarta"]),
    ("Phone & Internet", ["pulsa", "kuota", "paket data", "wifi", "indihome", "biznet"]),
    ("Income", ["gaji", "salary", "upah", "bonus", "cashback", "thr", "dividen", "honor"]),
]


@dataclass
class QuickCaptureResult:
    """Outcome of parsing informal natural Indonesian financial text."""

    status: str  # "SUCCESS" | "REVIEW_REQUIRED"
    raw_text: str
    diagnostic_reason: str | None = None
    transaction_type: str = ""  # "EXPENSE" | "INCOME" | "INTERNAL_TRANSFER"
    amount: Decimal | None = None
    account_from: str = ""
    account_to: str = ""
    description: str = ""
    category: str = ""
    date: str = ""
    time: str = ""
    candidate: ApplyCandidate | None = None

    @property
    def is_valid(self) -> bool:
        return self.status == "SUCCESS"

    @property
    def account(self) -> str:
        if self.transaction_type == "INCOME":
            return self.account_to
        return self.account_from

    @property
    def type(self) -> str:
        return self.transaction_type


def _now_wib() -> dt.datetime:
    return dt.datetime.now(WIB)


def _sanitize_diagnostics(msg: str) -> str:
    """Removes any potential file paths, secrets, or sensitive tokens from diagnostic string."""
    msg = re.sub(r"[a-zA-Z]:\\[^\s]+", "[REDACTED_PATH]", msg)
    msg = re.sub(r"/(?:Users|home|var|tmp)/[^\s]+", "[REDACTED_PATH]", msg)
    return msg.strip()


def parse_quick_capture(
    text: str,
    default_account: str = "Cash",
    current_date: str | None = None,
    current_time: str | None = None,
) -> QuickCaptureResult:
    """
    Parses concise informal Indonesian financial grammar into structured attributes and candidate.
    Enforces fail-closed behavior on ambiguity, unknown currencies, or invalid numbers.
    """
    raw = text.strip()
    if not raw:
        return QuickCaptureResult(
            status="REVIEW_REQUIRED",
            raw_text=text,
            diagnostic_reason=_sanitize_diagnostics("EMPTY_INPUT: Input text cannot be empty"),
        )

    # 1. Check for foreign/unrecognized currencies
    lower_raw = raw.lower()
    for pattern in FOREIGN_CURRENCY_PATTERNS:
        if re.search(pattern, lower_raw):
            return QuickCaptureResult(
                status="REVIEW_REQUIRED",
                raw_text=text,
                diagnostic_reason=_sanitize_diagnostics("UNRECOGNIZED_CURRENCY: Foreign currency not supported without manual exchange rate"),
            )

    # Determine date & time in WIB
    date_str = current_date or _now_wib().strftime("%Y-%m-%d")
    time_str = current_time or _now_wib().strftime("%H:%M:%S")

    # 2. Check for explicit sign prefix
    has_minus = raw.startswith("-")
    has_plus = raw.startswith("+")
    working_text = raw
    if has_minus or has_plus:
        working_text = raw[1:].strip()

    # 3. Detect transfer verbs
    words = working_text.split()
    is_transfer = False
    for w in words:
        clean_w = w.lower().strip(".,;:!?")
        if clean_w in TRANSFER_VERBS:
            is_transfer = True
            break

    # 4. Extract amount
    amt_pattern = re.compile(
        r"(?:rp\.?\s*)?(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*(rb|ribu|k|jt|juta|mio|m|miliar|milyar|b)?\b",
        re.IGNORECASE,
    )
    matches = list(amt_pattern.finditer(working_text))
    chosen_match = None
    parsed_amount: Decimal | None = None

    for m in matches:
        num_str = m.group(1)
        suffix = (m.group(2) or "").lower()

        # Disqualify if immediately preceded by an alphabetic character
        start, end = m.span()
        if start > 0 and working_text[start - 1].isalpha():
            continue

        try:
            if suffix in MULTIPLIERS:
                cleaned_num = num_str.replace(",", ".")
                dec_val = Decimal(cleaned_num) * MULTIPLIERS[suffix]
            else:
                if re.match(r"^\d{1,3}(?:\.\d{3})+(?:,\d+)?$", num_str):
                    cleaned_num = num_str.replace(".", "").replace(",", ".")
                    dec_val = Decimal(cleaned_num)
                elif re.match(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$", num_str):
                    cleaned_num = num_str.replace(",", "")
                    dec_val = Decimal(cleaned_num)
                else:
                    cleaned_num = num_str.replace(",", ".")
                    dec_val = Decimal(cleaned_num)

            parsed_amount = dec_val.quantize(Decimal("0.01"))
            chosen_match = m
            break
        except (InvalidOperation, ValueError):
            continue

    if chosen_match is None or parsed_amount is None:
        return QuickCaptureResult(
            status="REVIEW_REQUIRED",
            raw_text=text,
            diagnostic_reason=_sanitize_diagnostics("MISSING_AMOUNT: Unable to find valid monetary amount"),
        )

    if parsed_amount <= Decimal("0.00"):
        return QuickCaptureResult(
            status="REVIEW_REQUIRED",
            raw_text=text,
            diagnostic_reason=_sanitize_diagnostics("NON_POSITIVE_AMOUNT: Amount must be greater than zero"),
        )

    # 5. Determine Transaction Type
    if is_transfer:
        tx_type = "INTERNAL_TRANSFER"
    elif has_minus:
        tx_type = "EXPENSE"
    elif has_plus:
        tx_type = "INCOME"
    else:
        lower_cleaned = working_text.lower()
        if any(w in lower_cleaned for w in ["gaji", "salary", "bonus", "cashback", "terima", "dapat", "masuk", "income"]):
            tx_type = "INCOME"
        else:
            tx_type = "EXPENSE"

    # Remove amount span from working text to extract accounts and description
    rem_text = working_text[:chosen_match.start()] + " " + working_text[chosen_match.end():]
    rem_text = re.sub(r"\s+", " ", rem_text).strip()

    # 6. Parse Accounts & Description
    account_from = ""
    account_to = ""

    if tx_type == "INTERNAL_TRANSFER":
        ke_match = re.search(r"\b(?:ke|to)\s+([a-zA-Z0-9_\-]+(?:\s+[a-zA-Z0-9_\-]+)?)", rem_text, re.IGNORECASE)
        if not ke_match:
            return QuickCaptureResult(
                status="REVIEW_REQUIRED",
                raw_text=text,
                diagnostic_reason=_sanitize_diagnostics("MISSING_TRANSFER_DESTINATION: Transfer requires destination account ('ke <akun>')"),
            )
        dest_raw = ke_match.group(1).strip()
        dest_canonical = None
        for alias, canon in sorted(ACCOUNT_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
            if dest_raw.lower().startswith(alias):
                dest_canonical = canon
                break
        if not dest_canonical:
            dest_canonical = dest_raw.capitalize()

        account_to = dest_canonical

        rem_text_no_ke = rem_text[:ke_match.start()] + " " + rem_text[ke_match.end():]
        rem_text_no_ke = re.sub(r"\b(?:transfer|tf|trf|kirim|pindah)\b", " ", rem_text_no_ke, flags=re.IGNORECASE)
        rem_text_no_ke = re.sub(r"\s+", " ", rem_text_no_ke).strip()

        source_canonical = None
        for alias, canon in sorted(ACCOUNT_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
            pattern = rf"\b{re.escape(alias)}\b"
            if re.search(pattern, rem_text_no_ke, re.IGNORECASE):
                source_canonical = canon
                rem_text_no_ke = re.sub(pattern, " ", rem_text_no_ke, flags=re.IGNORECASE)
                break

        if not source_canonical:
            source_canonical = default_account
        account_from = source_canonical

        if account_from.lower() == account_to.lower():
            return QuickCaptureResult(
                status="REVIEW_REQUIRED",
                raw_text=text,
                diagnostic_reason=_sanitize_diagnostics("SAME_SOURCE_AND_DESTINATION: Source and destination account cannot be identical"),
            )

        description = rem_text_no_ke.strip() or f"Transfer {account_from} ke {account_to}"
        category = ""

    else:
        found_account = None
        for alias, canon in sorted(ACCOUNT_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
            pattern = rf"\b{re.escape(alias)}\b"
            if re.search(pattern, rem_text, re.IGNORECASE):
                found_account = canon
                rem_text = re.sub(pattern, " ", rem_text, flags=re.IGNORECASE)
                break

        if not found_account:
            found_account = default_account

        if tx_type == "EXPENSE":
            account_from = found_account
            account_to = ""
        else:
            account_from = ""
            account_to = found_account

        desc_clean = re.sub(r"\s+", " ", rem_text).strip()
        description = desc_clean or ("Pengeluaran" if tx_type == "EXPENSE" else "Pemasukan")

        category = "Other / Miscellaneous"
        desc_lower = description.lower()
        for cat_name, kw_list in CATEGORY_KEYWORDS:
            if any(kw in desc_lower for kw in kw_list):
                category = cat_name
                break

    # 7. Construct candidate
    candidate_id = f"qc_{uuid.uuid4().hex[:12]}"
    idemp_hash = hashlib.sha256(f"{raw.lower()}_{date_str}_{parsed_amount}".encode("utf-8")).hexdigest()[:24]
    idempotency_key = f"idemp_qc_{idemp_hash}"
    evidence_keys = (f"quick_capture:{candidate_id}",)

    ledger_ttype = "Expense" if tx_type == "EXPENSE" else ("Income" if tx_type == "INCOME" else "Transfer")

    mutation = LedgerMutation(
        date=date_str,
        time=time_str,
        transaction_type=ledger_ttype,
        amount=parsed_amount,
        account_from=account_from,
        account_to=account_to,
        description=description,
        category=category,
        for_with_whom="Personal / Self",
        money_context="Personal",
        status="Confirmed",
        canonical_id=f"tx_{candidate_id}",
        notes=f"Quick capture: '{raw}'",
        source_refs=f"qc:{raw}",
    )

    preview_hash = compute_preview_hash((mutation,), evidence_keys, candidate_id)

    candidate = ApplyCandidate(
        candidate_id=candidate_id,
        idempotency_key=idempotency_key,
        state=CandidateLifecycleState.PARSED,
        participating_evidence_keys=evidence_keys,
        mutations=(mutation,),
        preview_hash=preview_hash,
    )

    return QuickCaptureResult(
        status="SUCCESS",
        raw_text=text,
        diagnostic_reason=None,
        transaction_type=tx_type,
        amount=parsed_amount,
        account_from=account_from,
        account_to=account_to,
        description=description,
        category=category,
        date=date_str,
        time=time_str,
        candidate=candidate,
    )
