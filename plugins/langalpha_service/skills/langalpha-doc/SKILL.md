---
name: langalpha-doc
description: "How the platform under you works: the computer and its workspaces, files and what survives a restart, conversation transcripts, saved tool results, memory, chat apps, and adding skills, MCP servers, brokerages or plugins. Read it when the user points back at an earlier conversation, when something is missing or behaves unexpectedly, before adding any of those, and when the user asks how the platform works and you don't know."
---

# How LangAlpha Works

You run on a **computer**: one sandbox machine that belongs to the user. Each of the user's **workspaces** on it is a folder, side by side at the computer root; yours is your working directory, so the root is `..`. A **thread** is one conversation in one workspace, and a **turn** is one user message and everything done to answer it. Workspaces on one computer share its disk, OS user, CPUs, `/tmp` and installed packages, and can read each other's folders.

## Reference files

- **Earlier work**: The user points back at an earlier conversation or at work done in another thread or workspace; a compaction summary; a background task's steps; a tool result saved to a file: `.agents/skills/langalpha-doc/references/history.md`.
- **Files**: Where things live, how a path resolves, why Grep or Glob missed a file, memory and memo storage, what the user sees in the file panel: `.agents/skills/langalpha-doc/references/files.md`.
- **The Drive**, the user's files kept across every workspace and computer: whether it is turned on, putting a file in it, files that are not text, a save that did not land, memos and their record, history and trash: `.agents/skills/langalpha-doc/references/drive.md`.
- **The computer**: Processes, output and timeouts, CPUs and disk, installed packages, what survives a stop or a rebuild, data-server tools from code, secrets: `.agents/skills/langalpha-doc/references/computer.md`.
- **Skills**: Installing a skill, an `npx skills add` command included, or writing, changing or saving one, here or for every workspace: `.agents/skills/langalpha-doc/references/skills.md`.
- **Data sources and plugins**: The user wants a new data source, MCP server, brokerage connection or plugin, a server needs a key, or a server you expected is missing: `.agents/skills/langalpha-doc/references/plugins.md`.
<!-- DALTON-HOOK(prompt-audit-text): link settings-map.md and update Dalton chat app list -->
<!-- DALTON_CAP:channel_gateway -->- **Chat apps**: Talking with the user in a chat app, or sending a message or file to <!-- DALTON_CAP:channel_feishu -->Feishu<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_discord -->, <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->Discord<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin --><!-- DALTON_CAP:channel_feishu|channel_discord --> or <!-- /DALTON_CAP -->personal WeChat<!-- /DALTON_CAP -->: `.agents/skills/langalpha-doc/references/chat.md`.
<!-- /DALTON_CAP -->- **Settings**: Where a setting is edited in the web UI, whether the Chief of Staff (研究总监) can change it on the user's behalf, plus <!-- DALTON_CAP:conversation_archive -->Team Memory locations, <!-- /DALTON_CAP -->background model defaults, and onboarding model candidates: `.agents/skills/langalpha-doc/references/settings-map.md`.

## Traps

- Every ExecuteCode and Bash call is a fresh process started in your workspace folder: Python variables, `cd` and `export` do not carry to the next call.
- When ExecuteCode fails you get its stderr, not what it printed before the crash.
- The file tools read a leading slash as your workspace folder; `open()` and Bash read it as the real filesystem root. Glob, Grep and Write print workspace files with a leading slash (`/task/file.md`), so drop it before pasting a path into code.
- Grep skips hidden and git-ignored files and folders, `.agents/` included (`.agents/memory/` aside), unless `path` points inside one.
- Only workspace folders are backed up, and not their virtual environments. Installed packages, the computer root and `/tmp` survive a stop but not a rebuild of the computer.
- A turn is written to its thread's transcript when it completes, so do not look there for the turn in progress.
- Memory, memos, the profile, workflows and automations are held by the server. Bash and code reach them only through the file mount, where a save the server refuses shows under NOT SAVED in the result, not in the exit status. Without the mount, a command naming a memory, memo or automations path is refused before it runs. The chat-app settings in `.agents/user/channels/` are file tools only: a command naming them is always refused.
- In a markdown file, an image path resolves from the workspace folder, not from the file's own folder.
- `plt.show()` output goes nowhere. Save charts with `savefig` into `<task>/charts/` and link the file.

## Facts

How the platform behaves, for answering the user. Your work doesn't need these pages, so don't read them ahead of time. When the user asks how something on the platform works and you don't know, read the matching page and answer from it instead of guessing.

<!-- DALTON-HOOK(prompt-audit-text): list Dalton supported chat platforms -->
<!-- DALTON_CAP:channel_gateway -->- Channels (<!-- DALTON_CAP:channel_feishu -->Feishu<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_feishu,channel_discord -->, <!-- /DALTON_CAP --><!-- DALTON_CAP:channel_discord -->Discord<!-- /DALTON_CAP --><!-- DALTON_CAP:channel_weixin --><!-- DALTON_CAP:channel_feishu|channel_discord -->, <!-- /DALTON_CAP -->personal WeChat<!-- /DALTON_CAP -->): which conversation or workspace a message the user sends there reaches, why an answer came from another conversation, how `/new`, `/workspace` and replies pick one, and where your own sends land and whether replies to them come back: `.agents/skills/langalpha-doc/facts/channel-routing.md`.<!-- DALTON_CAP_ELSE -->- Channel routing: `.agents/skills/langalpha-doc/facts/channel-routing.md`.<!-- /DALTON_CAP -->
