#!/usr/bin/env python3
"""
飞书消息卡片发送脚本

用法:
    python3 feishu_send_card.py --chat-id oc_xxx --title "标题" --content "markdown内容"
    python3 feishu_send_card.py --chat-id oc_xxx --title "标题" --file /path/to/content.md
    python3 feishu_send_card.py --chat-id oc_xxx --card-json '{"type":"template",...}'
    # 发私聊：接收方 id 传 open_id，并显式指定类型
    python3 feishu_send_card.py --chat-id ou_xxx --receive-id-type open_id \
        --title "标题" --subtitle "窗口与口径" --file /path/to/content.md

卡片模板:
    Market Digest 使用预定义模板，自动将 markdown 转成分段卡片
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from typing import Any, Optional, Tuple


CARD_MARKDOWN_MAX_CHARS = 2000


def split_markdown_blocks(text: str, max_chars: int = CARD_MARKDOWN_MAX_CHARS) -> list[str]:
    """Split markdown without dropping content, preferring paragraph boundaries."""
    remaining = text.strip()
    blocks: list[str] = []

    while remaining:
        if len(remaining) <= max_chars:
            blocks.append(remaining)
            break

        split_at = remaining.rfind("\n\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind("\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars

        block = remaining[:split_at].rstrip()
        if block:
            blocks.append(block)
        remaining = remaining[split_at:].lstrip("\n")

    return blocks


def get_tenant_token() -> str:
    """从 openclaw.json 读取飞书配置并获取 tenant_access_token"""
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    
    with open(config_path, 'r') as f:
        content = f.read()
    
    # 尝试直接解析 JSON
    try:
        config = json.loads(content)
    except json.JSONDecodeError:
        # 如果失败，尝试清理 JSON5 特性
        # 移除单行注释
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        # 移除尾随逗号
        content = re.sub(r',\s*([}\]])', r'\1', content)
        config = json.loads(content)
    feishu = config.get("channels", {}).get("feishu", {})
    
    app_id = feishu.get("appId")
    app_secret = feishu.get("appSecret")
    
    # 支持 accounts.default 结构
    if not app_id and "accounts" in feishu:
        default_acc = feishu.get("accounts", {}).get("default", {})
        app_id = default_acc.get("appId")
        app_secret = default_acc.get("appSecret")
    
    if not app_id or not app_secret:
        raise ValueError("Missing feishu appId or appSecret in config")
    
    # 获取 tenant_access_token
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(url, data, headers={"Content-Type": "application/json"})
    
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    
    if result.get("code") != 0:
        raise ValueError(f"Failed to get token: {result}")
    
    return result["tenant_access_token"]


def upload_image(path: str, token: str) -> str:
    """上传本地图片到飞书, 返回 img_key (需要应用有 im:resource 权限)."""
    import uuid

    with open(path, "rb") as f:
        data = f.read()
    fname = os.path.basename(path)
    boundary = uuid.uuid4().hex
    parts = [
        (f'--{boundary}\r\nContent-Disposition: form-data; name="image_type"'
         f'\r\n\r\nmessage\r\n').encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
         f'filename="{fname}"\r\nContent-Type: image/png\r\n\r\n').encode()
        + data + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/images",
        body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if result.get("code") != 0:
        raise ValueError(f"Image upload failed for {path}: {result}")
    return result["data"]["image_key"]


IMG_LINE_RE = re.compile(r"^IMG:(\S+)\s*$")


def content_to_elements(text: str) -> list[dict[str, Any]]:
    """文本转卡片元素; 行首 `IMG:/abs/path.png` 转为图片占位元素."""
    elements: list[dict[str, Any]] = []
    buf: list[str] = []

    def flush_text() -> None:
        chunk = "\n".join(buf).strip()
        buf.clear()
        if chunk:
            for block in split_markdown_blocks(chunk):
                elements.append({"tag": "markdown", "content": block})

    for line in text.split("\n"):
        m = IMG_LINE_RE.match(line.strip())
        if m:
            flush_text()
            elements.append({"tag": "__img__", "path": m.group(1)})
        else:
            buf.append(line)
    flush_text()
    return elements


def resolve_image_placeholders(card: dict[str, Any], token: str) -> None:
    """把 __img__ 占位元素上传并替换为飞书 img 元素，递归支持 Card 2.0 及折叠面板."""
    def _resolve_list(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resolved = []
        for el in elements:
            if el.get("tag") == "__img__":
                img_key = upload_image(el["path"], token)
                resolved.append({
                    "tag": "img",
                    "img_key": img_key,
                    "alt": {"tag": "plain_text", "content": ""},
                    "mode": "fit_horizontal",
                    "preview": True,
                })
            elif el.get("tag") == "collapsible_panel" and "elements" in el:
                el["elements"] = _resolve_list(el["elements"])
                resolved.append(el)
            else:
                resolved.append(el)
        return resolved

    if "body" in card and isinstance(card["body"], dict) and "elements" in card["body"]:
        card["body"]["elements"] = _resolve_list(card["body"]["elements"])
    if "elements" in card and isinstance(card["elements"], list):
        card["elements"] = _resolve_list(card["elements"])


NOTATION_SECTION_RE = re.compile(
    r"^(?:📝\s*)?(?:来源|信源|数据源|口径说明|口径限制|信源与数据口径|说明|备注|免责声明)$"
)
COLLAPSIBLE_SUFFIX_RE = re.compile(
    r"[\(（](?:折叠|点击展开)[\)）]$"
)


def auto_infer_tag(title: str) -> Optional[Tuple[str, str]]:
    """根据标题自动推断卡片顶部的业务标签与颜色."""
    t = title.lower()
    if "晨报" in title:
        return ("市场晨报", "blue")
    if "晚报" in title:
        return ("市场晚报", "purple")
    if "etf" in t:
        return ("ETF Tracking", "indigo")
    if "token" in t or "用量" in title:
        return ("用量与成本", "blue")
    if "ai" in t and ("投资" in title or "信号" in title or "日报" in title):
        return ("AI 投研", "purple")
    if "周报" in title or "weekly" in t or "tracker" in t:
        return ("周度 Tracking", "indigo")
    if "日报" in title or "daily" in t:
        return ("每日追踪", "blue")
    if "风险" in title or "risk" in t or "预警" in title or "alert" in t:
        return ("风险信号", "orange")
    if "监控" in title or "monitor" in t:
        return ("实时监控", "wathet")
    if "扫描" in title or "scan" in t:
        return ("趋势扫描", "turquoise")
    if "总结" in title or "summary" in t:
        return ("每日总结", "blue")
    return None


def markdown_to_card_elements(markdown: str, title: str, subtitle: str = "",
                              header_template: str = "indigo",
                              tag: str = "", tag_color: str = "") -> dict[str, Any]:
    """
    将 markdown 内容转成飞书 Card Schema 2.0 卡片 JSON
    
    卡片结构:
    - schema: "2.0"
    - config: wide_screen_mode, update_multi
    - header: 标题、副标题（原生呈现于彩条下方）、配色模板、业务标签
    - body: 各章节 markdown 块、折叠面板、说明性脚注（notation）、图片与分隔线
    """
    sections: list[dict[str, Any]] = []
    current_section = {"title": "", "content": []}

    def flush_current_section() -> None:
        nonlocal current_section
        content = current_section["content"]
        while content and not content[0].strip():
            content.pop(0)
        while content and not content[-1].strip():
            content.pop()
        section_title = str(current_section["title"] or "").strip()
        is_duplicate_card_title = bool(
            section_title and not content and title.strip().startswith(section_title)
        )
        if content or (section_title and not is_duplicate_card_title):
            sections.append(current_section)
        current_section = {"title": "", "content": []}

    lines = markdown.strip().split('\n')
    i = 0
    extracted_subtitle = ""

    while i < len(lines):
        line = lines[i]
        
        # 跳过顶级文档标题 (# 开头)
        if line.startswith('# ') and not line.startswith('## '):
            if not subtitle and i + 1 < len(lines) and lines[i + 1].startswith('> '):
                extracted_subtitle = lines[i + 1][2:].strip()
                i += 2
                continue
            i += 1
            continue

        # 二至四级标题都开始新章节；四级标题用于栏目内的独立子区块
        heading = re.match(r'^#{2,4}\s+(.+?)\s*$', line)
        if heading:
            flush_current_section()
            current_section = {
                "title": heading.group(1),
                "content": []
            }
            i += 1
            continue

        current_section["content"].append(line)
        i += 1

    flush_current_section()

    effective_subtitle = subtitle or extracted_subtitle

    # 构建 Header (Card Schema 2.0)
    header: dict[str, Any] = {
        "title": {
            "tag": "plain_text",
            "content": title
        },
        "template": header_template or "indigo"
    }
    if effective_subtitle:
        header["subtitle"] = {
            "tag": "plain_text",
            "content": effective_subtitle
        }

    # 业务标签
    if tag:
        header["text_tag_list"] = [{
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": tag},
            "color": tag_color or "blue"
        }]
    else:
        inferred = auto_infer_tag(title)
        if inferred:
            header["text_tag_list"] = [{
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": inferred[0]},
                "color": tag_color or inferred[1]
            }]

    # 构建 Body Elements
    elements: list[dict[str, Any]] = []
    titled_sections = [s for s in sections if s["title"]]

    if not titled_sections:
        # 无 ## 章节：全部内容作为普通 markdown 块
        elements.extend(content_to_elements(markdown.strip()))
    else:
        for section in sections:
            sec_title = section["title"].strip()
            if not sec_title:
                continue

            section_text = '\n'.join(section["content"]).strip()

            # 1. 脚注/信源/说明类栏目：使用 notation 次要字号呈现，不抢占主干焦点
            if NOTATION_SECTION_RE.match(sec_title):
                if elements and elements[-1].get("tag") != "hr":
                    elements.append({"tag": "hr"})
                notation_lines = [
                    f"<font color='grey'>{l}</font>" if l.strip() else ""
                    for l in f"**{sec_title}**\n{section_text}".splitlines()
                ]
                elements.append({
                    "tag": "markdown",
                    "text_size": "notation",
                    "content": "\n".join(notation_lines)
                })
                continue

            # 2. 折叠面板栏目：以 (折叠) / (点击展开) 结尾或例行类栏目
            is_collapsible = bool(COLLAPSIBLE_SUFFIX_RE.search(sec_title)) or sec_title.startswith(("例行披露", "长尾"))
            if is_collapsible:
                if elements and elements[-1].get("tag") != "hr":
                    elements.append({"tag": "hr"})
                clean_title = COLLAPSIBLE_SUFFIX_RE.sub("", sec_title).strip()
                inner_elements = content_to_elements(section_text)
                elements.append({
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {
                            "tag": "markdown",
                            "content": f"**{clean_title}**"
                        },
                        "vertical_align": "center",
                        "padding": "4px 8px 4px 8px",
                        "icon": {
                            "tag": "standard_icon",
                            "token": "down-small-ccm_outlined",
                            "size": "12px 12px"
                        },
                        "icon_position": "right",
                        "icon_expanded_angle": -180
                    },
                    "border": {"color": "grey", "corner_radius": "6px"},
                    "vertical_spacing": "6px",
                    "padding": "8px 8px 8px 8px",
                    "elements": inner_elements
                })
                continue

            # 3. 常规业务章节
            section_content = f"**{sec_title}**\n\n{section_text}"
            elements.extend(content_to_elements(section_content))
            elements.append({"tag": "hr"})

        # 移除末尾多余分隔线
        while elements and elements[-1].get("tag") == "hr":
            elements.pop()

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True
        },
        "header": header,
        "body": {
            "direction": "vertical",
            "vertical_spacing": "8px",
            "elements": elements
        }
    }


RECEIVE_ID_TYPES = ["chat_id", "open_id", "user_id", "union_id", "email"]


def send_card(chat_id: str, card: dict[str, Any], token: str,
              receive_id_type: str = "chat_id") -> dict[str, Any]:
    """发送消息卡片。receive_id_type 决定 receive_id 是群还是个人。"""

    url = ("https://open.feishu.cn/open-apis/im/v1/messages"
           f"?receive_id_type={receive_id_type}")

    payload = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False)
    }
    
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url, 
        data, 
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}"
        }
    )
    
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _mirror_to_discord(chat_id, receive_id_type, card, label):
    """发到 Eve tracking 群成功后同步一份到 Discord；失败只报 stderr，不影响飞书结果。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from eve_tracking_mirror import mirror_after_feishu
    except Exception as exc:  # noqa: BLE001
        print(f"[eve-tracking-mirror] 未加载，Discord 未同步（飞书已发送成功，不要重发飞书）：{exc}",
              file=sys.stderr)
        return None
    return mirror_after_feishu(chat_id, receive_id_type, card, label)


