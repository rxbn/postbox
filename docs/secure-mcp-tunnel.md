# Connect a read-only Postbox to ChatGPT

Follow the [official Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) and use the official [OpenAI tunnel-client](https://github.com/openai/tunnel-client/releases/latest). Postbox itself speaks standard Streamable HTTP. This walkthrough assumes ChatGPT on the web and a workspace allowed to add custom MCP servers.

1. In [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels), select the intended Platform organization and create a tunnel associated with the target ChatGPT workspace. Creating a tunnel requires **Tunnels Read + Manage**; the plugin operator and restricted runtime key need **Tunnels Read + Use**. Copy the tunnel ID and create a dedicated runtime API key for the tunnel client. Keep the admin key out of the runtime. Check your organization’s availability and billing terms in Platform; don't assume the service is free or included in a ChatGPT plan.
2. Start Postbox with `MCP_READ_ONLY=true` and a dedicated `MCP_AUTH_TOKEN`. If the tunnel client runs on the Docker host, use the loopback Compose override and point it at `http://127.0.0.1:8000/mcp`. For a containerized client on the same Compose network, use `http://postbox:8000/mcp` without publishing Postbox. If the client runs on a separate host, use a private HTTPS endpoint or another protected hop, configure the exact Host allowlist, and allow only intended callers to reach it.
3. Install the client using the official release and its verification instructions. On its host, inject `CONTROL_PLANE_API_KEY` (the OpenAI runtime key) and `POSTBOX_TUNNEL_AUTHORIZATION` (the full `Bearer <Postbox MCP token>` header value) from your secret store. Configure the ID and upstream URL and run:

   ```sh
   export CONTROL_PLANE_TUNNEL_ID=tunnel_REPLACE_WITH_YOUR_ID
   export MCP_SERVER_URL=http://127.0.0.1:8000/mcp
   export MCP_EXTRA_HEADERS='Authorization: env:POSTBOX_TUNNEL_AUTHORIZATION'
   tunnel-client doctor --explain
   tunnel-client run
   ```

   The client adds the header only on the private backend hop. Its [configuration reference](https://github.com/openai/tunnel-client/blob/master/docs/configuration.md) also supports a `file:/path/to/secret` header value. The secret file contains the complete header value. For a long-running deployment, supervise the official binary with your service manager and supply these same settings through its credential mechanism. Restrict outbound access to the documented OpenAI endpoints and keep the client's admin listener on loopback.

4. Check the client's reported local admin URL: `/healthz` and `/readyz` should return success; `/ui` shows the upstream MCP status. In [ChatGPT Plugins](https://chatgpt.com/plugins), choose **+ → Add custom MCP server → Tunnel** and select or enter the ID. For the configuration above, choose no ChatGPT-side OAuth or bearer forwarding; the tunnel runtime already supplies Postbox's bearer credential on the backend hop. Create the plugin, check that discovery lists only read tools, and ask ChatGPT to call `list_mailbox_status` for INBOX. If discovery fails, confirm the workspace association and permissions, then check `tunnel-client doctor --explain`, the backend URL, and the token. Refresh the plugin's tools if you changed the server.

Tunnel access is governed by OpenAI organization and workspace permissions. Postbox independently checks the backend bearer token; a tunnel alone is not application authorization. A static backend token represents one operator and does not carry a distinct user identity. Give use permission only to people who may read this mailbox, and do not publish a personal mailbox plugin across a workspace. MCP arguments and results, including mail contents returned by tools, transit OpenAI and may enter ChatGPT conversations. Avoid protocol or raw HTTP logging on both hops.

ChatGPT custom tool availability depends on plan, rollout, and administrator settings. The [ChatGPT developer mode availability notes](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt) describe read/fetch access for Pro in developer mode and write support for eligible Business, Enterprise and Edu workspaces. Check the account's current policy and [custom MCP server guidance](https://developers.openai.com/api/docs/guides/custom-mcp-server) before deploying. Keep ChatGPT read-only by default even if the account can write. For Codex writes, use a separate Postbox instance, a distinct token, and client approvals.
