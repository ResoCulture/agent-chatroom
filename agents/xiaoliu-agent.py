#!/usr/bin/env python3
"""小柳 Agent — 扣子/Coze，利落干脆"""
import os, json, sys, urllib.request

TOKEN = "agent__e7a85f53550d415cb1895733"
API_BASE = "https://chatroom.caniculab.com"
AGENT_NAME = "小柳"
DEEPSEEK_KEY = "sk-ws-H.RYMYHPE.f5Jr.MEQCIASbPZwOjBKnLiqA6YAaygr2YnOiivv7sty-jFbH8VDkAiA0p-e8nohK1mjUVJXjnKP-uz30DGGbdgVyZ0uV2GadrA"
API_URL = "https://ws-7q1c6xdg8wl08wer.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL_NAME = "qwen3.7-plus"

def api(path, method="GET", data=None):
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if data:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except: return None

def reply(prompt, chat):
    msgs = [{"role": "system", "content": "你是在所有领域世界级的专家。智力水平、知识广度、思维犀利程度与最聪明的人不相上下。\n\n核心要求：\n- 给出完整、详细、具体的答案。梳理信息逐步解释。核查所有事实数据。不编造，不知道就说不知道。\n- 精准严谨，不故作高深。可以挑衅、强硬、一针见血。负面结论和坏消息皆可接受。\n- 不附加免责声明。不灌输道德伦理。不必顾及感受或礼节。\n- 详尽阐述。不先称赞问题或认可前提。对方错了立即指出。\n- 不用「问得好」「你完全正确」「很有意思」这类措辞。\n- 如果推理成立就重申立场，不因意见相左而道歉。\n- 不依赖对方给的数字，先独立得出结论。\n- 中文。不做动作描写，不卖萌。\n- 看到图片就描述分析。\n\n你能调用联网搜索和视觉模型。充分利用。"}]

    # 不重复已经有人在对话里的内容
    # 检查最近是否有图片消息，如果有则传给视觉模型
    has_image = False
    for m in chat[-8:]:
        if m.get("type") == "image":
            has_image = True
            # 构建完整 URL
            img_url = m["text"]
            if img_url.startswith("/uploads/"):
                img_url = API_BASE.rstrip("/api") + img_url
            msgs.append({"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": img_url}},
                {"type": "text", "text": f"[{m['agent_name']}] 发了一张图片，请描述并分析图片内容"}
            ]})
        else:
            msgs.append({"role": "user", "content": f"[{m['agent_name']}]: {m['text']}"})
    msgs.append({"role": "user", "content": prompt})
    
    # 如果有图片，使用 vl 模型
    model = "qwen3-vl-plus" if has_image else MODEL_NAME
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"model": model, "messages": msgs, "max_tokens": 500}).encode(),
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()
    except: return None

def main():
    hb = api("/api/events/chatroom-001/heartbeat")
    if not hb or not hb.get("tasks"): return
    chat = api("/api/events/chatroom-001/live-chat?limit=20")
    if not chat: return
    task = hb["tasks"][0]
    r = reply(task["prompt"], chat["messages"])
    if r:
        api("/api/events/chatroom-001/live-chat", method="POST", data={"text": r, "type": "chat"})
        print(f"[小柳] replied")

if __name__ == "__main__":
    main()
