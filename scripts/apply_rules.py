"""
AturUang Rules Engine v6 — single source of truth untuk klasifikasi tx.

Dipakai oleh:
- import_orchestrator.py (saat import baru)
- backfill script

Rule priority (first match wins):
  1. counterparty_accounts  — nomor akun spesifik
  2. va_prefix_rules        — VA prefix BCA (12208, 70001, dll)
  3. merchant_rules         — keyword contains, sorted by priority ASC
"""
import sqlite3, re
from pathlib import Path

DB_DEFAULT = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang-activation-v1\runtime\money_tracks.db")


class RuleEngine:
    def __init__(self, db_path=None):
        self.db = str(db_path or DB_DEFAULT)
        self.con = sqlite3.connect(self.db)
        self._load()
    
    def _load(self):
        self.va_rules = list(self.con.execute("""
            SELECT va_prefix, category, transaction_type, exclude_from_budget
            FROM va_prefix_rules WHERE enabled = 1 ORDER BY COALESCE(priority,100)
        """))
        self.mr = []
        for rid, mt, pat, cat, ttype, excl, prio in self.con.execute("""
            SELECT id, match_type, pattern, category, transaction_type,
                   exclude_from_budget, priority
            FROM merchant_rules WHERE enabled = 1 ORDER BY priority, id
        """):
            self.mr.append({"id": rid, "mt": mt, "pat": pat, "cat": cat,
                            "ttype": ttype, "excl": excl, "prio": prio})
        self.cp = []
        for acct, owner, rel in self.con.execute("""
            SELECT account_number, owner_name, relation FROM counterparty_accounts
        """):
            self.cp.append({"acct": acct.replace("*", ""), "owner": owner, "rel": rel})
    
    def apply(self, tx):
        """tx: dict {description, transaction_type} -> dict hasil atau None."""
        desc = tx.get("description") or ""
        ttype = tx.get("transaction_type")
        
        # 1. Counterparty
        m = re.search(r"(?:BANK JAGO|BCA|BNI|BRI|MANDIRI|SEABANK|JAGO|BLU)\s+(\*?\d{3,20})", desc, re.I)
        if m:
            acct = m.group(1).replace("*", "")
            for cp in self.cp:
                if acct == cp["acct"] or (len(cp["acct"]) >= 4 and acct.endswith(cp["acct"][-6:])):
                    rel = cp["rel"]
                    if rel == "self":
                        return {"category": "Allocation Movement", "transaction_type": "Transfer",
                                "exclude_from_budget": 1, "reason": f"self: {cp['owner']}",
                                "source": "counterparty_accounts", "confidence": 0.95}
                    cat = {
                        "family": "Transfer ke Keluarga" if ttype != "Income" else "Penerimaan dari Keluarga",
                        "partner": "Transfer ke Partner" if ttype != "Income" else "Penerimaan dari Partner",
                        "friend": "Transfer ke Teman" if ttype != "Income" else "Penerimaan dari Teman",
                        "vendor": "Pembayaran Vendor",
                        "intermediary": "Remitansi",
                    }.get(rel, "Transfer ke Pihak Lain")
                    return {"category": cat, "transaction_type": ttype,
                            "exclude_from_budget": 0, "reason": f"{cp['owner']} ({rel})",
                            "source": "counterparty_accounts", "confidence": 0.9}
        
        # 2. VA prefix
        m = re.search(r"FTFVA/WS\d+\s+(\d+)/", desc)
        if m:
            for va, cat, ttype_v, excl in self.va_rules:
                if va == m.group(1):
                    return {"category": cat,
                            "transaction_type": ttype_v or ttype,
                            "exclude_from_budget": excl if excl is not None else 0,
                            "reason": f"VA {va}",
                            "source": "va_prefix_rules", "confidence": 0.9}
        
        # 3. Merchant rules (priority ASC)
        for r in self.mr:
            hit = False
            if r["mt"] == "contains":
                hit = r["pat"].lower() in desc.lower()
            elif r["mt"] == "exact":
                hit = r["pat"] == desc
            elif r["mt"] == "regex":
                try: hit = bool(re.search(r["pat"], desc, re.I))
                except: pass
            if hit:
                return {"category": r["cat"],
                        "transaction_type": r["ttype"] or ttype,
                        "exclude_from_budget": r["excl"] if r["excl"] is not None else 0,
                        "reason": f"rule '{r['pat']}'",
                        "source": "merchant_rules",
                        "rule_id": r["id"], "confidence": 0.7}
        
        # 4. No match
        return None
    
    def close(self):
        self.con.close()
