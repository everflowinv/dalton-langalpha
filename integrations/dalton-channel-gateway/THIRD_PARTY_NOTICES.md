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

## Referenced and Adapted Tencent Weixin iLink Files (`@tencent-weixin/openclaw-weixin`, v2.1.5)

| Target File in `@dalton/channel-gateway` | Reference Source (`.agy/ref_openclaw_weixin/src/`) |
|---|---|
| `src/channels/weixin/types.ts` | `api/types.ts` |
| `src/channels/weixin/api.ts` | `api/api.ts`, `api/session-guard.ts` |
| `src/channels/weixin/cdn.ts` | `cdn/aes-ecb.ts`, `cdn/pic-decrypt.ts`, `cdn/cdn-url.ts`, `cdn/cdn-upload.ts`, `cdn/upload.ts`, `media/mime.ts` |
| `src/channels/weixin/markdown-filter.ts` | `messaging/markdown-filter.ts` |
| `src/channels/weixin/inbound.ts` | `messaging/inbound.ts`, `messaging/partial-quote.ts`, `messaging/process-message.ts` |
| `src/channels/weixin/send.ts` | `messaging/send.ts`, `messaging/send-media.ts` |
| `src/channels/weixin/login-qr.ts` | `auth/login-qr.ts` |
| `src/channels/weixin/monitor.ts` | `monitor/monitor.ts` |

---

## OpenClaw & Tencent Weixin Plugin Licenses

```text
MIT License

Copyright (c) 2025 Peter Steinberger
Copyright (c) 2026 Tencent

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
