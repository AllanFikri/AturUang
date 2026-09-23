"""
Money Tracks V12 — Merchant Category Rules & Classifier
Two-level categories (parent + child) with dynamic database-driven rules.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import re
import sqlite3
from typing import Any

PARENT_CATEGORIES = [
    "Makanan & Minuman",
    "Belanja",
    "Transportasi",
    "Kesehatan",
    "Pendidikan",
    "Jasa & Layanan",
    "Transfer & Investasi",
    "Lain-lain",
]

SEED_CATEGORIES: list[tuple[str, str]] = [
    ("Makanan & Minuman", "Makanan Berat"),
    ("Makanan & Minuman", "Cafe & Minuman"),
    ("Makanan & Minuman", "Snacks & Jajan"),
    ("Belanja", "Grocery"),
    ("Belanja", "Toko Retail"),
    ("Belanja", "Online"),
    ("Transportasi", "Bensin"),
    ("Transportasi", "Parkir & Tol"),
    ("Transportasi", "Otomotif"),
    ("Transportasi", "Umum"),
    ("Kesehatan", "Apotek"),
    ("Kesehatan", "Klinik"),
    ("Pendidikan", "Fotokopi & ATK"),
    ("Pendidikan", "Kampus"),
    ("Jasa & Layanan", "Personal Care"),
    ("Jasa & Layanan", "Jasa & Kurir"),
    ("Jasa & Layanan", "Print & Banner"),
    ("Jasa & Layanan", "Coworking"),
    ("Jasa & Layanan", "Studio"),
    ("Transfer & Investasi", "Transfer ke Teman"),
    ("Transfer & Investasi", "Investment Movement"),
    ("Transfer & Investasi", "Allocation Movement"),
    ("Lain-lain", "Subscriptions"),
    ("Lain-lain", "Toko Oleh-oleh"),
    ("Lain-lain", "Parfum"),
    ("Lain-lain", "Lab Equipment"),
    ("Lain-lain", "Other / Miscellaneous"),
]

# (merchant_pattern, match_type, parent_category, child_category, priority, amount_threshold, amount_below_child, amount_above_child)
SEED_RULES: list[tuple[str, str, str, str, int, float | None, str | None, str | None]] = [
    # Priority 5 (specific merchants — highest)
    ("ESB RESTAURANT", "substring", "Makanan & Minuman", "Makanan Berat", 5, None, None, None),
    ("KEDAI SHENTALIE", "substring", "Makanan & Minuman", "Makanan Berat", 5, None, None, None),
    ("WARUNG MBAK YANI", "substring", "Makanan & Minuman", "Makanan Berat", 5, None, None, None),
    ("KANTIN ARSITEKTUR", "substring", "Makanan & Minuman", "Makanan Berat", 5, None, None, None),
    ("DJAYA BARU", "substring", "Makanan & Minuman", "Makanan Berat", 5, None, None, None),
    ("WARMINDO SAM NDUT", "substring", "Makanan & Minuman", "Makanan Berat", 5, None, None, None),
    ("SIBERPOS", "substring", "Makanan & Minuman", "Makanan Berat", 5, None, None, None),
    ("HISANA", "substring", "Makanan & Minuman", "Makanan Berat", 5, None, None, None),
    ("GACOAN", "substring", "Makanan & Minuman", "Makanan Berat", 5, None, None, None),
    ("KJPRI UB", "substring", "Belanja", "Grocery", 5, None, None, None),
    ("AROMATIQUE MALANG", "substring", "Lain-lain", "Parfum", 5, None, None, None),
    ("TOKO NURRA LAB", "substring", "Lain-lain", "Lab Equipment", 5, None, None, None),
    ("MAESTRO BRIGJEN", "substring", "Jasa & Layanan", "Print & Banner", 5, None, None, None),
    ("HANA NAILA PUTRI", "substring", "Makanan & Minuman", "Snacks & Jajan", 5, None, None, None),
    ("MOLWN JUMBO", "substring", "Makanan & Minuman", "Snacks & Jajan", 5, None, None, None),
    ("GALAXY GAMING", "substring", "Makanan & Minuman", "Snacks & Jajan", 5, None, None, None),
    ("JAKARTA CHEESE", "substring", "Lain-lain", "Toko Oleh-oleh", 5, None, None, None),
    ("BURUNG SWARI", "substring", "Lain-lain", "Toko Oleh-oleh", 5, None, None, None),
    ("LAPIS KUKUS TUGU", "substring", "Lain-lain", "Toko Oleh-oleh", 5, None, None, None),
    ("SING JAYA", "substring", "Lain-lain", "Toko Oleh-oleh", 5, None, None, None),

    # Priority 10 (amount override)
    ("INDOMARET", "substring", "Belanja", "Grocery", 10, 25000.0, "Snacks & Jajan", "Grocery"),
    ("IDM INDOMARET", "substring", "Belanja", "Grocery", 10, 25000.0, "Snacks & Jajan", "Grocery"),

    # Priority 20 (fuel)
    ("SPBU", "substring", "Transportasi", "Bensin", 20, None, None, None),
    ("SHELL", "substring", "Transportasi", "Bensin", 20, None, None, None),
    ("PERTAMINA", "substring", "Transportasi", "Bensin", 20, None, None, None),

    # Priority 30 (grocery)
    ("ALFAMART", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("ALFAGIFT", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("SUPERINDO", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("KS 24", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("TOKO BU RUDI", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("ABC MART", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("B MART", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("99 FROZEN MART", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("SRIWIJAYA MEGA", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("LOKOMART", "substring", "Belanja", "Grocery", 30, None, None, None),
    ("GROSIR", "substring", "Belanja", "Grocery", 30, None, None, None),

    # Priority 40 (health)
    ("APOTEK", "substring", "Kesehatan", "Apotek", 40, None, None, None),
    ("KLINIK", "substring", "Kesehatan", "Klinik", 40, None, None, None),
    ("WATSONS", "substring", "Kesehatan", "Apotek", 40, None, None, None),

    # Priority 50 (education)
    ("FOTOCOPY", "substring", "Pendidikan", "Fotokopi & ATK", 50, None, None, None),
    ("SARJANA 1", "substring", "Pendidikan", "Fotokopi & ATK", 50, None, None, None),
    ("ATK", "substring", "Pendidikan", "Fotokopi & ATK", 50, None, None, None),
    ("STATIONARY", "substring", "Pendidikan", "Fotokopi & ATK", 50, None, None, None),
    ("KAMPUS", "substring", "Pendidikan", "Kampus", 50, None, None, None),

    # Priority 60 (cafe & drinks)
    ("COFFEE", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),
    ("COFFE", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),
    ("KOFFIE", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),
    ("NGOPI", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),
    ("KOPI", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),
    ("CAFE", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),
    ("KAFE", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),
    ("KAFFE", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),
    ("EATERY", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),
    ("WARKOP", "substring", "Makanan & Minuman", "Cafe & Minuman", 60, None, None, None),

    # Priority 70 (main meals - generic)
    ("WARUNG", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("KANTIN", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("WARTEG", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("BAKSO", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("SATE", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("MIE", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("AYAM", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("NASI", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("PECEL", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("SOTO", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("PENYETAN", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("ANGKRINGAN", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("BEBEK", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("WARMIND", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("GEPREK", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("RESTAURANT", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("MASAKAN", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("PADANG", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),
    ("DIMSUM", "substring", "Makanan & Minuman", "Makanan Berat", 70, None, None, None),

    # Priority 80 (snacks - generic)
    ("SNACK", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("JAJAN", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("BAKERY", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("ROTI", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("CAKE", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("KUE", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("MOLEN", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("PEMPEK", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("CIMOL", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("BATAGOR", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("MARTABAK", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("ICE CREAM", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("ES KRIM", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("ES DEGAN", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("DESSERT", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),
    ("BAKPAO", "substring", "Makanan & Minuman", "Snacks & Jajan", 80, None, None, None),

    # Priority 100 (parking)
    ("PARKIR", "substring", "Transportasi", "Parkir & Tol", 100, None, None, None),
    ("PARKING", "substring", "Transportasi", "Parkir & Tol", 100, None, None, None),
    ("TOLL", "substring", "Transportasi", "Parkir & Tol", 100, None, None, None),
    ("TOILET", "substring", "Transportasi", "Parkir & Tol", 100, None, None, None),
    ("SENTRYPARK", "substring", "Transportasi", "Parkir & Tol", 100, None, None, None),

    # Priority 110 (personal care)
    ("BARBER", "substring", "Jasa & Layanan", "Personal Care", 110, None, None, None),
    ("BARBERSHOP", "substring", "Jasa & Layanan", "Personal Care", 110, None, None, None),
    ("SALON", "substring", "Jasa & Layanan", "Personal Care", 110, None, None, None),

    # Priority 120 (otomotif)
    ("BENGKEL", "substring", "Transportasi", "Otomotif", 120, None, None, None),
    ("MOTOR", "substring", "Transportasi", "Otomotif", 120, None, None, None),
    ("CUCI MOTOR", "substring", "Transportasi", "Otomotif", 120, None, None, None),

    # Priority 130 (transportasi umum)
    ("TRANS", "substring", "Transportasi", "Umum", 130, None, None, None),
    ("TRAVEL", "substring", "Transportasi", "Umum", 130, None, None, None),
    ("BUS", "substring", "Transportasi", "Umum", 130, None, None, None),

    # Priority 140 (jasa & kurir)
    ("PAXEL", "substring", "Jasa & Layanan", "Jasa & Kurir", 140, None, None, None),
    ("JNE", "substring", "Jasa & Layanan", "Jasa & Kurir", 140, None, None, None),
    ("SICEPAT", "substring", "Jasa & Layanan", "Jasa & Kurir", 140, None, None, None),

    # Priority 150 (coworking)
    ("COWORKING", "substring", "Jasa & Layanan", "Coworking", 150, None, None, None),
    ("ATHOME SPACE", "substring", "Jasa & Layanan", "Coworking", 150, None, None, None),

    # Priority 160 (lab)
    ("NURRA LAB", "substring", "Lain-lain", "Lab Equipment", 160, None, None, None),

    # Priority 170 (parfum)
    ("PARFUM", "substring", "Lain-lain", "Parfum", 170, None, None, None),
    ("AROMATIQUE", "substring", "Lain-lain", "Parfum", 170, None, None, None),

    # Priority 180 (print)
    ("PRINT", "substring", "Jasa & Layanan", "Print & Banner", 180, None, None, None),
    ("BANNER", "substring", "Jasa & Layanan", "Print & Banner", 180, None, None, None),
    ("PERCETAKAN", "substring", "Jasa & Layanan", "Print & Banner", 180, None, None, None),

    # Priority 190 (online shopping)
    ("SHOPEE", "substring", "Belanja", "Online", 190, None, None, None),
    ("TOKOPEDIA", "substring", "Belanja", "Online", 190, None, None, None),
    ("LAZADA", "substring", "Belanja", "Online", 190, None, None, None),
    ("ALFAGIFT", "substring", "Belanja", "Online", 190, None, None, None),

    # Priority 200 (retail generic)
    ("TOKO", "substring", "Belanja", "Toko Retail", 200, None, None, None),
    ("STORE", "substring", "Belanja", "Toko Retail", 200, None, None, None),
    ("SHOP", "substring", "Belanja", "Toko Retail", 200, None, None, None),
    ("MART", "substring", "Belanja", "Toko Retail", 200, None, None, None),
    ("COLLECTION", "substring", "Belanja", "Toko Retail", 200, None, None, None),

    # Priority 210 (elektronik)
    ("ELEKTRONIK", "substring", "Belanja", "Toko Retail", 210, None, None, None),
    ("KOMPUTER", "substring", "Belanja", "Toko Retail", 210, None, None, None),
    ("XIAOMI", "substring", "Belanja", "Toko Retail", 210, None, None, None),
    ("CELL", "substring", "Belanja", "Toko Retail", 210, None, None, None),

    # Priority 220 (subscriptions)
    ("GOOGLE PLAY", "substring", "Lain-lain", "Subscriptions", 220, None, None, None),
    ("NETFLIX", "substring", "Lain-lain", "Subscriptions", 220, None, None, None),
    ("SPOTIFY", "substring", "Lain-lain", "Subscriptions", 220, None, None, None),
]

ALLOWED_MATCH_TYPES = ("substring", "exact", "regex")


def _resolve_connection(db_path: Path | str | sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
    if isinstance(db_path, sqlite3.Connection):
        return db_path, False
    if db_path is None:
        try:
            from aturuang.config import DB_FILE
            target_path = DB_FILE
        except ImportError:
            target_path = Path(__file__).resolve().parent.parent / "runtime" / "money_tracks.db"
    else:
        target_path = Path(db_path)
    con = sqlite3.connect(str(target_path))
    con.row_factory = sqlite3.Row
    return con, True


def init_merchant_rules_schema(con_or_path: sqlite3.Connection | str | Path | None = None) -> None:
    """
    Initializes merchant_categories and merchant_category_rules tables and indices idempotently,
    and seeds default categories and rules if not already seeded.
    Also ensures parent_category and child_category columns exist in transactions table.
    """
    con, should_close = _resolve_connection(con_or_path)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS merchant_categories (
              id                   INTEGER PRIMARY KEY AUTOINCREMENT,
              parent_name          TEXT NOT NULL,
              child_name           TEXT NOT NULL,
              display_order        INTEGER NOT NULL DEFAULT 100,
              is_active            INTEGER NOT NULL DEFAULT 1,
              created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(parent_name, child_name)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS merchant_category_rules (
              id                    INTEGER PRIMARY KEY AUTOINCREMENT,
              merchant_pattern      TEXT NOT NULL,
              match_type            TEXT NOT NULL DEFAULT 'substring',
              parent_category       TEXT NOT NULL,
              child_category        TEXT NOT NULL,
              priority              INTEGER NOT NULL DEFAULT 100,
              amount_threshold      REAL NULL,
              amount_below_child    TEXT NULL,
              amount_above_child    TEXT NULL,
              is_active             INTEGER NOT NULL DEFAULT 1,
              notes                 TEXT NULL,
              created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_rules_active_priority
              ON merchant_category_rules (is_active, priority)
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_rules_pattern
              ON merchant_category_rules (merchant_pattern)
            """
        )

        # Check transactions table columns if transactions table exists
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'")
        if cur.fetchone():
            cur_info = con.execute("PRAGMA table_info(transactions)")
            existing_cols = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cur_info.fetchall()}
            if "parent_category" not in existing_cols:
                con.execute("ALTER TABLE transactions ADD COLUMN parent_category TEXT NULL")
            if "child_category" not in existing_cols:
                con.execute("ALTER TABLE transactions ADD COLUMN child_category TEXT NULL")

        seed_categories(con)
        seed_default_rules(con)
        con.commit()
    finally:
        if should_close:
            con.close()


