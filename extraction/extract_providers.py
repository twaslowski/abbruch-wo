"""
extract_providers.py
--------------------
Parses the Bundesärztekammer § 13 Abs. 5 SchKG provider list PDF and
loads each entry into a SQLite database (providers.db).

Usage:
    python extract_providers.py [path/to/pdf] [path/to/output.db]

Defaults:
    PDF  → 20260605Liste___13_Abs_5_SchKG.pdf  (same directory)
    DB   → providers.db                          (same directory)
"""

import re
import sqlite3
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. Text extraction
# ---------------------------------------------------------------------------

def extract_text(pdf_path: str) -> str:
    """Extract the full text of the PDF using pdftotext with layout mode."""
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True, check=True
    )
    return result.stdout


# ---------------------------------------------------------------------------
# 2. Parsing helpers
# ---------------------------------------------------------------------------

# Sentinel strings that appear verbatim in the PDF
_SKIP_VALUES = {"k.a.", "--", "k. a."}

def _clean(value: str | None) -> str | None:
    """Strip whitespace; return None for empty / placeholder values."""
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower() in _SKIP_VALUES:
        return None
    return v


def _bool_field(value: str | None) -> bool | None:
    """Convert 'ja' / 'nein' to True / False; None if missing."""
    if value is None:
        return None
    v = value.strip().lower()
    if v == "ja":
        return True
    if v == "nein":
        return False
    return None


# ---------------------------------------------------------------------------
# 3. Block splitter
# ---------------------------------------------------------------------------

# Each entry starts with an institution / practice name on its own line,
# followed by a doctor name (optional) and then the address.
# The entries are separated by blank lines.  We split on the "Methoden zum"
# line which always ends an entry.

_ENTRY_SEPARATOR = re.compile(
    r"Methoden zum\s+Medikamentös:\s*(ja|nein)?\s*"
    r"Schwangerschaftsabbruch:\s+Operativ:\s*(ja|nein)?",
    re.DOTALL,
)

# Alternative pattern: some entries spread across two lines differently
_ENTRY_SEPARATOR_ALT = re.compile(
    r"Schwangerschaftsabbruch:\s+Operativ:\s*(ja|nein)?",
    re.DOTALL,
)


def split_into_blocks(full_text: str) -> list[str]:
    """
    Return a list of raw text blocks, one per provider entry.
    We locate every 'Methoden zum … Schwangerschaftsabbruch:' section and
    use it as the end-of-block marker.
    """
    # Remove page numbers and form-feed characters
    text = re.sub(r'\f', '\n', full_text)
    text = re.sub(r'\n\s*\d+\s*\n', '\n', text)

    # Strip the header line
    text = re.sub(
        r'Liste der Bundesärztekammer.*?Stand:.*?\n', '', text, flags=re.DOTALL
    )

    # We'll walk through and collect blocks by finding the pattern
    # "Methoden zum … Schwangerschaftsabbruch: Operativ: <val>"
    # which terminates each entry.
    combined_pattern = re.compile(
        r'(Methoden zum\s+Medikamentös:\s*(?:ja|nein)?'
        r'\s*Schwangerschaftsabbruch:\s*Operativ:\s*(?:ja|nein)?)',
        re.DOTALL,
    )

    parts = combined_pattern.split(text)
    # parts alternates: [pre-block, separator, pre-block, separator, ...]
    blocks = []
    i = 0
    while i < len(parts) - 1:
        body = parts[i].strip()
        trailer = parts[i + 1].strip()  # the "Methoden zum …" section
        if body:
            blocks.append(body + "\n" + trailer)
        i += 2
    return blocks


# ---------------------------------------------------------------------------
# 4. Per-block parser
# ---------------------------------------------------------------------------

