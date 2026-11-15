"""Portable operation receipts in existing comment/description metadata.

Only our exact versioned token is recognized. A color-space flag is not proof.
Original user text stays intact; this is an operation marker, not authenticity proof.
"""
COLOR = '[MediaOrganizer:color:action6-dlogm-rec709:v1]'
WATERMARK = '[MediaOrganizer:photo-watermark:v1]'


def marked(value, token):
    return token in str(value or '')


def append(value, token):
    value=str(value or '')
    return value if marked(value,token) else value+(' | ' if value else '')+token
