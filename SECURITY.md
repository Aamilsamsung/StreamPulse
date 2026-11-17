# Security policy

StreamPulse is an experimental **local, single-administrator** prototype.

**Do not put the current FastAPI server on the public internet** or accept uploads from untrusted users. The current implementation lacks production account management, per-user isolation, quotas, malware/content scanning, robust network egress/SSRF controls, a hardened media worker sandbox and rate limits.

Do not commit ADMIN_KEY, account tokens, private livestream URLs, or any runtime `data/` files. Rotate any credential accidentally exposed.

For responsible disclosure, avoid posting working exploit details or secrets in public issues. Contact the repository maintainer using an appropriate private channel.

There is no guarantee of immediate security response or production support during this prototype phase.
