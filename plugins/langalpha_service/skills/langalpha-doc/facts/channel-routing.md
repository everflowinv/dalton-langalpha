# Channel Routing

<!-- DALTON-HOOK(standalone-core): gate channel routing guidance on active Dalton channel capabilities -->
<!-- DALTON_CAP:channel_feishu|channel_discord|channel_weixin -->How messages sent on connected chat apps (<!-- DALTON_CAP:channel_feishu -->Feishu<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_discord -->, <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->Discord<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_weixin|channel_discord,channel_weixin -->, <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin -->personal WeChat<!-- /DALTON_CAP -->) map to conversations, workspaces, and slash commands.

## Shared rules and commands

- Each conversation scope keeps one active thread per user, running either in Home (Chief of Staff) or in a chosen workspace (its Analyst).
- Send `/workspace` (or `/workspace list`) to view your current place and list available workspaces.
- Send `/workspace <name>`, `/workspace <workspace_id>`, or `/<workspace name>` to switch to another place, which starts a fresh thread there (if you are already in that place, the current thread continues; send `/new` for a fresh one); send `/main` or `/workspace home` to switch back to Home (Chief of Staff).
- Send `/new` by itself to start a fresh thread in the current place, then send your question in the next message.
- Send `/stop` by itself to stop the currently running answer in that conversation.
- Send slash commands as a single line.
- While an answer is running, a follow-up message sent in the same conversation joins that thread; `/stop` cancels the active run.
- A result handed off to an Analyst returns to the chat or topic where the request started.
- A user links their chat account first to start chatting: <!-- DALTON_CAP:channel_feishu|channel_discord -->generate a bind code in Settings → Messaging & Channels and send `/bind <CODE>` in a DM on <!-- DALTON_CAP:channel_feishu -->Feishu<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_discord --> or <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->Discord<!-- /DALTON_CAP --> (send `/unbind` to disconnect)<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_weixin|channel_discord,channel_weixin -->; <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin -->scan the QR code in Settings → Messaging & Channels to link personal WeChat (send `/unbind` to disconnect)<!-- /DALTON_CAP -->.
<!-- DALTON_CAP:channel_feishu|channel_discord -->- In a Feishu or Discord group, each linked internal member has their own conversation scope.
<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu -->
## Feishu

- **Direct messages (DM):** Messages from the same sender reuse one active conversation (`feishu:@me`), continuing the same thread until you send `/new` or switch places with `/workspace` or `/main` (if a workspace is selected and the chat has had no messages for 6 hours, the next message returns to Home in a fresh thread; a thread in Home simply continues).
- **Group chats:** Mention the app to start a new topic conversation; the starter can continue replying inside that topic thread with or without mentioning the app again. Mention the app in the main group chat to start a separate topic conversation.
- **Commands:** `/workspace`, `/workspace <name|id>`, `/<workspace name>`, `/main`, `/new`, and `/stop` work in Feishu. In a group, send them inside the topic thread of the conversation they should act on (`/stop` in the topic whose answer is running). Send `/bind <CODE>` and `/unbind` in a DM with the app.
- **Agent sends:** Messages sent to `feishu:@me` arrive in the user's DM; messages sent to a group (`feishu:<chat_id>`) arrive in that group or its active topic thread.
<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->
## Discord

- **Direct messages (DM):** Messages from the same sender reuse one active conversation (`discord:@me`), continuing the same thread until you send `/new` or switch places with `/workspace` or `/main` (if a workspace is selected and the chat has had no messages for 6 hours, the next message returns to Home in a fresh thread; a thread in Home simply continues).
- **Server channels:** Mention the app in a channel to start a conversation in a thread; the person who started that thread can continue replying inside it without mentioning the app again.
- **Commands:** Text commands (`/workspace`, `/workspace <name|id>`, `/<workspace name>`, `/main`, `/new`, `/stop`) and Discord native slash commands (`/workspace`, `/main`, `/new`, `/stop`) both work; in a server channel, send them inside the thread they should act on. Send `/bind <CODE>` and `/unbind` in a DM with the app.
- **Agent sends:** Messages sent to `discord:@me` arrive in the user's DM; messages sent to a server channel (`discord:<guild>/<channel>`) arrive in that channel or thread.
<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin -->
## Personal WeChat

- **Direct messages (`weixin:@me`):** Messages from the linked user reuse one active conversation, continuing the same thread until you send `/new` or switch places with `/workspace` or `/main` (if a workspace is selected and the chat has had no messages for 6 hours, the next message returns to Home in a fresh thread; a thread in Home simply continues).
- **Commands:** `/workspace`, `/workspace <name|id>`, `/<workspace name>`, `/main`, `/new`, `/stop`, and `/unbind` work in the DM.
- **Agent sends:** Messages sent to `weixin:@me` appear directly in the user's DM alongside the chat's own messages.
<!-- /DALTON_CAP --><!-- DALTON_CAP_ELSE -->No chat app channels are enabled in this deployment.
<!-- /DALTON_CAP -->
