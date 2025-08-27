import asyncio
import websockets
import json
import time
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
import tomllib
import uuid
from asyncio import Queue

# ==================== 路径 ====================
BASE = Path(__file__).parent
CONF = BASE / "config"
WELCOME_DIR = CONF / "welcome"
TRIG_DIR = CONF / "triggers"

# ==================== 读文件 & front-matter ====================
def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")

def parse_md_with_frontmatter(p: Path) -> Tuple[Dict[str, Any], str]:
    """
    支持 +++ TOML +++ 作为 front-matter。
    返回 (meta, body)
    """
    text = read_text(p)
    if text.startswith("+++"):
        end = text.find("+++", 3)
        if end != -1:
            meta_raw = text[3:end].strip()
            body = text[end+3:].lstrip("\r\n")
            try:
                meta = tomllib.loads(meta_raw) if meta_raw else {}
            except Exception as e:
                logging.warning(f"[FM] 解析 TOML 失败：{p}，错误：{e}")
                meta = {}
            return meta, body
    return {}, text

def read_toml(p: Path) -> dict:
    with open(p, "rb") as f:
        return tomllib.load(f)

# ---- 动作应答通道 ----
PENDING_ACTIONS: Dict[str, asyncio.Future] = {}
EVENT_QUEUE: "Queue[dict]" = Queue()

def _new_echo(prefix="act"):
    return f"{prefix}:{uuid.uuid4().hex}"

async def send_action(ws, action: str, params: dict, timeout: float = 10.0) -> dict:
    """
    通过 echo 等待 OneBot11 的动作应答（包含 message_id 等）。
    注意：需要 ws_reader 持续收包并派发应答。
    """
    echo = _new_echo(action)
    fut = asyncio.get_running_loop().create_future()
    PENDING_ACTIONS[echo] = fut
    payload = {"action": action, "params": params, "echo": echo}
    await ws.send(json.dumps(payload, ensure_ascii=False))
    try:
        resp = await asyncio.wait_for(fut, timeout=timeout)
    finally:
        PENDING_ACTIONS.pop(echo, None)
    return resp

async def ws_reader(ws):
    """
    专门的收包任务：
    - 若是动作应答（含 echo/status），投递到 PENDING_ACTIONS
    - 若是事件（含 post_type），投递到 EVENT_QUEUE
    """
    async for msg in ws:
        try:
            data = json.loads(msg)
        except Exception:
            continue
        # 动作应答
        if isinstance(data, dict) and data.get("echo") and data.get("status"):
            fut = PENDING_ACTIONS.get(data["echo"])
            if fut and not fut.done():
                fut.set_result(data)
            continue
        # 事件
        await EVENT_QUEUE.put(data)
        
# ==================== OneBot11 发送（保持你现有规范） ====================
async def send_group_msg(ws, group_id, message):
    resp = await send_action(ws, "send_group_msg", {
        "group_id": int(group_id),
        "message": message
    })
    data = resp.get("data") or {}
    return data.get("message_id") or data.get("id")

async def send_group_msg_segments(ws, group_id, segments: list):
    resp = await send_action(ws, "send_group_msg", {
        "group_id": int(group_id),
        "message": segments
    })
    data = resp.get("data") or {}
    return data.get("message_id") or data.get("id")

async def send_forward_message(ws, group_id, messages, sender_id, sender_name="洛拉娜·奥蕾莉娅"):
    nodes = []
    for msg in messages:
        nodes.append({
            "type": "node",
            "data": {"name": sender_name, "uin": str(sender_id), "content": msg}
        })
    resp = await send_action(ws, "send_group_forward_msg", {
        "group_id": int(group_id),
        "messages": nodes
    })
    data = resp.get("data") or {}
    return data.get("message_id") or data.get("id")

