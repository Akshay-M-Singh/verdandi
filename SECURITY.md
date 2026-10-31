# Security policy

## Supported versions

The latest 1.x release receives security fixes. Older releases are not
supported.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private vulnerability reporting:
**Security → Report a vulnerability** on
<https://github.com/Akshay-M-Singh/verdandi/security/advisories/new>.

Include:

- the version and platform,
- a description of the issue and its impact,
- reproduction steps or a minimal proof of concept,
- any suggested fix.

You will get an acknowledgement as soon as possible; fixes and a disclosure
timeline will be agreed with you. Please give us a reasonable window before
publishing details.

## Scope

Verdandi runs locally:

- The desktop engine service binds `127.0.0.1` only.
- The gateway key is stored in the OS credential store; it is never written to
  logs or diagnostics.
- Documents are sent only to the model provider the user configures.
- The app has no telemetry, access check or feedback upload.

In-scope examples: local API authorization/CSRF issues, path traversal in run
or export handling, key leakage in diagnostics/logs, unsafe PDF or image
parsing, Tauri capability/CSP issues, and supply-chain problems in the
dependency set.

Out of scope: vulnerabilities in the model provider or gateway, issues that
require an already-compromised machine, and the accuracy of extracted values
(that is a correctness bug, not a security bug).

## Dependencies

Dependency and GitHub Actions updates are tracked with Dependabot. Please
report a vulnerable dependency privately if it is not already covered by an
advisory.
