# Admin WebUI

The Admin WebUI login shell is served at `/admin` without credentials and consumes only the authenticated Phase 08/09 endpoints under `/admin/api`.

## Source and build boundary

- `webui/src/**` is the only editable frontend source.
- `npm --prefix webui run build` removes and recreates `coding_tools_mcp/webui_dist/**`.
- The generated `admin.html` is self-contained and is never edited directly.
- `coding_tools_mcp.webui.admin_console_html()` reads the generated artifact.

## Authentication

The self-contained page contains no server data or credentials until sign-in. The user enters a dedicated Admin token, which is kept in page memory and sent in the `Authorization` header only to `/admin/api`. It is not written to a URL, browser persistent storage, logs, Settings, or Gateway configuration. Ordinary MCP bearer and OAuth access tokens do not grant Admin authority.

## Settings and Gateway

Settings writes include the last `persisted_revision`. A stale HTTP 409 keeps the current form draft, fetches the latest persisted state/revision, and shows a conflict instead of overwriting newer configuration.

Gateway changes are persisted for restart only. The WebUI has no upstream start, stop, reload, or Runtime mutation action. The normal form can create and patch one MCP connection, select Broker or Direct exposure, choose whether it is enabled for the next Runtime, and edit/delete persisted entries. Credential-bearing configurations are shown only as redacted summaries; per-server edits preserve hidden credential fields. The advanced whole-document JSON editor remains limited to credential-free replacement documents.

## Permission and compatibility wording

The WebUI has no tool-profile state or controls. Permission modes do not change the fixed tool catalog. Safe mode limits runtime capabilities but does not hide mutation tools.

The fake-readonly annotation override appears only in an advanced danger section. The warning states that it does not hide tools, block mutation, change handlers, or create a security boundary.

## Secret and OAuth rendering

OAuth, Gateway, Settings, and Vault responses pass through a defensive redaction layer before rendering. Client secrets, token material, digests, hashes, signing secrets, credential references, and Vault values are not displayed. Secret Vault values are accepted only through password inputs and are immediately cleared after requests.

Saving the reserved Secret name `oauth/authorization-password` updates the
encrypted OAuth Vault and the running authorization-page password immediately.
The WebUI marks that entry as OAuth-active, does not offer deletion, and never
echoes its value. Existing access and refresh tokens are unaffected.

Each OAuth Client card reports whether Authorize login uses the global password
or a dedicated Client override. Operators can set/rotate the dedicated password
in a password dialog or reset the Client to the global fallback. Both changes
take effect immediately. Password inputs provide an accessible Show/Hide toggle;
the toggle affects only the current input and never reveals a stored Vault value.

OAuth Client cards show a checkbox allowlist of active Workspaces. Operators can
allow one or many Workspaces and replace that list from the card; the next
Authorize request sees the change immediately without restarting the server.
When several are allowed, the Authorize page asks which Workspace this Grant
should use. Updating the allowlist affects subsequent authorizations and does
not rewrite the Workspace identity stored on existing Grants or Tokens.

## Conversation rendering

Conversation lists call the summary endpoint. Full messages and context are loaded only after an explicit selection through the paginated detail endpoint. Transcript, message, context, Workspace labels, OAuth metadata, and server errors are treated as untrusted text and rendered with DOM node creation and `textContent`; the source contains no `innerHTML` path.

## Accessibility

- Keyboard-accessible navigation and buttons.
- Explicit labels for every form control.
- Alert regions for errors and stale-revision conflicts.
- Focus restoration after destructive confirmation dialogs.
- 44-pixel minimum controls and responsive layouts for narrow screens.
- Destructive confirmations include the exact Workspace/object ID and expected impact before execution; actual affected counts are displayed afterward.

## Telemetry status

The status page displays the effective `on`, `off`, or `debug` mode returned by the Admin backend. The backend supplies only the mode and the `docs/telemetry.md` documentation entry; it does not expose telemetry events or add paths, identifiers, commands, arguments, or file content to the response.

The WebUI continues to show the documented controls `CODING_TOOLS_MCP_TELEMETRY=off`, `DO_NOT_TRACK=1`, and automatic CI suppression. Phase 11 preserves the upstream v0.2.2 default and does not add a browser-side telemetry switch.