# ==================== 热重载内容存储 ====================
class Store:
    def __init__(self):
        self.settings: Dict[str, Any] = {}
        # welcome：
        # - plain_msgs: List[(order_key, text, path)]
        # - packs: List[(order_key, List[(order_key, text, path)] , dirpath)]
        self.welcome_plain: List[Tuple[str, str, Path]] = []
        self.welcome_packs: List[Tuple[str, List[Tuple[str, str, Path]], Path]] = []

        # triggers：
        # - singles: List[(triggers[], order_key, text, path)]
        # - groups: List[(triggers[], order_key, List[(order_key, text, path)], dirpath)]
        self.trig_singles: List[Tuple[List[str], str, str, Path]] = []
        self.trig_groups:  List[Tuple[List[str], str, List[Tuple[str, str, Path]], Path]] = []

        self._mtimes: Dict[Path, float] = {}
        self.self_id: Optional[str] = None  # 当前机器人ID（从事件里拿）

    def _mtime(self, p: Path) -> float:
        try:
            return p.stat().st_mtime
        except FileNotFoundError:
            return -1.0

    def _index_key(self, p: Path) -> str:
        # 用文件/目录名作为顺序 key（字符串排序即可：000_* < 001_*）
        return p.name

    def _list_md(self, d: Path) -> List[Path]:
        return sorted([p for p in d.glob("*.md") if p.is_file()], key=lambda x: x.name)

    def _changed(self, paths: List[Path]) -> bool:
        dirty = False
        for p in paths:
            m = self._mtime(p)
            if self._mtimes.get(p) != m:
                self._mtimes[p] = m
                dirty = True
        return dirty

    def _log_summary(self):
        wp_plain_cnt = len(self.welcome_plain)
        wp_pack_cnt  = sum(len(parts) for _, parts, _ in self.welcome_packs)
        trg_single_cnt = len(self.trig_singles)
        trg_group_pack_cnt = sum(len(parts) for _, _, parts, _ in self.trig_groups)
        trg_group_cnt = len(self.trig_groups)

        logging.info(
            f"🧩 配置加载完成 | 欢迎(根:{wp_plain_cnt} / 包内总片:{wp_pack_cnt}) | "
            f"触发(单:{trg_single_cnt} / 组:{trg_group_cnt}, 组内总片:{trg_group_pack_cnt})"
        )

    def load_all(self):
        # settings
        st = CONF / "settings.toml"
        self.settings = read_toml(st)
        logging.info(f"⚙️ 读取 settings.toml 成功：{st}")

        # welcome
        self.welcome_plain = []
        self.welcome_packs = []
        if WELCOME_DIR.exists():
            logging.info(f"📁 扫描欢迎目录：{WELCOME_DIR}")
            # 根级 md → 普通消息
            for f in self._list_md(WELCOME_DIR):
                meta, body = parse_md_with_frontmatter(f)
                self.welcome_plain.append((self._index_key(f), body.strip(), f))
                logging.debug(f"  - 欢迎根级：{f}")

            # 子目录 → 合并转发
            for sub in sorted([p for p in WELCOME_DIR.iterdir() if p.is_dir()], key=lambda x: x.name):
                parts: List[Tuple[str, str, Path]] = []
                for f in self._list_md(sub):
                    if f.name.startswith("_"):
                        continue
                    _, body = parse_md_with_frontmatter(f)
                    parts.append((self._index_key(f), body.strip(), f))
                    logging.debug(f"  - 欢迎包片段：{f}")
                if parts:
                    self.welcome_packs.append((self._index_key(sub), parts, sub))
                    logging.debug(f"  * 欢迎包目录：{sub}（片段数 {len(parts)}）")

        # triggers
        self.trig_singles = []
        self.trig_groups = []
        if TRIG_DIR.exists():
            logging.info(f"📁 扫描触发目录：{TRIG_DIR}")
            # 根级 .md → 单条触发
            for f in self._list_md(TRIG_DIR):
                meta, body = parse_md_with_frontmatter(f)
                if meta.get("triggers"):
                    tlist = [t for t in meta["triggers"] if isinstance(t, str) and t.strip()]
                    if tlist:
                        self.trig_singles.append((tlist, self._index_key(f), body.strip(), f))
                        logging.debug(f"  - 单条触发：{f} | 触发词={tlist}")

            # 子目录 → 触发即合并转发
            for sub in sorted([p for p in TRIG_DIR.iterdir() if p.is_dir()], key=lambda x: x.name):
                triggers: List[str] = []
                for f in self._list_md(sub):
                    if not f.name.startswith("_"):
                        continue
                    meta, _ = parse_md_with_frontmatter(f)
                    if meta.get("triggers"):
                        triggers.extend(list(meta["triggers"]))
                triggers = [t for t in triggers if isinstance(t, str) and t.strip()]
                parts: List[Tuple[str, str, Path]] = []
                for f in self._list_md(sub):
                    if f.name.startswith("_"):
                        continue
                    _, body = parse_md_with_frontmatter(f)
                    parts.append((self._index_key(f), body.strip(), f))
                if triggers and parts:
                    self.trig_groups.append((triggers, self._index_key(sub), parts, sub))
                    logging.debug(f"  * 组合触发目录：{sub} | 触发词={triggers} | 片段数={len(parts)}")

        # 记录 mtime
        paths = [st]
        if WELCOME_DIR.exists():
            paths += list(WELCOME_DIR.glob("**/*.md"))
        if TRIG_DIR.exists():
            paths += list(TRIG_DIR.glob("**/*.md"))
        for p in paths:
            self._mtimes[p] = self._mtime(p)

        self._log_summary()

    def maybe_reload(self) -> bool:
        paths = [CONF / "settings.toml"]
        if WELCOME_DIR.exists(): paths += list(WELCOME_DIR.glob("**/*.md"))
        if TRIG_DIR.exists():    paths += list(TRIG_DIR.glob("**/*.md"))
        if self._changed(paths):
            logging.info("🔁 检测到配置文件变更，开始热重载…")
            self.load_all()
            logging.info("♻️ 热重载完成")
            return True
        return False

