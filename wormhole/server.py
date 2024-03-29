import asyncio
import itertools
import json
import logging
import os
import re
from collections import namedtuple

import aiohttp
import websockets
import yaml

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO)

Event = namedtuple("Event", "line source nick user host code args msg channel")

to_ws = asyncio.Queue()
to_irc = asyncio.Queue()

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)))

SETTINGS_PATH = os.environ.get("SETTINGS_FILE", os.path.join(HERE, "settings.yaml"))
with open(SETTINGS_PATH) as settings_fp:
    settings = yaml.safe_load(settings_fp)

irc_settings = settings["irc"]
SLACK_TOKEN = settings["slack"].get("token", os.environ.get("SLACK_TOKEN"))

NAME = irc_settings.get("nick", "wormhole")
IRC_COMMAND_REGISTRY = []

SLACK_HEADERS = {"Authorization": f"Bearer {SLACK_TOKEN}"}
SLACK_NAME_TO_ID = {}
SLACK_ID_TO_NAME = {}
SLACK_USERS = {}
SLACK_IDS = itertools.count()


def command(func):
    IRC_COMMAND_REGISTRY.append(func)
    return func


async def get_from_pastebin(num):
    async with aiohttp.ClientSession(headers=SLACK_HEADERS) as session:
        async with session.get(
            f"http://pastebin.test.redhat.com/pastebin.php?dl={num}"
        ) as resp:
            return await resp.text()


async def slack_pb(text, channel):
    async with aiohttp.ClientSession(headers=SLACK_HEADERS) as session:
        async with session.post(
            "https://slack.com/api/files.upload",
            data={
                "content": text,
                "filetype": "auto",
                "token": SLACK_TOKEN,
                "filename": None,
                "channels": channel,
            },
        ) as response:
            d = await response.json()
            return d["file"]["permalink"]


@command
async def handle_pastebin(irc, event):
    match = re.search(r"https?://pastebin.test.redhat.com/(?P<num>\d+)", event.msg)
    if match:
        text = await get_from_pastebin(match.group("num"))
        channel = irc_to_slack(event.channel)
        link = await slack_pb(text, channel)
        logger.debug("Copied pastebin to slack: %s", link)


@command
async def forward(irc, event):
    if event.nick != NAME and event.msg and event.msg.strip() != "" and event.code == "PRIVMSG":
        channel = irc_to_slack(event.channel)
        logger.info(f"About to forward to {channel}")
        if channel:
            payload = {"channel": channel, "text": f"*{event.nick}* says '{event.msg}'"}
            await to_ws.put(payload)



def parse_line(line):
    source = nick = user = host = None
    msg = line

    if line[0] == ":":
        pos = line.index(" ")
        source = line[1:pos]
        msg = line[pos + 1:]
        i = source.find("!")
        j = source.find("@")
        if i > 0 and j > 0:
            nick = source[:i]
            user = source[i + 1:j]
            host = source[j + 1:]

    sp = msg.split(" :", 1)
    code, *args = sp[0].split(" ")
    if len(sp) == 2:
        args.append(sp[1])

    return Event(
        line,
        source,
        nick,
        user,
        host,
        code,
        args,
        args[-1],
        args[0] if "#" in args[0] else None,
    )


