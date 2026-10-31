# Security Policy

## Reporting a vulnerability

**Never report a security issue in a public GitHub issue, discussion or pull request.**

Instead, report it privately through GitHub's [**Report a vulnerability**](https://github.com/Jomaries777/ph-statement-import/security/advisories/new) tab (`Security` → `Report a vulnerability`).

Include:
- A description of the issue and its potential impact.
- Steps to reproduce (using made-up data only — never attach a real statement PDF, token or personal financial data).

## Scope

In scope for security reports:
- Anything that exposes YNAB tokens, OAuth credentials, Flask session secrets, PDF passwords, statement PDFs, or parsed transaction data.
- Cross-user data exposure or authentication/session bypasses in the web app.
- Command injection, path traversal, XSS (e.g. unsanitized PDF text rendered in the web UI), or CSRF in state-changing routes.