STORE = Store()

# ==================== 迎新流程（保持你原有节奏 + 详细日志） ====================
新人记录: Dict[str, List[str]] = {}
定时器任务: Dict[str, asyncio.Task] = {}
触发冷却记录: Dict[Tuple[str, str], float] = {}

async def handle_new_member(ws, group_id, user_id):
    now_ms = int(time.time() * 1000)
    logging.info(f"👋 新人加入 | 群 {group_id} | 用户 {user_id} | ts={now_ms}")
    await send_group_msg(ws, STORE.settings["log_group"], f"【日志】用户 {user_id} 加入群 {group_id}，时间戳：{now_ms}")

    新人 = 新人记录.setdefault(group_id, [])
    if user_id not in 新人:
        新人.append(user_id)
    logging.info(f"👥 当前待欢迎列表[{group_id}]：{新人}")

    if group_id in 定时器任务:
        定时器任务[group_id].cancel()
        logging.info(f"⏹️ 取消已有定时器 | 群 {group_id}")

    delay = STORE.settings.get("welcome_delay_seconds", 60)
    task = asyncio.create_task(schedule_welcome(ws, group_id))
    定时器任务[group_id] = task
    logging.info(f"⏱️ 设定新定时器 | 群 {group_id} | delay={delay}s")
    await send_group_msg(ws, STORE.settings["log_group"], f"【日志】定时器重置：{group_id}")

async def schedule_welcome(ws, group_id):
    delay = STORE.settings.get("welcome_delay_seconds", 60)
    try:
        await asyncio.wait_for(asyncio.sleep(delay), timeout=delay + 10)
    except asyncio.CancelledError:
        logging.info(f"🛑 定时器被取消 | 群 {group_id}")
        return

    新人列表 = 新人记录.get(group_id, [])
    if not 新人列表:
        logging.info(f"⚠️ 欢迎触发失败：新人列表为空 | 群 {group_id}")
        await send_group_msg(ws, STORE.settings["log_group"], f"【日志】触发失败：新人列表为空，群号：{group_id}")
        return

    logging.info(f"🚀 开始发送欢迎消息 | 群 {group_id} | 新人={新人列表}")
    await send_group_msg(ws, STORE.settings["log_group"], f"【日志】开始发送欢迎消息：{group_id}")

    gap = STORE.settings.get("welcome_gap_seconds", 1)

    # 1) welcome 根级 md → 普通消息（按文件名顺序）
    for order_key, body, path in sorted(STORE.welcome_plain, key=lambda x: x[0]):
        if body:
            logging.info(f"📝 发送欢迎根级文本 | 群 {group_id} | 文件={path.name}")
            await send_group_msg(ws, group_id, body)
            await asyncio.sleep(gap)

    # 2) welcome 子目录 → 合并转发（按目录名顺序；目录内按文件名顺序）
    for dir_key, parts, dir_path in sorted(STORE.welcome_packs, key=lambda x: x[0]):
        texts = [b for _, b, _ in sorted(parts, key=lambda x: x[0]) if b]
        files = [p.name for _, _, p in sorted(parts, key=lambda x: x[0])]
        if texts:
            logging.info(f"📦 发送欢迎合并转发 | 群 {group_id} | 目录={dir_path.name} | 片段={files}")
            await send_forward_message(ws, group_id, texts, STORE.self_id or STORE.settings.get("forward_sender_id", "2162317375"))
            await asyncio.sleep(gap)

    # 3) @ 新人
    at_text = " ".join([f"[CQ:at,qq={qq}]" for qq in 新人列表])
    if at_text:
        logging.info(f"📣 @新人 | 群 {group_id} | {新人列表}")
        await send_group_msg(ws, group_id, at_text)

    # 清理
    新人记录.pop(group_id, None)
    定时器任务.pop(group_id, None)
    logging.info(f"🧹 清理欢迎状态完成 | 群 {group_id}")

