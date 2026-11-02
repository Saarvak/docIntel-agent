"""Send a chat message to the locally running DocIntel agent and print its reply.

Terminal 1:  python agent.py
Terminal 2:  python test_client.py "sample invoice-foobar-labs"
             python test_client.py "https://example.com/invoice.pdf"
"""

from __future__ import annotations

import os
import sys

from uagents import Agent, Context, Protocol
from uagents.resolver import RulesBasedResolver
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.identity import Identity

from docintel.config import get_settings

settings = get_settings()
TARGET = Identity.from_seed(settings.agent_seed, 0).address
TARGET_ENDPOINT = f"http://127.0.0.1:{settings.agent_port}/submit"
PROMPT = " ".join(sys.argv[1:]) or "sample invoice-sliced"
CLIENT_PORT = 8003

client = Agent(
    name="docintel-test-client",
    seed="docintel local test client seed",
    port=CLIENT_PORT,
    endpoint=[f"http://127.0.0.1:{CLIENT_PORT}/submit"],
    resolve=RulesBasedResolver({TARGET: TARGET_ENDPOINT}),
)
chat = Protocol(spec=chat_protocol_spec)


@client.on_event("startup")
async def send_request(ctx: Context) -> None:
    print(f"-> {TARGET}\n-> {PROMPT!r}\n", flush=True)
    status = await ctx.send(TARGET, ChatMessage(content=[TextContent(text=PROMPT)]))
    if status.status != "delivered":
        print(f"Could not deliver the message ({status.status}: {status.detail}). Is `python agent.py` running?", flush=True)
        os._exit(1)


@chat.on_message(ChatMessage)
async def on_reply(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))
    print(msg.text(), flush=True)
    os._exit(0)


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    print("(agent acknowledged, processing...)\n", flush=True)


client.include(chat)

if __name__ == "__main__":
    client.run()
