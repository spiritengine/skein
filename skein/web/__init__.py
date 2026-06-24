"""Read-only web surface for the content-hash station (port 9001).

Two surfaces over one store, chosen by content negotiation, both built from the
same native wire envelope (:mod:`skein.envelope`): the machine wire (JSON /
agent markdown / raw ``.md``) and the themeable human HTML. The HTML is rendered
FROM the envelope (slice 3) — the legacy ``ContentHashAdapter`` seam is retired,
so the two surfaces can no longer diverge on derived fields. A station's identity
and look come from its stationfile (:mod:`skein.stationfile`); themes layer
CSS over a fixed set of stable hooks and never touch the markup or the spine.
"""