def seed_categories(con: sqlite3.Connection) -> int:
    """Inserts all 27 parent+child categories idempotently."""
    count = 0
    for order, (parent, child) in enumerate(SEED_CATEGORIES, start=1):
        cur = con.execute(
            """
            INSERT OR IGNORE INTO merchant_categories (parent_name, child_name, display_order, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (parent, child, order * 10),
        )
        if cur.rowcount > 0:
            count += 1
    return count


def seed_default_rules(con: sqlite3.Connection, force: bool = False) -> int:
    """Inserts default rules once. If rules already exist and force is False, skips."""
    cur = con.execute("SELECT COUNT(*) FROM merchant_category_rules")
    existing_count = cur.fetchone()[0]
    if existing_count > 0 and not force:
        return 0

    inserted = 0
    for pattern, m_type, parent, child, priority, threshold, below_child, above_child in SEED_RULES:
        con.execute(
            """
            INSERT INTO merchant_category_rules
            (merchant_pattern, match_type, parent_category, child_category,
             priority, amount_threshold, amount_below_child, amount_above_child, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (pattern, m_type, parent, child, priority, threshold, below_child, above_child),
        )
        inserted += 1
    return inserted


def extract_qris_merchant(description: str) -> str:
    """
    Extracts the merchant name from a QRIS transaction description.
    """
    desc = (description or "").strip()
    if "qris" not in desc.lower():
        return ""

    # 1. ke <merchant>
    m = re.search(r'(?:berhasil\s+)?ke\s+([^,\d\n]+?)(?:\s+sebesar|\s+rp|\s+berhasil|\s+pada|$)', desc, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip()

    # 2. di <merchant>
    m = re.search(r'\bdi\s+([^,\d\n]+?)(?:\s+sebesar|\s+rp|\s+berhasil|\s+pada|$)', desc, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip()

    # 3. QRIS [-:] <merchant>
    m = re.search(r'qris\s*[-:]\s*([^,\d\n]+?)(?:\s+sebesar|\s+rp|\s+berhasil|\s+pada|$)', desc, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip()

    # 4. QRIS <merchant>
    m = re.search(r'qris(?:\s+bca)?\s+([^,\d\n]+?)(?:\s+sebesar|\s+rp|\s+berhasil|\s+pada|$)', desc, re.I)
    if m and m.group(1).strip():
        cand = m.group(1).strip()
        if cand.lower() not in ("payment", "berhasil", "bca"):
            return cand

    # Fallback: remove 'qris' and common noise words
    cleaned = re.sub(r'(?i)\b(transaksi|pembayaran|qris|bca|berhasil)\b', '', desc)
    cleaned = re.sub(r'[-:,]', ' ', cleaned).strip()
    return cleaned or desc


def classify_merchant(
    merchant_name: str,
    amount: float,
    db_path: Path | str | sqlite3.Connection | None = None,
) -> tuple[str, str]:
    """
    Returns (parent_category, child_category).
    Returns ("Lain-lain", "Other / Miscellaneous") if no match.
    """
    con, should_close = _resolve_connection(db_path)
    try:
        # Load child to parent mapping
        child_to_parent: dict[str, str] = {}
        cur_cats = con.execute("SELECT child_name, parent_name FROM merchant_categories WHERE is_active = 1")
        for row in cur_cats.fetchall():
            child_to_parent[row[0]] = row[1]

        # Load active rules ordered by priority ASC
        cur_rules = con.execute(
            """
            SELECT merchant_pattern, match_type, parent_category, child_category,
                   priority, amount_threshold, amount_below_child, amount_above_child
            FROM merchant_category_rules
            WHERE is_active = 1
            ORDER BY priority ASC, id ASC
            """
        )
        rules = cur_rules.fetchall()

        m_raw = str(merchant_name or "").strip()
        m_lower = m_raw.lower()

        for row in rules:
            pat = row[0]
            m_type = (row[1] or "substring").lower()
            parent = row[2]
            child = row[3]
            threshold = row[5]
            below_child = row[6]
            above_child = row[7]

            matched = False
            if m_type == "substring":
                matched = pat.lower() in m_lower
            elif m_type == "exact":
                matched = pat.lower() == m_lower
            elif m_type == "regex":
                try:
                    matched = bool(re.search(pat, m_raw, re.IGNORECASE))
                except re.error:
                    matched = False

            if matched:
                if threshold is None:
                    final_parent = child_to_parent.get(child, parent)
                    return final_parent, child
                else:
                    amt_val = float(amount or 0.0)
                    if amt_val < float(threshold):
                        selected_child = below_child or child
                    else:
                        selected_child = above_child or child
                    final_parent = child_to_parent.get(selected_child, parent)
                    return final_parent, selected_child

        return "Lain-lain", "Other / Miscellaneous"
    finally:
        if should_close:
            con.close()


def reclassify_all_transactions(
    db_path: Path | str | sqlite3.Connection | None = None,
    dry_run: bool = True,
) -> dict:
    """
    For each transaction with QRIS description, extract merchant,
    call classify_merchant, update parent_category + child_category
    columns.
    Return {"updated": N, "skipped": M}.
    """
    con, should_close = _resolve_connection(db_path)
    try:
        # Ensure schema has parent_category and child_category columns
        cur_info = con.execute("PRAGMA table_info(transactions)")
        existing_cols = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cur_info.fetchall()}
        if "parent_category" not in existing_cols:
            con.execute("ALTER TABLE transactions ADD COLUMN parent_category TEXT NULL")
        if "child_category" not in existing_cols:
            con.execute("ALTER TABLE transactions ADD COLUMN child_category TEXT NULL")

        rows = con.execute("SELECT id, description, amount FROM transactions WHERE is_deleted = 0").fetchall()
        updated_count = 0
        skipped_count = 0

        for row in rows:
            tx_id = row[0]
            desc = row[1] or ""
            amt = float(row[2] or 0.0)

            if "qris" not in desc.lower():
                skipped_count += 1
                continue

            merchant = extract_qris_merchant(desc)
            parent, child = classify_merchant(merchant, amt, db_path=con)

            if not dry_run:
                con.execute(
                    """
                    UPDATE transactions
                    SET parent_category = ?, child_category = ?, category = ?
                    WHERE id = ?
                    """,
                    (parent, child, child, tx_id),
                )
            updated_count += 1

        return {"updated": updated_count, "skipped": skipped_count}
    finally:
        if should_close:
            con.close()


def get_all_merchant_categories(con: sqlite3.Connection) -> list[dict]:
    """Returns all merchant categories ordered by display_order, id."""
    cur = con.execute(
        """
        SELECT id, parent_name, child_name, display_order, is_active, created_at
        FROM merchant_categories
        ORDER BY display_order ASC, id ASC
        """
    )
    rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "parent_name": r[1],
            "child_name": r[2],
            "display_order": r[3],
            "is_active": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]


def get_all_merchant_rules(con: sqlite3.Connection) -> list[dict]:
    """Returns all merchant rules ordered by priority ASC, id ASC."""
    cur = con.execute(
        """
        SELECT id, merchant_pattern, match_type, parent_category, child_category,
               priority, amount_threshold, amount_below_child, amount_above_child,
               is_active, notes, created_at, updated_at
        FROM merchant_category_rules
        ORDER BY priority ASC, id ASC
        """
    )
    rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "merchant_pattern": r[1],
            "match_type": r[2],
            "parent_category": r[3],
            "child_category": r[4],
            "priority": r[5],
            "amount_threshold": r[6],
            "amount_below_child": r[7],
            "amount_above_child": r[8],
            "is_active": r[9],
            "notes": r[10],
            "created_at": r[11],
            "updated_at": r[12],
        }
        for r in rows
    ]


