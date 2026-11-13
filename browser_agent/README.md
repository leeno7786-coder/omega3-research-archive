# Omega Browser Controller

The browser controller attaches Omega to its portable debugger-enabled
Microsoft Edge session through the Chrome DevTools Protocol
(`browser_agent/cdp_client.py`) and works in a tab it opens in the background
of the operator's window; the tab is never activated, so the HUD stays in
front. The session profile lives on the Omega SSD tree; the controller does not
store website credentials outside that Edge profile. The browser bootstrap
uses the shipped EdgeDriver archive to start the debugger-enabled session that
the controller then drives through CDP.

On the first browser action, the native chat tool selects the shipped
platform-specific archive from `runtimes/browser/edgedriver/`, verifies its
SHA-256 manifest entry and Edge-version compatibility, extracts it into
Omega's SSD tree, and starts EdgeDriver. It starts the user's Edge executable
with a local DevTools debugger address when one is not already available. The
defaults are `127.0.0.1:9222` for the debugger and
`http://127.0.0.1:9515` for EdgeDriver; both are tool arguments and can be
changed by the operator.

There is no network download fallback. If the installed Edge release does not
match the shipped driver, or the host has no shipped archive, setup fails with
an attributable error so the release can be updated deliberately.

The debugger-enabled Edge launch always uses the portable profile at
`guardian/state/browser/edge-debug-profile` inside the Omega tree. This is
intentional: Chromium-based Edge forwards a second launch into an existing
personal profile and drops the remote-debugging flag. The dedicated profile
lets Omega start alongside a normal Edge session without terminating it, and
the profile moves with the SSD so an operator signs in once per portable copy.
HUD launch and browser tasks use this same profile and local debugger port;
browser tasks themselves never close the Edge process that contains the HUD.

The controller is execution-only. `browser_agent/agent_loop.py` is the agent
that drives it: a bounded observe -> decide -> act -> screenshot loop in Omega's
own Edge window (`open_task_window`), with the tools in the model's prompt and
the model choosing which to call; `browser_agent/page_state.py` reads what the
window shows (address, title, viewport, readable text, visible controls with
their centres) through page JavaScript. Every action's screenshot streams to
the HUD chat panel as a `chat.browser_frame`. The operator's own tabs are never
listed, switched to, captured or closed. Page text and images are untrusted
external content; typed text is never kept in durable state.
