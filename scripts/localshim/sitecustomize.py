"""
sitecustomize.py — TLS trust shim for local development.

Python auto-loads any sitecustomize on PYTHONPATH at interpreter startup, which
is the only hook that reaches the Functions host's worker process: the host
spawns it, so exporting variables in your own shell does not help.

A TLS-inspecting agent (corporate proxy, some AV products) presents a root
certificate that the Windows store trusts and certifi does not. The .NET host is
fine; the Python worker fails every outbound HTTPS call with
CERTIFICATE_VERIFY_FAILED. truststore routes verification through the Windows
store instead.

Disabling verification is NOT the fix: it changes the behaviour under test, hides
a real certificate problem, and has a way of surviving into a deployed config.

No-op when truststore is not installed, which is the case in Azure.
"""

import os

os.environ.pop("REQUESTS_CA_BUNDLE", None)

try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass
