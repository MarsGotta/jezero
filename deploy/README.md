# deploy/

Files to run Jezero unattended:

- `systemd/jezero-ingest.service` and `systemd/jezero-ingest.timer`: run the due
  connectors every few minutes and check their health.
- `systemd/jezero-serve.service`: the MCP server.

How to install them, publish the server over HTTPS, back it up and upgrade it:
[Run Jezero as a service](../docs/how-to/deploy.md).
