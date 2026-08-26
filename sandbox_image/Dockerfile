# Layered on the base image every ordinary ticket already uses — see
# ../features/sandbox_runner.py's DEFAULT_IMAGE. Only a staging-verify run
# ever pulls the image built from this file (STAGING_IMAGE there); a build
# failure here can never break a support ticket that never touches a browser.
FROM opensandbox/code-interpreter:v1.0.2

# Pinned to the exact version @playwright/mcp@0.0.79 depends on — checked at
# the time this file was written via `npm view @playwright/mcp@0.0.79
# dependencies`. Installing a mismatched playwright-core would leave the MCP
# server looking for a Chromium revision that was never downloaded here; it
# would then try to fetch one itself at runtime, which the sandbox's egress
# allowlist (sandbox_runner.STAGING_EGRESS_ALLOW — FQDNs only, no browser CDN)
# does not permit. Bumping @playwright/mcp later means re-checking this pair
# and re-building, not just changing one number.
ENV PLAYWRIGHT_MCP_VERSION=0.0.79 \
    PLAYWRIGHT_VERSION=1.63.0-alpha-2026-08-05 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# playwright-core is installed globally a second time, alongside
# @playwright/mcp's own nested copy, purely to get its `install --with-deps`
# CLI on PATH at build time — Playwright resolves a browser by the version
# string, not by which install triggered the download, so @playwright/mcp's
# bundled copy finds what this step downloads as long as the versions match
# exactly (enforced by ENV above, not by a floating range).
RUN npm install -g "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" "playwright-core@${PLAYWRIGHT_VERSION}" \
    && npx --yes "playwright-core@${PLAYWRIGHT_VERSION}" install --with-deps chromium \
    # World-readable: this RUN step executes as the image's build-time user,
    # but the sandbox later runs as a dynamically created user whose uid
    # matches whichever host account started the run (sandbox_runner.AGENT_UID)
    # — never the uid that ran this RUN step.
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

# Xvfb: a headless-looking `--headless` Chromium is exactly the pattern
# Cloudflare's bot-management is built to catch — confirmed live, repeatedly,
# on this same store — while a real headed browser with the same session
# sails through with zero challenge. There is no real display in this
# container, so Xvfb fakes one Chromium can render into, letting
# sandbox_runner.py drop `--headless` and launch actual headed Chromium
# whenever a captured Chrome profile is mounted (see its own
# SHOPIFY_CHROME_PROFILE_DIRNAME comment). `--with-deps` above already
# installed Chromium's own shared-library dependencies; this is the one
# extra package neither that nor the base image provides.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*
