# Generic web recording scenario

Use this plugin when no site-specific plugin matches the target.

## Brief-first planning

The user does not need to write browser actions. When they provide a purpose,
audience, and key messages:

1. Normalize the request as Video Brief V1.
2. Get the plugin planning context.
3. Inspect the starting page with the intended capture settings when its
   current controls are not established by a guide.
4. Map each key message to a visible scene.
5. Author and preflight Scenario V1.

Do not invent a menu, route, or locator that is absent from a guide, template,
or inspection result.

## Required information

- Absolute start URL
- Exact allowed origin
- Authentication profile ID when the page requires login
- Ordered user-visible steps
- A completion assertion for every navigation or interaction
- Explicit external effects and retry policy
- Capture target: desktop, mobile web, or tablet

## Capture size

Desktop web uses a fixed `1920x1080` browser viewport and final MP4.

Mobile and tablet capture keep the preset device viewport so responsive
navigation works. The raw WebM uses that device viewport, while the final
mobile/tablet MP4 is scaled proportionally and centered on a `1920x1080`
canvas. Do not override the output size.

Use the same `capture` object for login, page inspection, and recording.
Supported devices are `desktop-chrome`, `iphone-13`, `pixel-7`, and
`ipad-mini`, with portrait or landscape orientation.

Mobile capture emulates responsive web content in Chromium, including user
agent, touch input capability, device scale factor, and mobile viewport. It
does not record a native iOS or Android application.

Desktop and mobile navigation may expose different accessible names. Inspect
the mobile page instead of reusing desktop-only locator assumptions.

## Locator preference

1. `role` with accessible `name`
2. `label`
3. `test_id`
4. `text`
5. `placeholder`
6. `css`

Do not use coordinates, raw JavaScript, or generated CSS class names.

## Safety

Treat `goto`, `click`, `fill`, `press`, and `select_option` as potential
mutations. Mark them `approval: required` and `retry_policy: never` unless the
job contains no such actions. The MCP server enforces the conservative risk
floor even if a scenario declares an action read-only.
