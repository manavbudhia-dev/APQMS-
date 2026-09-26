"""Unit tests for app/ocr/parser.py and app/ocr/matching.py.
Pure logic on hand-built lines — no OCR, no database, runs in milliseconds."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from app.ocr import matching, parser
from app.ocr.engine import Cell, Line


def L(*cells, y=None, h=10.0, conf=None):
    """A Line from cells: plain strings are spaced 100 apart, or pass
    (text, x0, x1). `y` gives it vertical geometry."""
    cs = []
    for i, c in enumerate(cells):
        cs.append(Cell(c, i * 100.0, i * 100.0 + 90.0) if isinstance(c, str) else Cell(*c))
    return Line(cells=cs, conf=conf, y0=y, y1=None if y is None else y + h)


def text_lines(*rows):
    return [L(r) for r in rows]


class NumbersAndUnits(unittest.TestCase):
    def test_parse_number(self):
        p = parser.parse_number
        self.assertEqual(p('1,20,000.50'), 120000.5)
        self.assertEqual(p('Rs. 2700/-'), 2700)
        self.assertEqual(p('₹ 550'), 550)
        self.assertEqual(p('1O0'), 100)            # OCR: letter O for zero
        self.assertIsNone(p('12x'))
        self.assertIsNone(p(''))
        self.assertIsNone(p(None))

    def test_units(self):
        self.assertEqual(parser.canon_unit('MTRS.'), 'Mtr.')
        self.assertEqual(parser.canon_unit('Meters'), 'Mtr.')
        self.assertEqual(parser.canon_unit('nos'), 'Nos.')
        self.assertEqual(parser.canon_unit('Kgs'), 'Kg.')
        self.assertIsNone(parser.canon_unit('mm'))    # a dimension, not a selling unit
        self.assertIsNone(parser.canon_unit('pitch'))

    def test_dates(self):
        self.assertEqual(parser.parse_date('Date: 12-09-2026'), '12 - 09 - 2026')
        self.assertEqual(parser.parse_date('5/9/26'), '05 - 09 - 2026')
        self.assertEqual(parser.parse_date('12th September 2026'), '12 - 09 - 2026')
        self.assertEqual(parser.parse_date('Sep 12, 2026'), '12 - 09 - 2026')
        self.assertEqual(parser.parse_date('31-02-2026'), '31 - 02 - 2026')  # format only, no calendar check
        self.assertEqual(parser.parse_date('99-99-2026'), '')

    def test_date_survives_a_dropped_dash_separator(self):
        # Extraction drops a lone '-' as noise (see _NOISE_ONLY in engine.py),
        # so "21 - 09 - 2026" can arrive here as "21 09 2026".
        self.assertEqual(parser.parse_date('Date: 21 09 2026'), '21 - 09 - 2026')
        self.assertEqual(parser.parse_date('some 1 2 3 unrelated numbers'), '')  # needs a real 4-digit year

    def test_subject_prefix_is_removed(self):
        # the quotation PDF already prints "Quotation for <subject>"
        self.assertEqual(parser.clean_subject('Enquiry for Chains'), 'Chains')
        self.assertEqual(parser.clean_subject('Request for Quotation for Roller Chain'), 'Roller Chain')
        self.assertEqual(parser.clean_subject('Quotation: Sugar Chain'), 'Sugar Chain')


class HeaderFields(unittest.TestCase):
    def test_gstin_ocr_slips_are_corrected(self):
        self.assertEqual(parser.find_gstin(['GSTIN: O3AABCK1234F1Z5']), '03AABCK1234F1Z5')
        self.assertEqual(parser.find_gstin(['GST No 03 AABCK 1234 F1Z5']), '03AABCK1234F1Z5')

    def test_our_own_gstin_is_never_the_customers(self):
        self.assertEqual(parser.find_gstin(['Supplier GSTIN: 03AAHFA4106G1Z4']), '')
        # ...even when OCR garbled a character of it
        self.assertEqual(parser.find_gstin(['GSTIN 03AAHFA4106G1ZA']), '')
        self.assertEqual(parser.find_gstin(['GSTIN 03AAHFA4106G1Z4', 'GSTIN 03AABCK1234F1Z5']), '03AABCK1234F1Z5')

    def test_own_email_and_phone_ignored(self):
        self.assertEqual(parser.find_email(['mail aypeeinds@yahoo.com', 'x purchase @krishna.in']),
                         'purchase@krishna.in')
        self.assertEqual(parser.find_phone(['Phone: 0181-2672260 Mobile: 9357255758']), '')
        self.assertEqual(parser.find_phone(['Mobile: 98765 43210, 9041881996']), '98765 43210')

    def test_reference(self):
        self.assertEqual(parser.find_reference(['Ref No: KSM/PUR/2026/045']), 'KSM/PUR/2026/045')
        self.assertEqual(parser.find_reference(['PO No: PO/2026/778 Dated: 05/09/2026']), 'PO/2026/778')
        self.assertEqual(parser.find_reference(['Sub: Enquiry for Chains']), '')

    def test_letterhead_customer_when_addressed_to_us(self):
        d = parser.parse(text_lines(
            'KRISHNA SUGAR MILLS LTD.', 'Village Bhogpur, Jalandhar - 144201', 'GSTIN: 03AABCK1234F1Z5',
            'To,', 'M/s Aypee Industries', 'Near Chadha Engg. Works, Jalandhar - 144008',
            'Sub: Enquiry for Chains'))
        self.assertEqual(d.company_name, 'KRISHNA SUGAR MILLS LTD.')
        self.assertNotIn('Chadha', d.address)          # our address, not theirs
        self.assertEqual(d.subject, 'Chains')

    def test_to_block_customer(self):
        d = parser.parse(text_lines(
            'To,', 'The Purchase Manager', 'M/s Bharat Agro Industries Ltd.',
            'Industrial Focal Point, Phase 5', 'Ludhiana 141010', 'Dear Sir,',
            '1. Roller Chain 25.4 mm pitch - 50 Mtrs'))
        self.assertEqual(d.company_name, 'Bharat Agro Industries Ltd.')
        self.assertEqual(d.contact_person, 'Purchase Manager')
        self.assertEqual(d.address, 'Industrial Focal Point, Phase 5\nLudhiana - 141010')


HEAD = ['S.No', 'Description', 'Qty', 'Unit', 'Rate', 'Amount']


class TableMode(unittest.TestCase):
    def cols(self):
        return [('S.No', 0, 60), ('Description', 100, 500), ('Qty', 550, 600),
                ('Unit', 650, 700), ('Rate', 750, 820), ('Amount', 870, 950)]

    def test_basic_rows_and_totals_stop(self):
        d = parser.parse([
            L('Customer Ltd.'), L(*HEAD),
            L('1', 'Sugar Elevator Chain 66.6 MM', '120', 'Mtr.', '2,700.00', '3,24,000.00'),
            L('2', 'Roller Chain', '50', 'Nos', '550', '27,500'),
            L('', 'Total', '', '', '', '3,51,500'),
            L('Terms: 30 days')])
        self.assertEqual(d.mode, 'table')
        self.assertEqual([(i.description, i.quantity, i.unit, i.rate, i.amount) for i in d.items],
                         [('Sugar Elevator Chain 66.6 MM', 120, 'Mtr.', 2700, 324000),
                          ('Roller Chain', 50, 'Nos.', 550, 27500)])
        self.assertEqual(d.items[0].flags, [])

    def test_row_missing_a_cell_is_aligned_by_position(self):
        # OCR: the blank Unit cell isn't there, so this row has one cell fewer than the header
        hdr = L(*[(t, x0, x1) for t, x0, x1 in self.cols()], y=100)
        row = L(('1', 0, 60), ('Roller Chain', 100, 300), ('50', 555, 590), ('550', 760, 800),
                ('27500', 880, 940), y=130)
        d = parser.parse([L('Acme Pvt. Ltd.', y=10), hdr, row])
        it = d.items[0]
        self.assertEqual((it.quantity, it.unit, it.rate, it.amount), (50, '', 550, 27500))

    def test_wrapped_description_lead_line_above_the_numbers_row(self):
        # Vertically-centred cell: first description line sits ABOVE the row holding the numbers
        hdr = L(*[(t, x0, x1) for t, x0, x1 in self.cols()], y=100)
        lead = L(('Double Strand Pintle Chain', 100, 400), y=134)       # 4 below header... no:
        row = L(('1', 0, 60), ('50.80 MM Pitch', 100, 300), ('40', 555, 590), ('Mtr.', 650, 690),
                ('3200', 760, 800), ('128000', 880, 940), y=150)
        d = parser.parse([L('Acme Pvt. Ltd.', y=10), hdr, lead, row])
        self.assertEqual(len(d.items), 1)
        self.assertEqual(d.items[0].description, 'Double Strand Pintle Chain\n50.80 MM Pitch')

    def test_wrapped_description_trailing_line_below_the_row(self):
        hdr = L(*[(t, x0, x1) for t, x0, x1 in self.cols()], y=100)
        r1 = L(('1', 0, 60), ('Sugar Elevator Chain', 100, 300), ('120', 555, 590), ('Mtr.', 650, 690), y=130)
        tail = L(('Plate thickness 5 MM', 100, 300), y=143)               # tight under r1
        r2 = L(('2', 0, 60), ('Roller Chain', 100, 300), ('50', 555, 590), ('Mtr.', 650, 690), y=175)
        d = parser.parse([L('Acme Pvt. Ltd.', y=10), hdr, r1, tail, r2])
        self.assertEqual([i.description for i in d.items],
                         ['Sugar Elevator Chain\nPlate thickness 5 MM', 'Roller Chain'])

    def test_arithmetic_check_and_rate_recovery(self):
        d = parser.parse([L('X Ltd.'), L(*HEAD),
                          L('1', 'Chain A', '10', 'Mtr.', '100', '900'),      # 10 x 100 != 900
                          L('2', 'Chain B', '10', 'Mtr.', '', '500')])        # rate missing
        self.assertTrue(any('does not equal' in f for f in d.items[0].flags))
        self.assertEqual(d.items[1].rate, 50)
        self.assertTrue(any('worked out' in f for f in d.items[1].flags))

    def test_item_without_quantity_is_kept_and_flagged(self):
        d = parser.parse([L('X Ltd.'), L('S.No', 'Description', 'Qty', 'Unit'),
                          L('1', 'Chain A', '', ''), L('2', 'Chain B', '5', 'Mtr.')])
        self.assertEqual(len(d.items), 2)
        self.assertIn('No quantity found', d.items[0].flags)

    def test_low_confidence_rows_are_flagged(self):
        d = parser.parse([L('X Ltd.'), L(*HEAD, conf=95),
                          L('1', 'Chain A', '10', 'Mtr.', '', '', conf=41)])
        self.assertTrue(any('Poorly scanned' in f for f in d.items[0].flags))

    def test_specification_lines_stay_with_the_item_not_own_rows(self):
        # Regression: a real AQ-2026-001.pdf quotation. The header's own
        # label words ("Sr. No.", "Description", ...) sit narrower than the
        # data columns below them, so continuation lines like "Specification:"
        # and "PIN DIA 20 MM" don't x-overlap any header column at all, and
        # a trailing "per Mtr." on the wrapped description landed in the
        # Rate column by nearest-position guessing. Both used to produce
        # duplicate/phantom items and lose text; numbered "Terms and
        # Conditions" lines used to leak into the last item too.
        hdr = L(('Sr. No.', 88, 137), ('Description', 337, 420), ('Quantity', 644, 706),
                ('Price', 808, 845), y=100)
        row1 = L(('1.', 92, 105), ('BAGASSE CARRIER CHAIN', 153, 353), ('100.0 Mtr.', 625, 693),
                 ('@ Rs. 2,850.00/-', 745, 860), y=115)
        wrap = L(('Bagasse Carrier Chain 152.40 MM Pitch', 153, 423), ('per Mtr.', 745, 798), y=128)
        spec = L(('Specification:', 153, 252), y=141)
        d1 = L(('PLATE THICKNESS 8 MM', 153, 342), y=154)
        d2 = L(('PIN DIA 20 MM', 153, 300), y=167)
        d3 = L(('BUSH DIA 28 MM', 153, 300), y=180)
        d4 = L(('ROLLER DIA 40 MM', 153, 320), y=193)
        d5 = L(('ATTACHMENT 80 X 8 MM', 153, 342), y=206)
        terms = L(('Terms and Conditions', 70, 247), y=219)
        t1 = L(('1. Rates are Ex-works.', 70, 260), y=232)
        d = parser.parse([L('Aypee Industries', y=10), hdr, row1, wrap, spec,
                           d1, d2, d3, d4, d5, terms, t1])
        self.assertEqual(d.mode, 'table')
        self.assertEqual(len(d.items), 1)
        it = d.items[0]
        self.assertEqual((it.quantity, it.unit, it.rate), (100, 'Mtr.', 2850))
        self.assertEqual(it.description, 'BAGASSE CARRIER CHAIN\n'
                          'Bagasse Carrier Chain 152.40 MM Pitch per Mtr.\nSpecification:\n'
                          'PLATE THICKNESS 8 MM\nPIN DIA 20 MM\nBUSH DIA 28 MM\n'
                          'ROLLER DIA 40 MM\nATTACHMENT 80 X 8 MM')
        self.assertNotIn('Terms and Conditions', it.description)
        self.assertNotIn('Rates are Ex-works', it.description)

    def test_word_table_groups_do_not_leak(self):
        a, b = L(*HEAD), L('1', 'Chain A', '10', 'Mtr.', '5', '50')
        a.group = b.group = 1
        other = L('9', 'Unrelated other table', '77', 'Kg.', '1', '77'); other.group = 2
        d = parser.parse([L('X Ltd.'), a, b, other])
        self.assertEqual([i.description for i in d.items], ['Chain A'])


class FreeForm(unittest.TestCase):
    def items(self, *rows):
        d = parser.parse(text_lines('To,', 'M/s Bharat Agro Industries Ltd.', 'Dear Sir,', *rows))
        self.assertEqual(d.mode, 'freeform')
        return d.items

    def test_numbered_list(self):
        its = self.items('1. Sugar Elevator Chain 66.60 mm pitch - 120 Mtrs',
                         '2. Roller Chain 25.4 mm pitch - 50 Mtrs',
                         '3. Bucher Harvesting Chain 101.60 mm pitch, 80 Mtr')
        self.assertEqual([(i.description, i.quantity, i.unit) for i in its],
                         [('Sugar Elevator Chain 66.60 mm pitch', 120, 'Mtr.'),
                          ('Roller Chain 25.4 mm pitch', 50, 'Mtr.'),
                          ('Bucher Harvesting Chain 101.60 mm pitch', 80, 'Mtr.')])
        self.assertTrue(all('free text' in i.flags[0] for i in its))

    def test_dimension_is_not_mistaken_for_quantity(self):
        its = self.items('Roller chain 66.60 mm pitch 25 Mtrs')
        self.assertEqual((its[0].quantity, its[0].unit), (25, 'Mtr.'))

    def test_regression_ocr_mangled_dash_before_quantity(self):
        # OCR read " - 50 Mtrs" as " -.50 Mtrs"; the item used to vanish
        its = self.items('1. Sugar Elevator Chain 66.60 mm pitch - 120 Mtrs',
                         '2. Roller Chain:25.4 mm pitch -.50 Mtrs')
        self.assertEqual([i.quantity for i in its], [120, 50])

    def test_numbered_line_with_unreadable_quantity_is_flagged_not_dropped(self):
        its = self.items('1. Sugar Elevator Chain 66.60 mm pitch - 120 Mtrs',
                         '2. Roller Chain 25.4 mm pitch - S0 Mtrs',            # OCR: 5 -> S
                         '3. Bucher Harvesting Chain 101.60 mm pitch - 80 Mtrs')
        self.assertEqual(len(its), 3)
        self.assertIsNone(its[1].quantity)
        self.assertIn('No quantity found', its[1].flags)

    def test_quantity_on_the_line_below_the_description(self):
        its = self.items('Bagasse Carrier Chain 152.40 mm pitch', '75 Mtrs')
        self.assertEqual((its[0].description, its[0].quantity), ('Bagasse Carrier Chain 152.40 mm pitch', 75))

    def test_inline_rate(self):
        its = self.items('1. Roller Chain 25.4 mm pitch - 50 Mtrs @ Rs. 550/-')
        self.assertEqual((its[0].quantity, its[0].rate), (50, 550))

    def test_letter_boilerplate_is_not_an_item(self):
        d = parser.parse(text_lines('To,', 'M/s Bharat Agro Industries Ltd.', 'Dear Sir,',
                                    'Please quote your best rates for 100 Mtrs of chain within 10 days.',
                                    'Delivery: 10 weeks', 'Phone: 98765 43210'))
        self.assertEqual(d.items, [])
        self.assertTrue(d.warnings)


class Matching(unittest.TestCase):
    CATALOG = [
        dict(id=1, item_name='SUGAR ELEVATOR CHAIN', description='Sugar Elevator Chain 66.60 MM Pitch'),
        dict(id=2, item_name='ROLLER CHAIN', description='Roller Chain 25.40 MM Pitch'),
        dict(id=3, item_name='ROLLER CHAIN', description='Roller Chain 31.75 MM Pitch'),
        dict(id=4, item_name='CRANK CHAIN', description='Crank Chain for Sugar Mills 76.20 MM Pitch'),
    ]

    def setUp(self):
        self.ix = matching.CatalogIndex(self.CATALOG)

    def test_same_name_different_size_resolve_to_the_right_item(self):
        self.assertEqual(self.ix.best('Roller Chain 25.4 mm pitch')[0]['id'], 2)
        self.assertEqual(self.ix.best('roller chain 31.75mm PITCH')[0]['id'], 3)

    def test_unknown_size_or_product_is_not_forced_onto_the_catalog(self):
        self.assertIsNone(self.ix.best('Roller Chain 50 mm pitch')[0])
        self.assertIsNone(self.ix.best('Hydraulic Pump 5 HP')[0])
        self.assertIsNone(self.ix.best('')[0])

    def test_tokens_normalise_numbers(self):
        self.assertEqual(matching.tokens('66.60MM Pitch'), ['66.6', 'mm', 'pitch'])

    def test_customer_by_gstin_then_name(self):
        cs = [dict(company_name='Krishna Sugar Mills Ltd.', gstin='03AABCK1234F1Z5'),
              dict(company_name='Other Co', gstin='')]
        self.assertEqual(matching.match_customer('03aabck1234f1z5', 'zzz', cs)[1], 'GSTIN')
        c, how = matching.match_customer('', 'M/s Krishna Sugar Mills Limited', cs)
        self.assertEqual((c['company_name'], how), ('Krishna Sugar Mills Ltd.', 'name'))
        self.assertEqual(matching.match_customer('', 'Krishna Cement Works', cs), (None, ''))


if __name__ == '__main__':
    unittest.main()
