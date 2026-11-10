# Third-Party Notices

This package (`@dalton/channel-gateway`) includes code adapted from the OpenClaw project (`.agy/ref_openclaw_channels/`).

## Referenced and Adapted OpenClaw Files

| Target File in `@dalton/channel-gateway` | OpenClaw Reference Source (`.agy/ref_openclaw_channels/`) | Referenced Lines |
|---|---|---|
| `src/shared/async.ts` | `feishu/dist/.setup/probe-DXnFxq5j.mjs`, `feishu/dist/.setup/monitor.account-UbN1RhlP.mjs` | `1–12`, `1–14` |
| `src/shared/dedupe.ts` | `feishu/dist/.setup/monitor.account-UbN1RhlP.mjs` | `5215–5309` |
| `src/shared/keyed-queue.ts` | `feishu/dist/.setup/monitor.account-UbN1RhlP.mjs` | `858–872` |
| `src/shared/safe-fetch.ts` | `feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs` | `1709–1899` |
| `src/shared/text.ts` | `feishu/dist/.setup/monitor.account-UbN1RhlP.mjs`, `feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs` | `15–261`, `11–31` |
| `src/channels/feishu/client.ts` | `feishu/dist/.setup/client-wwr8PGPH.mjs` | `161–170`, `222–331` |
| `src/channels/feishu/probe.ts` | `feishu/dist/.setup/probe-DXnFxq5j.mjs` | `14–169` |
| `src/channels/feishu/targets.ts` | `feishu/dist/.setup/conversation-id-D00i9uy5.mjs`, `feishu/dist/.setup/monitor.account-UbN1RhlP.mjs` | `6–119`, `633–702` |
| `src/channels/feishu/format.ts` | `feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs`, `feishu/dist/.setup/monitor.account-UbN1RhlP.mjs` | `36–630`, `295–345` |
| `src/channels/feishu/inbound-parse.ts` | `feishu/dist/.setup/monitor.account-UbN1RhlP.mjs`, `feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs` | `276–433`, `822–857`, `2373–2404`, `5215–5309`, `5343–5565`, `352–436`, `461–630` |
| `src/channels/feishu/send.ts` | `feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs` | `750–838`, `1110–1113`, `2439–3015` |
| `src/channels/feishu/media.ts` | `feishu/dist/.setup/reply-delivery-result-iCCrdxE1.mjs` | `1709–2437` |
| `src/channels/feishu/directory.ts` | `feishu/dist/.setup/channel.runtime-CC15v8AP.mjs`, `feishu/dist/.setup/chat-fC3dvxFs.mjs` | `91–183`, `46–101` |
| `src/channels/feishu/monitor.ts` | `feishu/dist/.setup/monitor.account-UbN1RhlP.mjs` | `858–872`, `5215–5309`, `5343–5565`, `5622–5760`, `6228–6317` |
| `src/native-commands/manifest.ts`, `src/native-commands/discord-registrar.ts` | `openclaw/dist/commands-registry-list-BbUjmer4.mjs`, `@openclaw/discord/dist/.setup/provider-CbmXLYrW.mjs` | `1–30`, `3948–4026`, `6096–6121` |

## Referenced and Adapted Lark CLI Files

| Target File in `@dalton/channel-gateway` | `larksuite/cli` Source (`7dbefd9cf17357545450ca53007d413212bc8aba`) | Referenced Lines |
|---|---|---|
| `src/native-commands/manifest.ts`, `src/native-commands/feishu-registrar.ts` | `shortcuts/application/slash_command_create.go`, `shortcuts/application/slash_command_update.go`, `shortcuts/application/slash_command_common.go`, `shortcuts/application/slash_command_resolve.go` | `1–113`, `1–95`, `1–105`, `1–54` |

---

## OpenClaw License

```text
MIT License

Copyright (c) 2025 Peter Steinberger
Copyright (c) 2026 OpenClaw Foundation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Lark CLI License (`larksuite/cli`)

```text
MIT License

Copyright (c) 2026 Lark Technologies Pte. Ltd.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
