"""Client-side mesh access over the HTTP wire (the mesh entrypoint).

The instance is HTTP-only; its routes ARE the operations (brief-20260603-dirz
fork E). This package is the client side: mesh fetch resolves an address against an
instance, strict-verifies the returned envelope LOCALLY (verify, do not trust the
station's own asserted verdict), and reports a fork-F verdict + exit code.
"""
