

## Current delivery rules

- The user removed Software Delivery Harness on 2026-11-10. Do not restore it or use its verdict as a release gate. Preserve historical evidence.
- Run meaningful frontend typecheck, tests and build plus native Rust regressions. Use a temporary CC_SWITCH_TEST_HOME; never real login/history/configuration for tests.
- Keep Claude GG resume bound to the original launcher, identity and working directory. Do not fall back to bare Claude.
- Do not change or restart the user network, proxies, dedicated-line services, browser processes or login sessions. Any possible network interruption needs fresh scoped user approval.