# ==================== 触发逻辑（兼容你现有规则 + 详细日志） ====================
def build_sep_pattern():
    return r"(?:,|，|~|～|\?|？|!|！|…{1,2}|\.{3,6})"

def normalize_for_match(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s)

def extract_at_info_and_text(event: dict, self_id: Optional[str]) -> Tuple[bool, List[str], List[str], str]:
    """
    解析 OneBot11 标准消息段：
    返回 (has_at_me, leading_others, trailing_others, text_all)
    - has_at_me：是否 @ 了机器人（任意位置）
    - leading_others：句首连续出现的 @别人的 qq 列表（不含机器人）
    - trailing_others：句末连续出现的 @别人的 qq 列表（不含机器人）
    - text_all：把所有 text 段拼起来（仅文本）
    """
    msg = event.get("message")
    if not isinstance(msg, list):
        return False, [], [], ""

    def _is_blank_text(seg):
        return seg.get("type") == "text" and seg.get("data", {}).get("text", "").strip() == ""

    has_at_me = any(seg.get("type") == "at" and str(seg.get("data", {}).get("qq")) == str(self_id) for seg in msg)

    # 句首连续 @别人
    leading = []
    i = 0
    while i < len(msg):
        seg = msg[i]
        if _is_blank_text(seg):
            i += 1
            continue
        if seg.get("type") == "at":
            qq = str(seg.get("data", {}).get("qq"))
            if qq != str(self_id):
                leading.append(qq)
                i += 1
                continue
        break

    # 句末连续 @别人
    trailing = []
    j = len(msg) - 1
    while j >= 0:
        seg = msg[j]
        if _is_blank_text(seg):
            j -= 1
            continue
        if seg.get("type") == "at":
            qq = str(seg.get("data", {}).get("qq"))
            if qq != str(self_id):
                trailing.append(qq)
                j -= 1
                continue
        break
    trailing.reverse()

    # 汇总文本
    texts = []
    for seg in msg:
        if seg.get("type") == "text":
            texts.append(seg.get("data", {}).get("text", ""))
    text_all = "".join(texts).strip()
    return has_at_me, leading, trailing, text_all

def has_reply_segment(event: dict) -> bool:
    """是否包含 OneBot11 标准 reply 段"""
    msg = event.get("message")
    if not isinstance(msg, list):
        return False
    return any(seg.get("type") == "reply" for seg in msg)

