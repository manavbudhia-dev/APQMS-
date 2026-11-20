"""
The fixed extraction schema, sent to the API as a structured-output
format (so the model can only answer in this shape) and re-checked in
`extractor.validate` (so a bad answer is never trusted blindly).
"""

NUMERIC_FIELDS = ('total_width_mm', 'quantity_mtrs', 'rate_per_mtr', 'original_rate_per_mtr',
                  'gst_percent', 'delivery_weeks')
ATTACHMENT_NUMERIC = ('height_mm', 'thickness_mm')
TEXT_FIELDS = ('product', 'link_material', 'pin', 'price_basis', 'payment_terms', 'notes')

# Must be present (non-null) before a quotation can be made.
REQUIRED_FIELDS = ('rate_per_mtr', 'quantity_mtrs', 'gst_percent', 'delivery_weeks', 'payment_terms')
# A wrong digit here is expensive: the reviewer must tick each one explicitly.
MUST_CONFIRM = ('rate_per_mtr', 'quantity_mtrs', 'delivery_weeks')

FIELD_LABELS = {
    'product': 'Product',
    'link_material': 'Link material',
    'pin': 'Pin',
    'attachment.material': 'Attachment material',
    'attachment.height_mm': 'Attachment height (mm)',
    'attachment.thickness_mm': 'Attachment thickness (mm)',
    'total_width_mm': 'Total width (mm)',
    'quantity_mtrs': 'Quantity (mtrs)',
    'rate_per_mtr': 'Rate per mtr (Rs.)',
    'original_rate_per_mtr': 'Original (struck-out) rate (Rs.)',
    'gst_percent': 'GST %',
    'price_basis': 'Price basis',
    'delivery_weeks': 'Delivery (weeks)',
    'payment_terms': 'Payment terms',
    'notes': 'Notes',
}


def _nullable(t):
    return {'anyOf': [{'type': t}, {'type': 'null'}]}


EXTRACTION_SCHEMA = {
    'type': 'object',
    'properties': {
        'product': _nullable('string'),
        'link_material': _nullable('string'),
        'pin': _nullable('string'),
        'attachment': {
            'type': 'object',
            'properties': {
                'material': _nullable('string'),
                'height_mm': _nullable('number'),
                'thickness_mm': _nullable('number'),
            },
            'required': ['material', 'height_mm', 'thickness_mm'],
            'additionalProperties': False,
        },
        'total_width_mm': _nullable('number'),
        'quantity_mtrs': _nullable('number'),
        'rate_per_mtr': _nullable('number'),
        'original_rate_per_mtr': _nullable('number'),
        'gst_percent': _nullable('number'),
        'price_basis': _nullable('string'),
        'delivery_weeks': _nullable('number'),
        'payment_terms': _nullable('string'),
        'notes': _nullable('string'),
        'needs_review': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'field': {'type': 'string'},
                    'reason': {'type': 'string'},
                },
                'required': ['field', 'reason'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['product', 'link_material', 'pin', 'attachment', 'total_width_mm', 'quantity_mtrs',
                 'rate_per_mtr', 'original_rate_per_mtr', 'gst_percent', 'price_basis',
                 'delivery_weeks', 'payment_terms', 'notes', 'needs_review'],
    'additionalProperties': False,
}

# Ground truth for the sample sheet in the project brief (used by the eval
# script and the tests). The brief's `needs_review: []` is kept empty.
SAMPLE_GROUND_TRUTH = {
    'product': 'Forged Type Chain, Pitch 142mm (Heavy Duty)',
    'link_material': '20MnCr5, Hardened & Tempered',
    'pin': '20MnCr5, 25mm, with Lock Washer both sides',
    'attachment': {'material': 'EN8D', 'height_mm': 98, 'thickness_mm': 10},
    'total_width_mm': 471,
    'quantity_mtrs': 70,
    'rate_per_mtr': 8360,
    'original_rate_per_mtr': 7600,
    'gst_percent': 18,
    'price_basis': 'Ex Factory Jalandhar',
    'delivery_weeks': 8,
    'payment_terms': '25% advance, balance as used in one month',
    'notes': 'Inquiry specified attachment material 20MnCr5; offered EN8D instead.',
    'needs_review': [],
}


def empty_extraction():
    return {
        'product': None, 'link_material': None, 'pin': None,
        'attachment': {'material': None, 'height_mm': None, 'thickness_mm': None},
        'total_width_mm': None, 'quantity_mtrs': None, 'rate_per_mtr': None,
        'original_rate_per_mtr': None, 'gst_percent': None, 'price_basis': None,
        'delivery_weeks': None, 'payment_terms': None, 'notes': None, 'needs_review': [],
    }