def main():
    parser = argparse.ArgumentParser(description="发送飞书消息卡片 (Card Schema 2.0)")
    parser.add_argument("--chat-id", required=False, default="",
                        help="接收方 id：群 chat_id（默认），或按 --receive-id-type 传 open_id 等")
    parser.add_argument("--receive-id-type", default="chat_id", choices=RECEIVE_ID_TYPES,
                        help="接收方 id 类型，默认 chat_id；发私聊传 open_id")
    parser.add_argument("--title", help="卡片标题")
    parser.add_argument("--subtitle", help="标题下的小字备注，放窗口与口径这类元信息")
    parser.add_argument("--tag", help="卡片右上角业务标签文案（如：市场晨报、周度 Tracking）")
    parser.add_argument("--tag-color", default="", help="标签颜色（blue/purple/orange/indigo/wathet/green/grey等）")
    parser.add_argument("--header-template", default="indigo",
                        help="卡片标题栏配色：indigo(默认)/blue/turquoise/green/orange/red/carmine/violet/purple/grey 等")
    parser.add_argument("--content", help="Markdown 内容（直接传入）")
    parser.add_argument("--file", help="Markdown 内容文件路径")
    parser.add_argument("--card-json", help="完整的卡片 JSON（跳过模板转换）")
    parser.add_argument("--card-json-file", help="完整卡片 JSON 的文件路径（跳过模板转换；大卡片别塞进命令行参数）")
    parser.add_argument("--dry-run", action="store_true", help="只打印卡片 JSON，不执行发送网络请求")
    parser.add_argument("--json", action="store_true", help="JSON 输出模式")
    
    args = parser.parse_args()
    
    try:
        if args.card_json or args.card_json_file:
            # 直接使用传入的卡片 JSON
            if args.card_json_file:
                with open(args.card_json_file, "r", encoding="utf-8") as f:
                    card = json.load(f)
            else:
                card = json.loads(args.card_json)
            # 兼容：如果传入的是 1.0 卡片（顶层含 elements 且无 schema: 2.0），自动平滑升级为 2.0
            if card.get("schema") != "2.0" and "elements" in card:
                card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True, "update_multi": True},
                    "header": card.get("header", {}),
                    "body": {
                        "direction": "vertical",
                        "vertical_spacing": "8px",
                        "elements": card.get("elements", [])
                    }
                }
        else:
            # 从 markdown 构建卡片
            if args.file:
                with open(args.file, 'r', encoding='utf-8') as f:
                    content = f.read()
            elif args.content:
                content = args.content
            else:
                raise ValueError("Must provide --content, --file, or --card-json")
            
            title = args.title or "Market Digest"
            card = markdown_to_card_elements(
                markdown=content,
                title=title,
                subtitle=args.subtitle or "",
                header_template=args.header_template,
                tag=args.tag or "",
                tag_color=args.tag_color or ""
            )
        
        # 如果是 dry-run 模式，直接打印卡片 JSON 并退出，不请求网络
        if args.dry_run:
            print(json.dumps(card, ensure_ascii=False, indent=2))
            return

        if not args.chat_id:
            raise ValueError("--chat-id is required when not running in --dry-run mode")

        token = get_tenant_token()
        mirror_source = json.loads(json.dumps(card))  # 图片占位上传后只剩 img_key，同步要在这之前取
        resolve_image_placeholders(card, token)
        result = send_card(args.chat_id, card, token, args.receive_id_type)
        mirror = None
        if result.get("code") == 0:
            mirror = _mirror_to_discord(args.chat_id, args.receive_id_type, mirror_source, args.title or "")
        
        if args.json:
            output = {
                "status": "ok" if result.get("code") == 0 else "error",
                "message_id": result.get("data", {}).get("message_id"),
                "result": result
            }
            if mirror is not None:
                output["discord_mirror"] = {k: mirror.get(k) for k in ("status", "messages_total", "error")}
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            if result.get("code") == 0:
                print(f"✅ 卡片发送成功: {result.get('data', {}).get('message_id')}")
            else:
                print(f"❌ 发送失败: {result}")
                sys.exit(1)
    
    except Exception as e:
        if args.json:
            print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