def parse_block(block: str) -> dict | None:
    """
    Parse a single raw text block into a structured dict.
    Returns None if the block is too short / malformed.
    """
    lines = [l.rstrip() for l in block.splitlines()]
    lines = [l for l in lines if l.strip()]  # drop blank lines

    if len(lines) < 3:
        return None

    # ---- Institution / practice name and doctor name ----
    # The first non-empty line(s) before the address are the institution/name.
    # The address line contains a comma and a 5-digit postal code.
    addr_idx = None
    for idx, line in enumerate(lines):
        # Address pattern: "Street …, 12345 City"
        if re.search(r',\s*\d{5}\s+\S', line):
            addr_idx = idx
            break

    if addr_idx is None:
        return None

    # Everything before the address line forms institution + doctor name
    header_lines = [l.strip() for l in lines[:addr_idx] if l.strip()]
    if not header_lines:
        return None

    # If there are two header lines, first = institution, second = doctor.
    # If only one, it's the institution (which may itself contain a name).
    if len(header_lines) >= 2:
        institution = header_lines[0]
        doctor      = header_lines[1]
        # Sometimes there's a third header line (e.g. very long institution names)
        # — append to institution if it looks like a continuation
        for extra in header_lines[2:]:
            # If the extra line doesn't start with a title-like word or name it's
            # probably part of institution
            institution = institution + " " + extra
    else:
        institution = header_lines[0]
        doctor      = None

    address = lines[addr_idx].strip()

    # ---- Key-value fields ----
    remaining = " ".join(lines[addr_idx + 1:])

    def extract_field(label: str) -> str | None:
        """Pull the value that appears right after 'label:' up to the next label."""
        pattern = re.compile(
            label + r':\s*(.*?)(?=Telefon:|Internet:|E-Mailadresse:|Fremdsprachen:|Methoden zum|Schwangerschaftsabbruch:|Medikamentös:|Operativ:|$)',
            re.DOTALL | re.IGNORECASE,
        )
        m = pattern.search(remaining)
        if m:
            return _clean(m.group(1))
        return None

    phone    = extract_field("Telefon")
    internet = extract_field("Internet")
    email    = extract_field("E-Mailadresse")
    langs_raw = extract_field("Fremdsprachen")

    languages: list[str] = []
    if langs_raw:
        # Split on comma or whitespace runs
        parts = re.split(r'[,;]+', langs_raw)
        languages = [p.strip() for p in parts if p.strip()]

    # ---- Methods ----
    medik_match = re.search(r'Medikamentös:\s*(ja|nein)', remaining, re.IGNORECASE)
    operat_match = re.search(r'Operativ:\s*(ja|nein)', remaining, re.IGNORECASE)

    method_medicinal  = _bool_field(medik_match.group(1) if medik_match else None)
    method_surgical   = _bool_field(operat_match.group(1) if operat_match else None)

    # ---- Combine institution + doctor into "praxis" field ----
    praxis = institution
    if doctor:
        praxis = f"{institution}\n{doctor}"

    return {
        "praxis":            praxis,
        "institution":       institution,
        "doctor":            _clean(doctor),
        "address":           address,
        "phone":             phone,
        "email":             email,
        "website":           internet,
        "languages":         ",".join(languages) if languages else "",
        "method_medicinal":  method_medicinal,
        "method_surgical":   method_surgical,
    }


# ---------------------------------------------------------------------------
# 5. Database
# ---------------------------------------------------------------------------

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS providers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    praxis            TEXT NOT NULL,
    institution       TEXT NOT NULL,
    doctor            TEXT,
    address           TEXT NOT NULL,
    phone             TEXT,
    email             TEXT,
    website           TEXT,
    languages         TEXT,        -- comma-separated list
    method_medicinal  INTEGER,     -- 1=ja, 0=nein, NULL=unknown
    method_surgical   INTEGER      -- 1=ja, 0=nein, NULL=unknown
);
"""

INSERT_ROW = """
INSERT INTO providers
    (praxis, institution, doctor, address, phone, email, website,
     languages, method_medicinal, method_surgical)
VALUES
    (:praxis, :institution, :doctor, :address, :phone, :email, :website,
     :languages, :method_medicinal, :method_surgical);
"""


def save_to_db(records: list[dict], db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()
    cur.executescript(CREATE_TABLE)

    # Convert booleans to SQLite integers
    def prep(r: dict) -> dict:
        out = dict(r)
        for key in ("method_medicinal", "method_surgical"):
            v = out[key]
            if v is True:
                out[key] = 1
            elif v is False:
                out[key] = 0
            else:
                out[key] = None
        return out

    cur.executemany(INSERT_ROW, [prep(r) for r in records])
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 6. Main
# ---------------------------------------------------------------------------

def main():
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "20260605Liste___13_Abs_5_SchKG.pdf"
    db_path  = sys.argv[2] if len(sys.argv) > 2 else "providers.db"

    print(f"Reading PDF: {pdf_path}")
    full_text = extract_text(pdf_path)

    print("Splitting into blocks …")
    blocks = split_into_blocks(full_text)
    print(f"  Found {len(blocks)} raw blocks")

    records = []
    skipped = 0
    for i, block in enumerate(blocks):
        parsed = parse_block(block)
        if parsed:
            records.append(parsed)
        else:
            skipped += 1
            # Uncomment to debug malformed blocks:
            # print(f"  [SKIP block {i}]\n{block[:200]}\n")

    print(f"  Parsed:  {len(records)} entries")
    print(f"  Skipped: {skipped} malformed blocks")

    print(f"Saving to database: {db_path}")
    save_to_db(records, db_path)
    print("Done.")

    # Quick sanity check
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
    sample = conn.execute(
        "SELECT praxis, address, phone, languages, method_medicinal, method_surgical "
        "FROM providers LIMIT 3"
    ).fetchall()
    conn.close()

    print(f"\nTotal rows in DB: {count}")
    print("\nSample rows:")
    for row in sample:
        print(" ", row)


if __name__ == "__main__":
    main()