def _validate_category_pair(con: sqlite3.Connection, parent: str, child: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM merchant_categories WHERE parent_name = ? AND child_name = ? AND is_active = 1",
        (parent, child),
    )
    return cur.fetchone() is not None


def _validate_child_exists(con: sqlite3.Connection, child: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM merchant_categories WHERE child_name = ? AND is_active = 1",
        (child,),
    )
    return cur.fetchone() is not None


def create_merchant_rule(con: sqlite3.Connection, payload: dict[str, Any]) -> tuple[dict, int]:
    """Validates and creates a new merchant rule."""
    pattern = str(payload.get("merchant_pattern", "")).strip()
    if not pattern:
        return {"error": "merchant_pattern wajib diisi."}, 400

    match_type = str(payload.get("match_type", "substring")).strip().lower()
    if match_type not in ALLOWED_MATCH_TYPES:
        return {"error": f"match_type tidak valid. Pilihan: {', '.join(ALLOWED_MATCH_TYPES)}."}, 400

    parent = str(payload.get("parent_category", "")).strip()
    child = str(payload.get("child_category", "")).strip()
    if not _validate_category_pair(con, parent, child):
        return {"error": f"Kombinasi parent '{parent}' dan child '{child}' tidak ditemukan di merchant_categories."}, 400

    try:
        priority = int(payload.get("priority", 100))
        if priority < 0:
            raise ValueError()
    except (TypeError, ValueError):
        return {"error": "priority harus berupa bilangan bulat non-negatif (>= 0)."}, 400

    threshold = payload.get("amount_threshold")
    if threshold is not None and str(threshold).strip() != "":
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            return {"error": "amount_threshold harus berupa angka."}, 400
    else:
        threshold = None

    below_child = payload.get("amount_below_child")
    if below_child:
        below_child = str(below_child).strip()
        if not _validate_child_exists(con, below_child):
            return {"error": f"amount_below_child '{below_child}' tidak valid."}, 400
    else:
        below_child = None

    above_child = payload.get("amount_above_child")
    if above_child:
        above_child = str(above_child).strip()
        if not _validate_child_exists(con, above_child):
            return {"error": f"amount_above_child '{above_child}' tidak valid."}, 400
    else:
        above_child = None

    notes = str(payload.get("notes", "")).strip() or None

    cur = con.execute(
        """
        INSERT INTO merchant_category_rules
        (merchant_pattern, match_type, parent_category, child_category,
         priority, amount_threshold, amount_below_child, amount_above_child,
         is_active, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (pattern, match_type, parent, child, priority, threshold, below_child, above_child, notes),
    )
    return {"status": "success", "rule_id": cur.lastrowid}, 201


def update_merchant_rule(con: sqlite3.Connection, rule_id: int, payload: dict[str, Any]) -> tuple[dict, int]:
    """Validates and updates an existing merchant rule."""
    cur = con.execute("SELECT * FROM merchant_category_rules WHERE id = ?", (rule_id,))
    row = cur.fetchone()
    if not row:
        return {"error": "Aturan merchant tidak ditemukan."}, 404

    existing = dict(row) if isinstance(row, sqlite3.Row) else {
        "merchant_pattern": row[1],
        "match_type": row[2],
        "parent_category": row[3],
        "child_category": row[4],
        "priority": row[5],
        "amount_threshold": row[6],
        "amount_below_child": row[7],
        "amount_above_child": row[8],
        "is_active": row[9],
        "notes": row[10],
    }

    pattern = str(payload.get("merchant_pattern", existing["merchant_pattern"])).strip()
    if not pattern:
        return {"error": "merchant_pattern tidak boleh kosong."}, 400

    match_type = str(payload.get("match_type", existing["match_type"])).strip().lower()
    if match_type not in ALLOWED_MATCH_TYPES:
        return {"error": f"match_type tidak valid. Pilihan: {', '.join(ALLOWED_MATCH_TYPES)}."}, 400

    parent = str(payload.get("parent_category", existing["parent_category"])).strip()
    child = str(payload.get("child_category", existing["child_category"])).strip()
    if not _validate_category_pair(con, parent, child):
        return {"error": f"Kombinasi parent '{parent}' dan child '{child}' tidak ditemukan di merchant_categories."}, 400

    try:
        priority = int(payload.get("priority", existing["priority"]))
        if priority < 0:
            raise ValueError()
    except (TypeError, ValueError):
        return {"error": "priority harus berupa bilangan bulat non-negatif (>= 0)."}, 400

    if "amount_threshold" in payload:
        threshold_raw = payload["amount_threshold"]
        if threshold_raw is not None and str(threshold_raw).strip() != "":
            try:
                threshold = float(threshold_raw)
            except (TypeError, ValueError):
                return {"error": "amount_threshold harus berupa angka."}, 400
        else:
            threshold = None
    else:
        threshold = existing["amount_threshold"]

    if "amount_below_child" in payload:
        below_child = payload["amount_below_child"]
        if below_child:
            below_child = str(below_child).strip()
            if not _validate_child_exists(con, below_child):
                return {"error": f"amount_below_child '{below_child}' tidak valid."}, 400
        else:
            below_child = None
    else:
        below_child = existing["amount_below_child"]

    if "amount_above_child" in payload:
        above_child = payload["amount_above_child"]
        if above_child:
            above_child = str(above_child).strip()
            if not _validate_child_exists(con, above_child):
                return {"error": f"amount_above_child '{above_child}' tidak valid."}, 400
        else:
            above_child = None
    else:
        above_child = existing["amount_above_child"]

    is_active = int(payload.get("is_active", existing["is_active"]))
    notes = str(payload.get("notes", existing["notes"] or "")).strip() or None

    con.execute(
        """
        UPDATE merchant_category_rules
        SET merchant_pattern = ?, match_type = ?, parent_category = ?, child_category = ?,
            priority = ?, amount_threshold = ?, amount_below_child = ?, amount_above_child = ?,
            is_active = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (pattern, match_type, parent, child, priority, threshold, below_child, above_child, is_active, notes, rule_id),
    )
    return {"status": "success", "rule_id": rule_id}, 200


def delete_merchant_rule(con: sqlite3.Connection, rule_id: int) -> tuple[dict, int]:
    """Soft-deletes a merchant rule by setting is_active = 0."""
    cur = con.execute(
        "UPDATE merchant_category_rules SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (rule_id,),
    )
    if cur.rowcount == 0:
        return {"error": "Aturan merchant tidak ditemukan."}, 404
    return {"status": "success", "deleted_id": rule_id}, 200
