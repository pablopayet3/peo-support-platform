# The integration layer over MCP

`POST /mcp` on the integration service is an MCP server (Model Context Protocol, Streamable HTTP,
stateless, JSON responses). Any MCP client (Claude, ChatGPT, a voice platform, a vendor agent) can
discover and call the same governed tools the support stack uses.

| Tool | What it does | Writes anything? |
|---|---|---|
| `find_person` | Who is this person, verified or not, and their companies and roles | No |
| `get_health_plan` | Plan, carrier, coverage, deductible and out-of-pocket maximum | No |
| `check_life_event_eligibility` | Identity, active plan and 30-day window, with the deadline | No |
| `find_duplicate_payments` | Invoices paid more than once, for a company admin | No |
| `get_procedure_steps` | The current Benefits Ops steps, read live from Confluence | No |

Security: a bearer key with the scope `mcp:read` (`INT_MCP_API_KEY`, or any key in `INT_API_KEYS` that
has the scope). Every call is logged with the caller, the tool and a request number (`mcp-...`) that also
reaches the core platform's audit log. Approving an enrollment is not exposed: a person approves in Zendesk.

MCP does not replace the integrations: the APIs underneath (core platform, Zendesk, Salesforce,
Confluence) are what the tools call. MCP is the standard front door for AI to use them.
