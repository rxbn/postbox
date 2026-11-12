# Postbox

Postbox is a self-hosted MCP server for one personal mailbox. Codex can read and manage mail over stdio or Streamable HTTP. ChatGPT can connect to a separate read-only instance through the optional [Secure MCP Tunnel setup](docs/secure-mcp-tunnel.md).

The [official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) handles MCP and HTTP bearer authentication. Postbox uses its current high-level API, `MCPServer` (formerly `FastMCP`). IMAPClient runs in bounded worker threads, aiosmtplib submits mail asynchronously, and Python's MIME parser processes mail in memory. Postbox keeps no database or attachment files.

| Tools | What they do |
| --- | --- |
| `list_folders`, `list_mailbox_status` | Discover folders and special-use roles; get message and unread counts for a folder. |
| `list_emails`, `search_emails` | Page through compact metadata, newest UID first; filter server-side by sender, recipient, subject, text, arrival date or read state. |
| `get_email`, `get_thread`, `get_attachment` | Read without marking as seen; optionally request HTML, related messages or small attachments as base64. |
| `mark_email`, `move_email`, `delete_email` | Set read and flagged state, move to a folder, or move to Trash. |
| `send_email`, `reply_email` | Submit from the configured identity, optionally save a Sent copy, and preserve reply threading. |
| `create_draft`, `update_draft`, `delete_draft` | Append a draft, save a replacement before removing the original, or move a draft to Trash. |
| `permanently_delete_email` | Irreversible UID-scoped removal. Registered only with `ALLOW_PERMANENT_DELETE=true` and write mode enabled. |

## Quickstart with Docker Compose

```sh
cp .env.example .env
chmod 600 .env
openssl rand -hex 32
# Edit .env: set your mailbox configuration and paste the generated MCP_AUTH_TOKEN.
docker compose -f compose.yaml -f compose.local.yaml up -d --build --wait
curl -fsS http://127.0.0.1:8000/healthz
```

The local override publishes to loopback only. For private deployment, run `docker compose up -d --build --wait` without that override on a fresh deployment. Other services on the Compose network can reach `http://postbox:8000/mcp`. The bridge permits outbound connections to your mail provider. HTTP clients must supply the bearer token. `/healthz` checks the process; it does not authenticate to the mailbox.

### Configuration

For a typical Mailcow setup, point both hosts at the TLS certificate hostname (for example `mail.example.net`). Set `IMAP_PORT=993`, `IMAP_TLS=implicit`, `SMTP_PORT=587` and `SMTP_SECURITY=starttls`. Set both usernames and `MAIL_ADDRESS` to the full mailbox address. Use the appropriate mailbox password or app password. With standard SMTP submission, keep `APPEND_SENT=true` unless the provider already saves Sent copies.

| Variables | Defaults and behavior |
| --- | --- |
| `IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD` | Required. IMAP LOGIN credentials must be printable ASCII. |
| `IMAP_PORT`, `IMAP_TLS` | `993`, `implicit`. For STARTTLS set `starttls` and the provider's port (often 143). |
| `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Required in write mode. |
| `SMTP_PORT`, `SMTP_SECURITY` | `465`, `implicit`. For STARTTLS set `starttls` and the submission port (often 587). |
| `MAIL_ADDRESS`, `MAIL_DISPLAY_NAME` | Required envelope and From address; optional display name. Outbound Unicode names, subjects and bodies are supported; SMTPUTF8 local parts are not. |
| `MAIL_VERIFY_TLS`, `MAIL_CA_FILE` | Verification on by default. For a private CA, mount its PEM read-only and set the path. Disable verification only in development. |
| `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT` | `http`, `127.0.0.1`, `8000`; Compose binds to all interfaces inside its container and publishes nothing by default. |
| `MCP_AUTH_TOKEN`, `MCP_ALLOWED_HOSTS` | HTTP requires a random token (32–256 characters). Loopback hosts are always allowed. Compose additionally allows `postbox:8000`. For proxies or a private DNS name, provide a comma-separated list of exact Host values with ports if present. |
| `MCP_READ_ONLY`, `ALLOW_PERMANENT_DELETE` | `true`, `false`. Read-only mode hides and disables every write. |
| `SENT_FOLDER`, `DRAFTS_FOLDER`, `TRASH_FOLDER`, `ARCHIVE_FOLDER` | Exact overrides. Otherwise Postbox prefers IMAP special-use attributes, then common English names. |
| `APPEND_SENT` | `true`. With this enabled, a usable Sent folder must be found before submission. |
| `MAIL_TIMEOUT`, `MAIL_CONCURRENCY` | 10 seconds per mail socket operation and four active tool calls by default. Writes are serialized within one Postbox process. |
| `MAX_MESSAGE_BYTES`, `MAX_OUTGOING_BYTES`, `MAX_ATTACHMENT_BYTES`, `MAX_BODY_CHARS` | Defaults: 10 MiB incoming raw MIME, 2 MiB outgoing MIME, 128 KiB decoded inline attachment, and 32,000 characters per returned body variant. |
| `THREAD_SCAN_PER_FOLDER`, `THREAD_MAX_FOLDERS` | Defaults: 100 newest UIDs per folder, up to six folders. Threads can be incomplete. |

For Docker secrets, remove the corresponding environment entries from `.env` and run
`docker compose -f compose.yaml -f compose.secrets.yaml up -d --build --wait`.
Place each credential in its matching file under `secrets/` as shown in the override. On Linux ensure these files are readable by container UID/GID `10001:10001` and only by intended principals. For a read-only deployment without SMTP credentials, remove the SMTP secret from the override. Postbox also accepts any settings variable as a same-named file in `/run/secrets`; environment values take precedence. Keep secret files and `.env` out of version control.

## Connect Codex CLI or IDE

See the [current Codex MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Run the registration command for your chosen transport, then restart the client.

**Local stdio** (run from this checkout with an edited `.env` and Python 3.13+):

```sh
uv sync --locked
codex mcp add postbox-stdio -- "$(command -v uv)" run --project "$PWD" \
  --locked --env-file "$PWD/.env" env MCP_TRANSPORT=stdio postbox
