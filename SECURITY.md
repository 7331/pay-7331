# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: on this repository's Security tab, choose "Report a
vulnerability" ([private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
Do not open a public issue or pull request for a vulnerability.

A useful report says what is affected, how to reproduce it, and what an attacker could do with it.

## Scope

- https://pay.7331.org (production)
- https://pay-sandbox.7331.org (the testnet sandbox)
- The code in this repository: the backend, the frontend, the contracts, the Monero scanner and the Python SDK

## Rules for testing

- Test on the sandbox, with testnet coins, wherever you can.
- Use only your own accounts, invoices and funds. Do not pay into, cancel, refund or otherwise touch other people's
  invoices, deposit addresses or funds.
- Do not run denial-of-service or load tests, spam sign-ups or invoices, or social-engineer anyone.
- Stop and report as soon as you can show the issue; do not keep or share data that isn't yours.
