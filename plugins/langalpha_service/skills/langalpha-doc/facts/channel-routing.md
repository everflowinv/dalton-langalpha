# Channel Routing

<!-- DALTON-HOOK(standalone-core): gate channel routing guidance on active Dalton channel capabilities -->
<!-- DALTON_CAP:channel_feishu|channel_discord|channel_weixin -->How messages on connected chat apps (<!-- DALTON_CAP:channel_feishu -->Feishu<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_discord -->, <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->Discord<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_weixin|channel_discord,channel_weixin -->, <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin -->personal WeChat<!-- /DALTON_CAP -->) route to threads and workspaces, and how slash commands control them.

## Shared rules and commands

- Each conversation keeps one active thread per user, running either in Home (Chief of Staff) or in a selected workspace (its Analyst).
- Send `/workspace` (or `/workspace list`) to view your current workspace (or Home) and list available workspaces.
- Send `/workspace <name>`, `/workspace <workspace_id>`, or `/<workspace name>` to switch to a workspace in a new thread, and `/main` or `/workspace home` to switch back to Home (Chief of Staff). Switching to where you already are keeps the current thread; send `/new` to start a new one.
- Send `/new` by itself to start a new thread in your current workspace or Home, then send your question in the next message.
- Send `/stop` by itself to stop the active reply in that conversation.
- Send slash commands as a single line.
- While a reply is running, a follow-up message in the same conversation joins that thread.
- When a workspace is selected, after 6 hours of inactivity the next message returns to Home in a new thread; a thread in Home continues.
- A result handed off to an Analyst returns to the conversation where the request started.
- To start chatting, link your chat account first: <!-- DALTON_CAP:channel_feishu|channel_discord -->generate a bind code in Settings → Messaging & Channels and send `/bind <CODE>` in a DM on <!-- DALTON_CAP:channel_feishu -->Feishu<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_discord --> or <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->Discord<!-- /DALTON_CAP --> (send `/unbind` to disconnect)<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_weixin|channel_discord,channel_weixin -->; <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin -->scan the QR code in Settings → Messaging & Channels to link personal WeChat (send `/unbind` to disconnect)<!-- /DALTON_CAP -->.
<!-- DALTON_CAP:channel_feishu|channel_discord -->- In a group chat, each linked internal member has their own conversation.
<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu -->
## Feishu

- **Direct messages (DM):** Messages from the same sender reuse one active conversation (`feishu:@me`), continuing the same thread until you send `/new` or switch with `/workspace` or `/main`.
- **Group chats:** Mention the app to start a new topic conversation; the person who started it can keep replying inside that topic thread directly (mentioning the app again is optional). Mention the app in the main group chat to start a separate topic.
- **Commands:** `/workspace`, `/workspace <name|id>`, `/<workspace name>`, `/main`, `/new`, and `/stop` work in Feishu. In a group, send them inside the topic thread of the conversation they should act on (`/stop` in the topic whose reply is running). Send `/bind <CODE>` and `/unbind` in a DM with the app.
- **Agent sends:** Messages sent to `feishu:@me` arrive in the user's DM; messages sent to a group (`feishu:<chat_id>`) arrive in that group or its active topic thread.
<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->
## Discord

- **Direct messages (DM):** Messages from the same sender reuse one active conversation (`discord:@me`), continuing the same thread until you send `/new` or switch with `/workspace` or `/main`.
- **Server channels:** Mention the app in a channel to start a conversation in a thread; the person who started it can keep replying inside that thread directly (mentioning the app again is optional).
- **Commands:** Text commands (`/workspace`, `/workspace <name|id>`, `/<workspace name>`, `/main`, `/new`, `/stop`) and Discord native slash commands (`/workspace`, `/main`, `/new`, `/stop`) both work; in a server channel, send them inside the thread they should act on. Send `/bind <CODE>` and `/unbind` in a DM with the app.
- **Agent sends:** Messages sent to `discord:@me` arrive in the user's DM; messages sent to a server channel (`discord:<guild>/<channel>`) arrive in that channel or thread.
<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin -->
## Personal WeChat

- **Direct messages (`weixin:@me`):** Messages from the linked user reuse one active conversation, continuing the same thread until you send `/new` or switch with `/workspace` or `/main`.
- **Commands:** `/workspace`, `/workspace <name|id>`, `/<workspace name>`, `/main`, `/new`, `/stop`, and `/unbind` work in the DM.
- **Agent sends:** Messages sent to `weixin:@me` appear directly in the user's DM alongside the chat's own messages.
<!-- /DALTON_CAP --><!-- DALTON_CAP_ELSE -->No chat app channels are enabled in this deployment.
<!-- /DALTON_CAP -->