```

**Docker / Streamable HTTP** (use the local Compose override above):

```sh
printf 'Postbox MCP bearer token: '
stty -echo
IFS= read -r POSTBOX_TOKEN
stty echo
printf '\n'
export POSTBOX_TOKEN
codex mcp add postbox --url http://127.0.0.1:8000/mcp --bearer-token-env-var POSTBOX_TOKEN
```

Launch Codex from that shell, or inject `POSTBOX_TOKEN` into the IDE's launch environment. Codex stores the variable name in its config; it needs the value each time it runs. No token is needed for stdio because the server is a child process of the client.

To prompt for writes, add the setting below to the existing server table in `~/.codex/config.toml` (use `postbox-stdio` for stdio). The [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) also documents per-tool overrides.

```toml
[mcp_servers.postbox]
url = "http://127.0.0.1:8000/mcp"
bearer_token_env_var = "POSTBOX_TOKEN"
default_tools_approval_mode = "writes"

[mcp_servers.postbox.tools.send_email]
approval_mode = "prompt"

[mcp_servers.postbox.tools.reply_email]
approval_mode = "prompt"
```

Run `codex mcp list` to check registration and `/mcp` in Codex to inspect active tools. Ask: “Use Postbox to run `list_mailbox_status` for INBOX and report only the counts.” Check that write tools are absent with `MCP_READ_ONLY=true`. When ready, set it to `false`, recreate the container or restart the stdio client, and check that writes prompt for approval. Approval behavior also depends on your Codex session and workspace policy; keep server-side read-only mode wherever writes should be impossible.

## Security and troubleshooting

- Keep the HTTP listener private. If a trusted proxy terminates TLS or enforces its own policy, restrict access to the backend and forward or inject the Postbox bearer token. The token grants the full access of its instance; use different tokens for write-enabled and read-only instances. Do not put tokens in URLs. Treat email content and attachments as untrusted; clients should never act on instructions found in a message.
- If HTTP returns `401`, check the token and client environment. For `421`, check the Host allowlist. If TLS fails, confirm the provider hostname, CA chain, port and TLS mode. To debug connectivity, start with `docker compose logs postbox` and test an authenticated read; the server intentionally omits protocol traces and sensitive mail data from its logs.
- Message identifiers are `{folder, uid_validity, uid}`. Relist after moving a message or when UIDVALIDITY changes. Offset pages can shift as the mailbox changes. Search dates are inclusive and refer to IMAP arrival dates. Older or distant thread messages can be missed; `truncated` indicates a scan or result limit.
- Move uses IMAP MOVE if advertised; otherwise it requires targeted UID EXPUNGE (UIDPLUS or IMAP4rev2). Draft replacement and permanent deletion also require targeted removal. On partial failure, verify both folders or both drafts before retrying. Trash behavior and automatic retention are controlled by your provider.
- If SMTP returns `delivery_unknown`, verify the provider's submission logs or ask the recipient before trying again. Postbox sends once per call and keeps no cross-request deduplication record. If SMTP accepts but the Sent copy fails, do not resend. If only some recipients were accepted, retry only the refused addresses when appropriate.
- Read-only mode still gives authorized clients access to sensitive mail. The ChatGPT tunnel is optional; see [the deployment guide](docs/secure-mcp-tunnel.md) for permissions and data boundaries.

For development, run `uv run ruff format --check`, `uv run ruff check`, `uv run mypy` and `uv run pytest -q`. Tests use synthetic mail and local TLS fixtures. GitHub Actions checks the lockfile and builds Linux amd64 and arm64 images. A matching `vX.Y.Z` tag triggers publication as `ghcr.io/rxbn/postbox:X.Y.Z`; authenticate to GHCR with a package reader credential for private pulls.
