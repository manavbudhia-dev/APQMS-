"""
Fuzzy matching of what the OCR read against what is already in the app:
line items -> the item catalog, and the customer -> the customers table.

Pure functions over plain dicts/rows (no database access), so they are
easy to test. The point of catalog matching is that an enquiry mentions
"Sugar elevator chain 66.6mm pitch", while the quotation should use the
catalog's proper name, description, specification and rate.

Numbers are treated as the strongest evidence: "Roller Chain 25.4 mm pitch"
and "Roller Chain 31.75 mm pitch" are different products that differ by one
token, so a query whose numbers don't appear in a candidate is penalised
hard rather than being matched on the shared words alone.
"""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

ITEM_LINK_THRESHOLD = 0.60      # at or above: pre-link the row to the catalog item

_STOPWORDS = {'of', 'the', 'and', 'for', 'with', 'type', 'size', 'a', 'an', 'to', 'as', 'per', 'in'}


def _num(m):
    v = ('%f' % float(m.group(0))).rstrip('0').rstrip('.')
    return v or '0'


def tokens(text):
    """Lower-case word/number tokens: '66.60MM' -> ['66.6', 'mm']."""
    s = (text or '').lower().replace('×', ' x ')
    s = re.sub(r'(\d)([a-z])', r'\1 \2', s)
    s = re.sub(r'([a-z])(\d)', r'\1 \2', s)
    s = re.sub(r'\d+(?:\.\d+)?', _num, s)
    s = re.sub(r'[^a-z0-9. ]+', ' ', s)
    out = [t.strip('.') for t in s.split()]
    return [t for t in out if t and t not in _STOPWORDS]


def _is_number(tok):
    return bool(re.fullmatch(r'\d+(?:\.\d+)?', tok))


class CatalogIndex:
    """Pre-tokenised catalog with inverse-document-frequency weights, so a
    word every item shares ('chain') counts for less than a distinguishing
    one ('crank') or number ('76.2')."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.toks = [set(tokens(f"{r['item_name']} {r['description'] or ''}")) for r in self.rows]
        df = {}
        for ts in self.toks:
            for t in ts:
                df[t] = df.get(t, 0) + 1
        n = len(self.rows)
        self._df, self._n = df, n

    def weight(self, tok):
        w = math.log((self._n + 1) / (self._df.get(tok, 0) + 1)) + 1.0
        return w * 1.6 if _is_number(tok) else w

    def score(self, q_tokens, idx):
        q, c = set(q_tokens), self.toks[idx]
        if not q or not c:
            return 0.0
        inter = sum(self.weight(t) for t in q & c)
        union = sum(self.weight(t) for t in q | c)
        wq = sum(self.weight(t) for t in q)
        jaccard = inter / union if union else 0.0
        coverage = inter / wq if wq else 0.0
        r = self.rows[idx]
        seq = SequenceMatcher(None, ' '.join(tokens(r['item_name'] + ' ' + (r['description'] or ''))),
                              ' '.join(q_tokens)).ratio()
        s = 0.4 * jaccard + 0.4 * coverage + 0.2 * seq
        qn, cn = {t for t in q if _is_number(t)}, {t for t in c if _is_number(t)}
        if qn and cn and not (qn & cn):
            s *= 0.6                                    # different size/pitch: a different product
        return s

    def best(self, text):
        """-> (catalog row or None, score 0..1). The row is returned only
        when the score reaches ITEM_LINK_THRESHOLD."""
        q = tokens(text)
        if not q or not self.rows:
            return None, 0.0
        scored = [(self.score(q, i), i) for i in range(len(self.rows))]
        top, i = max(scored)
        return (self.rows[i] if top >= ITEM_LINK_THRESHOLD else None), round(top, 3)


_COMPANY_NOISE = {'ltd', 'limited', 'pvt', 'private', 'llp', 'inc', 'co', 'company', 'the', 'and', 'ms', 'm',
                  's', 'corp', 'corporation'}


def _company_key(name):
    return ' '.join(t for t in re.sub(r'[^a-z0-9 ]+', ' ', (name or '').lower()).split()
                    if t not in _COMPANY_NOISE)


def match_customer(gstin, company, customers):
    """-> (customer row or None, how) where how is 'GSTIN' or 'name'.
    A GSTIN match is certain; a name match needs a near-identical name
    ('Krishna Sugar Mills Ltd.' == 'M/s Krishna Sugar Mills Limited')."""
    g = (gstin or '').strip().upper()
    if g:
        for c in customers:
            if (c['gstin'] or '').strip().upper() == g:
                return c, 'GSTIN'
    key = _company_key(company)
    if len(key) >= 4:
        best, best_r = None, 0.0
        for c in customers:
            ck = _company_key(c['company_name'])
            if not ck:
                continue
            r = 1.0 if ck == key else SequenceMatcher(None, ck, key).ratio()
            if r > best_r:
                best, best_r = c, r
        if best is not None and best_r >= 0.88:
            return best, 'name'
    return None, ''
