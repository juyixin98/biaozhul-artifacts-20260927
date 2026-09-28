"""Hand-built synthetic fixtures with concrete, hand-computed expectations.

Nothing here is derived from the code under test.  Values were computed from
Unicode code charts:

* U+00E9 LATIN SMALL LETTER E WITH ACUTE   → UTF-8 C3 A9 (2 bytes)
* U+0065 e, U+0301 COMBINING ACUTE ACCENT  → 65 CC 81 (1+2 = 3 bytes)
* U+1F600                                    → F0 9F 98 80 (4 bytes)
* U+200D ZWJ                                → E2 80 8D (3 bytes)
* U+1F1FA REGIONAL INDICATOR SYMBOL LETTER U → F0 9F 87 BA
* U+1F1F8 ... LETTER S                      → F0 9F 87 B8
* U+1F3FD EMOJI MODIFIER FITZPATRICK TYPE-4 → F0 9F 8F BD
* CR U+000D, LF U+000A                      → CR×LF is ONE grapheme cluster
"""

from __future__ import annotations

# --- Fixture A: plain + precomposed + decomposed accented -----------------
A_TEXT = "aé é"
# code points: a(1) é(1) space(1) e(1) combining-acute(1) = 5 cps
# bytes:       61   C3A9    20       65    CC81           = 7 bytes
# clusters:    | a | é | _ | e+U+0301 |                  = 4
A_EXPECTED = {
    "codepoints": 5,
    "bytes": 7,
    "clusters": ["a", "é", " ", "é"],
    "cluster_cp_starts": [0, 1, 2, 3, 5],
    "cluster_byte_starts": [0, 1, 3, 4, 7],
}

# --- Fixture B: flag (paired RIS) then ascii --------------------------------
B_TEXT = "\U0001F1FA\U0001F1F8x"
# cps 3; bytes 4+4+1 = 9; clusters 2: [US flag][x]
B_EXPECTED = {
    "codepoints": 3,
    "bytes": 9,
    "clusters": ["\U0001F1FA\U0001F1F8", "x"],
    "cluster_cp_starts": [0, 2, 3],
    "cluster_byte_starts": [0, 8, 9],
}

# --- Fixture C: three regional indicators: (U S) pair + lone J -------------
C_TEXT = "\U0001F1FA\U0001F1F8\U0001F1EF"
# cps 3; clusters 2: [U S][J]; byte starts 0,8,12
C_EXPECTED = {
    "codepoints": 3,
    "bytes": 12,
    "clusters": ["\U0001F1FA\U0001F1F8", "\U0001F1EF"],
    "cluster_cp_starts": [0, 2, 3],
    "cluster_byte_starts": [0, 8, 12],
}

# --- Fixture D: ZWJ family MAN ZWJ WOMAN ZWJ GIRL ---------------------------
D_TEXT = "\U0001F468‍\U0001F469‍\U0001F467"
# cps 5; byte lengths 4+3+4+3+4 = 18; exactly one cluster
D_EXPECTED = {
    "codepoints": 5,
    "bytes": 18,
    "clusters": [D_TEXT],
    "cluster_cp_starts": [0, 5],
    "cluster_byte_starts": [0, 18],
}

# --- Fixture E: waving hand + skin tone modifier ----------------------------
E_TEXT = "\U0001F44B\U0001F3FD"
# cps 2; bytes 8; one cluster
E_EXPECTED = {
    "codepoints": 2,
    "bytes": 8,
    "clusters": [E_TEXT],
    "cluster_cp_starts": [0, 2],
    "cluster_byte_starts": [0, 8],
}

# --- Fixture F: CRLF and controls -------------------------------------------
F_TEXT = "a\r\nb\rc\nd"
# cps: a CR LF b CR c LF d = 8
# clusters (GB3 CR×LF): [a][CR LF][b][CR][c][LF][d] = 7
# bytes equal cps here = 8
F_EXPECTED = {
    "codepoints": 8,
    "bytes": 8,
    "clusters": ["a", "\r\n", "b", "\r", "c", "\n", "d"],
    "cluster_cp_starts": [0, 1, 3, 4, 5, 6, 7, 8],
    "cluster_byte_starts": [0, 1, 3, 4, 5, 6, 7, 8],
}

# --- Fixture G: everything adjacent -----------------------------------------
G_TEXT = "x\U0001F1FA\U0001F1F8é\U0001F468‍\U0001F469‍\U0001F467z"
# cps: x(1) U(1) S(1) e(1) comb(1) man(1) zwj(1) woman(1) zwj(1) girl(1) z(1)
#    = 11 cps
# bytes: 1 + 4 + 4 + 1 + 2 + 4 + 3 + 4 + 3 + 4 + 1 = 31
# clusters: [x][US][e+comb][family][z] = 5
G_EXPECTED = {
    "codepoints": 11,
    "bytes": 31,
    "clusters": [
        "x",
        "\U0001F1FA\U0001F1F8",
        "é",
        "\U0001F468‍\U0001F469‍\U0001F467",
        "z",
    ],
    "cluster_cp_starts": [0, 1, 3, 5, 10, 11],
    "cluster_byte_starts": [0, 1, 9, 12, 30, 31],
}

# --- Fixture H: four RIS (two flags) ----------------------------------------
H_TEXT = "\U0001F1FA\U0001F1F8\U0001F1EF\U0001F1F5"
# clusters [US][JP] = 2; cp starts 0,2,4; byte starts 0,8,16
H_EXPECTED = {
    "codepoints": 4,
    "bytes": 16,
    "clusters": [H_TEXT[:2], H_TEXT[2:]],
    "cluster_cp_starts": [0, 2, 4],
    "cluster_byte_starts": [0, 8, 16],
}

# --- Invalid byte sequences (raw upload) ------------------------------------
INVALID_UTF8 = {
    "surrogate_encoded": bytes([0xED, 0xA0, 0x80]),     # U+D800
    "overlong_slash": bytes([0xC0, 0xAF]),              # overlong U+002F
    "bare_continuation": b"a\x80b",
    "truncated_3byte": "é".encode() + bytes([0xE2, 0x82]),
    "raw_ff": bytes([0xFF]),
}

# --- JSON text containing a lone surrogate (via \u escape) ------------------
JSON_LONE_SURROGATE = '"\\ud800"'

ALL_TEXT_FIXTURES = {
    "A": (A_TEXT, A_EXPECTED),
    "B": (B_TEXT, B_EXPECTED),
    "C": (C_TEXT, C_EXPECTED),
    "D": (D_TEXT, D_EXPECTED),
    "E": (E_TEXT, E_EXPECTED),
    "F": (F_TEXT, F_EXPECTED),
    "G": (G_TEXT, G_EXPECTED),
    "H": (H_TEXT, H_EXPECTED),
}
