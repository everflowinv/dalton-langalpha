# Channel Routing

<!-- DALTON-HOOK(standalone-core): gate channel routing guidance on active Dalton channel capabilities -->
<!-- DALTON_CAP:channel_feishu|channel_discord|channel_weixin -->Which conversation a message the user sends on a connected channel (<!-- DALTON_CAP:channel_feishu -->Feishu<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_discord -->, <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->Discord<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_weixin|channel_discord,channel_weixin -->, <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin -->personal WeChat<!-- /DALTON_CAP -->) reaches, and why an answer can come from a different conversation than the one they expected.

## The rules every app shares

- A conversation is one thread, and it runs in one place: All workspaces, where the Chief of Staff answers from Home, or one workspace, where its Analyst answers. It keeps that place for as long as it lasts.
- A message that isn't a reply goes to the chat's current conversation, or starts a new one; each app below says which.
<!-- DALTON_CAP:channel_feishu|channel_discord -->- A reply goes to the conversation of the message it answers, not to the chat's current one. In <!-- DALTON_CAP:channel_feishu -->Feishu<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_discord --> and <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->Discord<!-- /DALTON_CAP --> a reply is a message in a thread or topic.
<!-- /DALTON_CAP -->- A message that starts with `/new` starts a new conversation and runs the rest of the message there, on every app. It never changes the place.<!-- DALTON_CAP:channel_discord --> In a Discord thread that isn't the user's it gets a lock, except that `/new <text>` typed in a thread nobody owns continues that thread's conversation.<!-- /DALTON_CAP -->
- A new conversation starts in the place the user set for that chat with `/workspace`; in a group, each person has their own. A chat without one uses the account's default, and with no default it runs in All workspaces. `.agents/user/channels/channels.json` holds this user's settings (`default`, and each app's `chats`): read it before telling the user where a chat runs now.
- Changing a chat's place doesn't move a conversation already started; each app below says when the next message starts a new one.
- While an answer is running in a conversation, a message the user sends in it joins that answer. A `/new` with text, or the first message after a bare `/new` or after a `/workspace` that starts the conversation over, waits instead and runs as a new conversation when the answer ends; anything sent after it waits behind it.<!-- DALTON_CAP:channel_feishu --> On Feishu a message with files also waits, and runs on its own.<!-- /DALTON_CAP -->
- A result you handed to an Analyst comes back in the thread, topic or chat the request came from.
- Someone who hasn't connected their account gets a prompt to connect it, not an answer.

## Why one chat shows two conversations

One chat can show answers from more than one conversation. In a group, each person has their own. And a chat shows whatever you sent there from another conversation or an automation.<!-- DALTON_CAP:channel_feishu|channel_discord --> In <!-- DALTON_CAP:channel_feishu -->Feishu<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_discord --> and <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->a Discord server<!-- /DALTON_CAP -->, those sends go in your conversation's own thread or topic there, opening one if it has none, and the user's messages there come back to you.<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord|channel_weixin --> In <!-- DALTON_CAP:channel_discord -->a Discord DM<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord,channel_weixin --> or <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin -->a personal WeChat DM<!-- /DALTON_CAP -->, they sit among the chat's own messages, so two answers in one chat can come from two conversations with different histories.<!-- /DALTON_CAP -->
<!-- DALTON_CAP:channel_feishu -->
## Feishu

- In a DM every message is answered. In a group, the user mentions the app, or writes in a topic where it was mentioned in the last 30 days. On a company's own Feishu app every group message needs a mention, unless the company gave the app all group messages; even then, whoever connected it can require a mention again.
- Each topic is a conversation, one per person, in a DM too. A message at the top starts a new one, and replies in its topic continue it.
- `/new` alone makes the topic it was typed in start over with the user's next message there within 24 hours.
- After the place changes, by `/workspace`, channels.json or the account default, each of the user's conversations in that chat starts over with their next message there. A move to All workspaces does this only within 24 hours of a `/workspace` or channels.json change, and never for the account default.
- There is no command to stop an answer.
- You can message the user's DM, or a group they picked for you and talked to you in within the last 90 days; a group they haven't opens up once they mention the app there. A message you send goes in your conversation's topic in that chat, or at the top, opening one. The user's replies in that topic come back to you, in a group without a mention. `new_thread` starts another topic; the old one still reaches the same conversation. A hand-off's result comes back as a reply in its topic.
<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->
## Discord

- In a server channel, the user mentions the app to start; the answer opens a thread from that message. Inside the thread no mention is needed, and only the person who started it can continue it; anyone else gets a lock and no answer. A thread or forum post someone else started gets no answer unless the app has been added to it.
- Each mention in a channel starts a new conversation in a new thread. A DM is one conversation until `/new`, `/workspace` (even re-picking the same place) or a change to the DM in channels.json, after which the next message starts a new one if it comes within 24 hours; otherwise the old one goes on, in its old place. Changing the account default doesn't restart it.
- `/new <text>` runs the text in a new conversation: from a channel, in a new thread; in a DM, or in a thread that is the user's, in place. `/new` alone makes the user's next message there within 24 hours start one.
- `/workspace` in a server needs the server-wide Manage Channels permission and sets the place for that person in that channel; inside a thread it sets the thread's channel. After `/workspace` in a thread, the next message there within 24 hours starts a new conversation.
- There is no command to stop an answer.
- A message you send to a server channel goes in your conversation's thread there, or opens one from your first message; the user's replies there come back to you. Where the app can't start a thread, your message stays in the channel and replies to it don't come back; your next send there tries the thread again. A DM has no threads: the user's next message there, even a Discord reply to yours, goes to the DM's own conversation.
<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin -->
## Personal WeChat

- Direct messages only (`weixin:@me`); group chats are not supported.
- The DM holds one active conversation at a time until `/new` or `/workspace`, after which the next message starts a new conversation in the chosen place.
- `/new <text>` starts a new conversation with that text right away; `/new` alone makes the user's next message start a new conversation.
- Messages you send to `weixin:@me` appear directly in the user's DM alongside the chat's own messages (no threads or quoted reply routing).
<!-- /DALTON_CAP --><!-- DALTON_CAP_ELSE -->No chat app channels are enabled in this deployment.
<!-- /DALTON_CAP -->
