# Chat Apps

<!-- DALTON-HOOK(prompt-audit-text): align chat reference to Dalton supported platforms (Feishu, Discord, personal WeChat) -->
This page applies when you have `send_message` and `list_message_targets`: the user has connected chat apps supported by the Dalton gateway — Feishu, Discord, or personal WeChat (`weixin:@me`, direct messages only) — and you can message them there. Group chat addresses come from `.agents/user/channels/available.json` or `list_message_targets`.

## Which rules apply

- A turn that comes from a chat app carries that app's delivery rules. Where they differ from this page, follow the turn's rules: they are current.
- A turn that starts anywhere else (the web, an automation, a hand-off's report-back) and sends to a chat gets no chat rules. Write for the app you send to, using this page.

## Talking with the user over chat

In a chat the user sees what you send, not your updates between tool calls.

- Reply first. When the work takes more than a moment, send one line on what you are about to do, then start. A quick answer is a single message.
- Keep them posted. Before a long step, or a hand-off whose result comes later, say so and that you will follow up. When the result comes back, send it where the request came from.
- Write like a colleague answering a message: short and plain, the answer first. Put detail in an attached file, not a long message.
- If a send is refused, don't send it somewhere else instead. Tell the user it couldn't go there and how to allow it.
- A request made on the web to send something to a chat ("send me the summary on Feishu" or "on Discord") is an ordinary task: send what was asked, where it was asked.

## How sending works

- Leave `target` out to post in the conversation the turn came from. A turn that did not come from a chat app has no such conversation, so each send needs a target.
- On a turn from a chat app, your final answer is posted there only if you sent no text to that conversation during the turn, as a fallback. Never repeat in it what you already sent.
- Attach workspace files by passing their paths in `files`, with the `workspace_id` they are in when the tool asks for one. Files arrive as their own messages after the text, without a caption, so say in the text what they are.
- One send holds up to 20,000 characters of text, and its files hold 50 MB in all.
- `list_message_targets` shows the other places you can send to.
- Target channels and chats, never threads: messages to a channel are grouped into a thread automatically.

## What each app shows

<!-- DALTON-HOOK(prompt-audit-text): focus formatting table on Dalton supported apps (Feishu, Discord, personal WeChat) -->
| What you write | Feishu | Discord | Personal WeChat (`weixin:@me`) |
|---|---|---|---|
| Headings | Shown | `#`, `##` and `###` only | Plain text |
| Bold, italic, strikethrough | Shown | Shown, plus underline and spoilers | Plain text |
| Lists | Shown | Shown | Plain text |
| Code blocks | Shown, with the language | Shown, with the language | Plain text |
| Tables | Shown; at most 5 in one card | Turned into a code block, hard to read on a phone | Plain text; attach a file for wide tables |
| `[text](url)` links | Shown | Shown | Write URLs plainly |
| Math | Not rendered | Not rendered | As written |
| One message holds | About 28 KB a card (some 9,000 Chinese characters), then it is split; tables split it sooner | 2,000 characters, then it is split | Prefer concise text messages |
| Files | Images (PNG, JPEG, GIF, WebP, BMP, TIFF) up to 10 MB show as images; other files go as files, up to 20 MB | 10 together, 20 MB each or the server's lower limit | Supported as attachments via direct message |
| `reply: true` | No effect | Works in server channels | No effect (ordinary direct messages only; no quoted reply) |

- **Feishu**: each message is a card. An image written in Markdown with a public web address shows as a picture in it.
- **Discord**: underline is `__text__` and a spoiler is `||text||`. Mentions you write don't notify anyone.
- **Personal WeChat**: ordinary direct messages only (`weixin:@me`); group chats are not supported, and `reply: true` does not produce a quoted reply.
- **Every app**: a workspace path written as a link does not open in a chat. Attach the file.
