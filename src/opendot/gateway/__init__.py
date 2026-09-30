"""The host MCP gateway: connectors such as FactIQ, reached only from the host.

The model's CLI inside a step container starts bridge.py as an stdio MCP server.
The bridge forwards bytes to a unix socket in the step's run folder, where a host
thread (server.Gateway) answers. The gateway:

- lists and forwards only the connector's allowlisted read tools;
- adds the login (an API key from a host variable, or an OAuth token kept in the
  host database) on the host side, so no login enters the container;
- refuses every other tool, and tells the model to propose a write tool as the
  action mcp.<server>.<tool>, which goes through rules, review and approval;
- logs every call in the gateway_calls table.

Modules: bridge (container side, standard library only), upstream (the host's
connection to one connector), server (the gateway thread), step (the step
extension), actions (write tools as actions), instructions (the public FactIQ
plugin files), oauth (sign-in), cli (opendot connectors ...).

The mcp package is imported inside functions only, so commands that do not use a
connector do not load it.
"""