async def handle_custom_triggers(ws, group_id, user_id, message, event=None):
    raw_msg = message
    message = message.strip()
    now = time.time()

    sep_pattern = build_sep_pattern()

    # —— 解析 OneBot11 消息段：是否 @了机器人、句首/句末 @别人、纯文本
    has_at_me, at_leading, at_trailing, text_only = (False, [], [], "")
    if isinstance(event, dict) and STORE.settings.get("mention_trigger_enabled", True):
        has_at_me, at_leading, at_trailing, text_only = extract_at_info_and_text(event, STORE.self_id)

    # 新增：检测是否含 reply 段
    has_reply = bool(event) and has_reply_segment(event)

    # 统一一个文本源：优先用 text_only，其次退回 raw 文本
    src_text = (text_only or message or "").strip()

    # —— 确定入口与“待匹配文本”
    used_at_entry = False
    used_reply_entry = False
    if has_at_me:
        # A) @机器人触发：不要求称呼
        content = src_text
        used_at_entry = True
        left = f"@{STORE.self_id}"
    elif has_reply:
        # B) **回复触发**：不要求称呼（允许直接“[回复]洛拉娜，签名”甚至“[回复]签名”）
        content = src_text
        used_reply_entry = True
        left = "[reply]"
    else:
        # C) 称呼 + 分隔符 + 内容
        trimmed = re.sub(r"[,，~～\?？!！…\.]+$", "", src_text)
        sep_match = re.search(sep_pattern, trimmed)
        if not sep_match:
            logging.debug(f"🔎 触发检查未命中：无@bot、无reply且无分隔符 | 群 {group_id} | 用户 {user_id} | msg={raw_msg!r} | src_text={src_text!r}")
            return
        sep_start, sep_end = sep_match.span()
        left = trimmed[:sep_start]
        right = trimmed[sep_end:]

        # 左侧必须包含称呼：名字（这里不要求 @机器人）
        names = STORE.settings.get("names", [])
        if names:
            name_pattern = "|".join(re.escape(n) for n in names)
            if not re.search(rf"(?i){name_pattern}", left):
                logging.debug(f"🔎 左侧未包含称呼 | 群 {group_id} | 用户 {user_id} | left={left!r}")
                return
        content = right

    # —— 清洗用于匹配 & 日志
    content = re.sub(rf"^{sep_pattern}+", "", content or "").strip()
    content_clean = normalize_for_match(content)
    logging.info(
        f"💬 触发候选 | 群 {group_id} | 用户 {user_id} | "
        f"used_at={used_at_entry} | used_reply={used_reply_entry} | "
        f"content={content!r} | clean={content_clean!r}"
    )
    
    # —— 冷却
    key = (group_id, user_id)
    if user_id != STORE.settings.get("super_user_id", ""):
        last = 触发冷却记录.get(key, 0.0)
        cd = STORE.settings.get("trigger_cooldown_seconds", 1)
        if now - last < cd:
            logging.info(f"⏳ 冷却拦截 | 群 {group_id} | 用户 {user_id} | cool_down={cd}s | since={now - last:.2f}s")
            return
        触发冷却记录[key] = now

    # —— 匹配（最长关键字优先）
    best_single = None
    best_len = 0
    best_kw_single = None
    for trg_list, order_key, body, p in STORE.trig_singles:
        for kw in sorted(trg_list, key=len, reverse=True):
            if kw and kw.lower() in content_clean.lower() and len(kw) > best_len:
                best_single = (trg_list, order_key, body, p)
                best_len = len(kw)
                best_kw_single = kw
                break

    best_group = None
    best_len_g = 0
    best_kw_group = None
    for trg_list, dir_key, parts, d in STORE.trig_groups:
        for kw in sorted(trg_list, key=len, reverse=True):
            if kw and kw.lower() in content_clean.lower() and len(kw) > best_len_g:
                best_group = (trg_list, dir_key, parts, d)
                best_len_g = len(kw)
                best_kw_group = kw
                break

    # —— 组合触发优先（若关键字不短于单条）
    if best_group and best_len_g >= best_len:
        texts = [b for _, b, _ in sorted(best_group[2], key=lambda x: x[0]) if b]
        files = [p.name for _, _, p in sorted(best_group[2], key=lambda x: x[0])]
        logging.info(f"✅ 组合触发 | 群 {group_id} | 用户 {user_id} | 关键字={best_kw_group!r} | 目录={best_group[3].name} | 片段={files}")

        if texts:
            fwd_id = await send_forward_message(ws, group_id, texts, STORE.self_id or STORE.settings.get("forward_sender_id", "2162317375"))
            logging.info(f"📨 合并转发已发送 | 群 {group_id} | fwd_id={fwd_id}")

            delay_after_forward = float(STORE.settings.get("group_forward_then_at_delay_seconds", 1.0))
            try:
                await asyncio.sleep(delay_after_forward)
            except Exception:
                pass
            order_qqs: List[str] = []
            seen = set()
            for q in at_leading + at_trailing:
                q = str(q)
                if q and q not in seen:
                    seen.add(q)
                    order_qqs.append(q)
            segs = []
            if fwd_id:
                segs.append({"type": "reply", "data": {"id": fwd_id}})
            segs.append({"type": "text", "data": {"text": "请阅读该聊天记录内的内容"}})
            for q in order_qqs:
                segs.append({"type": "at", "data": {"qq": q}})
            await send_group_msg_segments(ws, group_id, segs)
            logging.info(f"📣 已 reply+@ | 群 {group_id} | at={order_qqs} | reply_to={fwd_id}")
        return
    
    # —— 单条触发
    if best_single:
        logging.info(f"✅ 单条触发 | 群 {group_id} | 用户 {user_id} | 关键字={best_kw_single!r} | 文件={best_single[3].name}")
        await send_group_msg(ws, group_id, best_single[2])
        logging.info(f"📨 单条消息已发送 | 群 {group_id}")
        return

    logging.debug(f"🙈 未命中任何触发 | 群 {group_id} | 用户 {user_id} | clean={content_clean!r}")

