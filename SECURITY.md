# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting
on this repository ("Security" tab, "Report a vulnerability"). Do not open public issues
for security reports.

You can expect an acknowledgment within 72 hours. Reports stay private until a fix is
released.

## Scope

Biblioteca stores summaries of personal and work material and serves them to AI agents.
Issues of particular interest:

- Authentication or per-client source filtering bypasses in the MCP server.
- Secrets surviving redaction into the database or the summarizer's input.
- Content from external sources being able to influence agents as instructions.
- FTS5 or SQL injection through queries or configuration.