class IRCProtocol(asyncio.Protocol):

    def __init__(self):
        self.transport = None
        self.channels = set()

    def connection_made(self, transport):
        self.transport = transport
        logger.debug(f"Connected to {transport}")
        self.send_data(f"NICK {NAME}")
        self.send_data(f"USER {NAME} 0 * :{NAME}")
        logger.debug(irc_settings)
        for channel in irc_settings["channels"]:
            self.join(channel)
        logger.debug("About to start consume_to_irc")
        self.consume_task = asyncio.Task(consume_to_irc(self))

    def data_received(self, data):
        logger.debug(f"Data received '{data.decode()}'")
        for chunk in data.decode().rstrip().split("\r\n"):
            logger.debug(f"Chunk -> '{chunk}'")
            event = parse_line(chunk)
            self.handle_event(event)

    def connection_lost(self, exc):
        logger.debug(f"IRC Connection lost: {exc}")
        logger.debug("Attempting to reconnect...")
        self.consume_task.cancel()
        IRCProtocol.connect()

    def handle_event(self, event):
        logger.debug(f"Event -> '{event}'")
        if event.code == "PRIVMSG":
            logger.info(f"{event.channel}:{event.nick}:{event.msg}")
        if event.code == "PING":
            self.send_data(f"PONG {event.args[0]}")
        else:
            for handler in IRC_COMMAND_REGISTRY:
                asyncio.Task(handler(self, event))

    def join(self, channel):
        self.send_data(f"JOIN #{channel}")

    def send_to_channel(self, channel, line):
        channel = f"#{channel.lstrip('#')}"
        self.send_data(f"PRIVMSG {channel} :{line}")

    def send_data(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.transport.write(data + b"\r\n")

    @staticmethod
    def connect():
        loop = asyncio.get_event_loop()
        irc_conn = loop.create_connection(
            IRCProtocol,
            irc_settings.get("server", "localhost"),
            irc_settings.get("port", 6667),
        )
        loop.run_until_complete(irc_conn)


def slack_to_irc(chan_id):
    try:
        return settings["chanmaps"][SLACK_ID_TO_NAME[chan_id]]

    except:
        logger.debug("No mapping for slack channel %s", SLACK_NAME_TO_ID.get(chan_id))


def irc_to_slack(irc_chan, inverse={v: k for k, v in settings["chanmaps"].items()}):
    try:
        return SLACK_NAME_TO_ID[inverse[irc_chan.strip("#")]]

    except Exception as e:
        logger.info("No mapping for irc channel %s in %s: %s", irc_chan, inverse, e)


async def consume_to_ws(ws):
    logger.info("Starting to consume %s", to_ws)
    logger.info(asyncio.get_event_loop().is_running())
    while asyncio.get_event_loop().is_running():
        event = await to_ws.get()
        event["id"] = next(SLACK_IDS)
        event["type"] = "message"
        logger.debug(f"Got {event}; sending to {ws}")
        await ws.send(json.dumps(event))


async def produce_to_irc(ws):
    async for msg in ws:
        logger.debug(f"Got {msg} from {ws}; sending to IRC")
        await to_irc.put(msg)


def handle_chat(msg, irc):
    if "channel" not in msg:
        return

    if "subtype" in msg:
        return

    user = SLACK_USERS[msg["user"]]
    if user == NAME:
        return

    message = msg["text"]
    if not message.strip():
        return

    ch = slack_to_irc(msg["channel"])
    if not ch:
        return

    irc.send_to_channel(ch, f"{user} says '{message}'")


def handle_upload(msg, irc):
    if "channel" not in msg:
        return

    if not msg.get("upload", False):
        return

    user = SLACK_USERS[msg["user"]]
    if user == NAME:
        return

    ch = slack_to_irc(msg["channel"])
    if not ch:
        return

    for f in msg["files"]:
        message = f"{user} pasted {f['permalink']}"
        irc.send_to_channel(ch, message)


async def consume_to_irc(irc):
    logger.debug(f"staring loop to consume: {to_irc}")
    while asyncio.get_event_loop().is_running():
        msg = await to_irc.get()
        logger.debug("consume_to_irc:Got %s", msg)
        msg = json.loads(msg)
        logger.debug("Read '%s' from the to_irc Q", msg)
        for cb in (handle_chat, handle_upload):
            try:
                cb(msg, irc)
            except Exception as e:
                logger.debug("Exception in callback(%s): %s", cb, e)


async def wsclient():
    global SLACK_NAME_TO_ID
    global SLACK_ID_TO_NAME
    global SLACK_USERS

    async with aiohttp.ClientSession(headers=SLACK_HEADERS) as session:
        async with session.get("https://slack.com/api/rtm.start") as r:
            resp = await r.json()
            logger.debug(resp)
            uri = resp["url"]
            SLACK_NAME_TO_ID = {
                c["name"]: c["id"] for c in resp["channels"] if c["is_member"]
            }
            SLACK_ID_TO_NAME = {
                c["id"]: c["name"] for c in resp["channels"] if c["is_member"]
            }
            SLACK_USERS = {
                u["id"]: u["real_name"] for u in resp["users"] if "real_name" in u
            }


    async with websockets.connect(uri) as ws:
        ctw = asyncio.Task(consume_to_ws(ws))
        pti = asyncio.Task(produce_to_irc(ws))
        while ws.open:
            await asyncio.sleep(1)

        ctw.cancel()
        pti.cancel()

    # connection lost, reconnect...
    logger.info("Slack connection lost, reconnecting.")
    asyncio.Task(wsclient())


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    IRCProtocol.connect()
    loop.run_until_complete(wsclient())
    loop.run_forever()
    loop.close()