# ==================== 主循环 ====================
async def reloader_loop():
    while True:
        try:
            STORE.maybe_reload()
        except Exception as e:
            logging.warning(f"热重载异常：{e}")
        await asyncio.sleep(2)

async def main():
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
    STORE.load_all()

    uri = STORE.settings.get("ws_url", "ws://127.0.0.1:6700")
    welcome_enabled = STORE.settings.get("welcome_enabled", True)
    welcome_groups = set(str(x) for x in STORE.settings.get("welcome_groups", []))
    log_group = STORE.settings.get("log_group", "")

    logging.info(f"🔌 WebSocket 地址：{uri}")
    logging.info(f"👮‍♀️ 欢迎启用：{welcome_enabled} | 欢迎群：{sorted(list(welcome_groups))}")

    # 启动热重载
    asyncio.create_task(reloader_loop())

    while True:
        try:
            async with websockets.connect(uri) as ws:
                logging.info("✅ WebSocket 连接成功，监听中…")
                # 启动收包任务
                reader_task = asyncio.create_task(ws_reader(ws))
                try:
                    while True:
                        event = await EVENT_QUEUE.get()
                        try:
                            # 记录 self_id（仅首次）
                            if STORE.self_id is None:
                                sid = event.get("self_id")
                                if sid:
                                    STORE.self_id = str(sid)
                                    logging.info(f"🤖 当前机器人 ID：{STORE.self_id}")

                            # 新成员入群
                            if event.get("post_type") == "notice" and event.get("notice_type") == "group_increase":
                                group_id = str(event["group_id"])
                                user_id = str(event["user_id"])
                                logging.info(f"📥 收到入群事件 | 群 {group_id} | 用户 {user_id}")
                                if welcome_enabled and group_id in welcome_groups:
                                    await handle_new_member(ws, group_id, user_id)
                                else:
                                    logging.info(f"⛔ 欢迎未启用或不在白名单群 | 群 {group_id}")

                            # 群消息
                            elif event.get("post_type") == "message" and event.get("message_type") == "group":
                                group_id = str(event["group_id"])
                                user_id = str(event["user_id"])
                                raw = event.get("raw_message", "")
                                logging.debug(f"✉️ 群消息 | 群 {group_id} | 用户 {user_id} | 内容={raw!r}")

                                # 手动触发迎新
                                if (
                                    welcome_enabled and group_id in welcome_groups
                                    and user_id == STORE.settings.get("super_user_id", "")
                                    and raw.strip() == STORE.settings.get("test_command", "Another Me，测试迎新")
                                ):
                                    logging.info(f"🧪 收到测试迎新指令 | 群 {group_id} | 触发者 {user_id}")
                                    await send_group_msg(ws, log_group, f"【日志】收到测试迎新指令，立即执行：{group_id}")
                                    新人记录[group_id] = [user_id]
                                    if group_id in 定时器任务:
                                        定时器任务[group_id].cancel()
                                        定时器任务.pop(group_id, None)
                                        logging.info(f"⏹️ 测试迎新：清理旧定时器 | 群 {group_id}")
                                    await schedule_welcome(ws, group_id)
                                else:
                                    if group_id in welcome_groups:
                                        await handle_custom_triggers(ws, group_id, user_id, raw, event)

                        except Exception as e:
                            logging.warning(f"事件处理异常：{e}")
                finally:
                    reader_task.cancel()
        except websockets.exceptions.ConnectionClosedError as e:
            logging.warning(f"🔌 连接关闭，尝试重连：{e}")
            await asyncio.sleep(5)
        except Exception as e:
            logging.warning(f"🔌 连接异常，5秒后重试：{e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("退出程序")
